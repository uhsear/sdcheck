#!/usr/bin/env python
"""Read a staged ArcGIS service definition (.sd) and flag a raw host or a copied dataset.

A staged .sd is a 7-Zip archive. Its manifest.xml records, for every
workspace the service will read, whether the data stays in the database
(by reference) or travels inside the .sd (copied), and the connection string
the server will use. This tool opens the archive with the standard library,
reads that manifest, and gives one verdict per file:

  PASS        every live connection names a host you expect, nothing copied
  FLAGGED     a live connection names another host, or a dataset is copied
  NO VERDICT  the manifest could not be read or names no data connection

A NO VERDICT is never a pass. The guard this replaces opened the archive with
zipfile, which cannot read 7-Zip, and carried on without an answer.

    python sdcheck.py --self-test
    python sdcheck.py staged.sd --expect-host gisdb.example.org
    python sdcheck.py a.sd b.sd --expect-host gisdb.example.org --allow-copied
    python sdcheck.py staged.sd --expect-host gisdb.example.org --sevenzip 7z
    python sdcheck.py staged.sd --expect-host gisdb.example.org --out v.json --apply

Nothing is written without --apply.

Exit codes: 0 every file passed, 1 at least one file was flagged, 2 no flag
but at least one file got no verdict, 64 usage error.
"""

from __future__ import print_function

import argparse
import base64
import io
import json
import lzma
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zlib

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# The member that holds the service manifest, at the root of the archive.
# ArcGIS Pro 3.6 writes it there. A manifest.xml further down the tree belongs
# to copied data or a nested package, not to this service.
MANIFEST_NAME = "manifest.xml"

# Largest manifest this tool will decompress. A real one is tens of KB. The
# size comes from the archive header, so a hostile archive can claim anything.
MAX_MANIFEST_BYTES = 64 * 1024 * 1024

# Largest header this tool will read. Pro's header for a 69-file .sd is 1249
# bytes packed.
MAX_HEADER_BYTES = 64 * 1024 * 1024

# Read size when streaming a packed block.
CHUNK = 1024 * 1024

# Seconds the --sevenzip fallback may run.
SEVENZIP_TIMEOUT = 120

EXIT_PASS = 0
EXIT_FLAGGED = 1
EXIT_NO_VERDICT = 2
EXIT_USAGE = 64

MAGIC = b"7z\xbc\xaf\x27\x1c"

# Property ids from 7-Zip's DOC/7zFormat.txt.
K_END, K_HEADER, K_ARCHIVE_PROPERTIES, K_ADDITIONAL_STREAMS = 0, 1, 2, 3
K_MAIN_STREAMS, K_FILES_INFO, K_PACK_INFO, K_UNPACK_INFO = 4, 5, 6, 7
K_SUBSTREAMS, K_SIZE, K_CRC, K_FOLDER = 8, 9, 10, 11
K_CODERS_UNPACK_SIZE, K_NUM_UNPACK_STREAM, K_EMPTY_STREAM = 12, 13, 14
K_EMPTY_FILE, K_NAME, K_ENCODED_HEADER = 15, 17, 23

# Coder ids from 7-Zip's DOC/Methods.txt. Only the first three are decoded.
CODER_COPY = b"\x00"
CODER_LZMA = b"\x03\x01\x01"
CODER_LZMA2 = b"\x21"
CODER_AES = b"\x06\xf1\x07\x01"


class ArchiveError(ValueError):
    """The file is not a readable 7z archive."""


class Unsupported(ArchiveError):
    """A valid 7z feature this reader does not decode. --sevenzip may."""


class ManifestError(ValueError):
    """The manifest is missing or cannot be trusted as XML."""


# =============================================================================
# 7z reader. File object in, members out. Nothing here writes.
# =============================================================================

class _Buf(object):
    """A bounded cursor over header bytes. Every read past the end raises."""

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def take(self, n):
        if n < 0 or self.pos + n > len(self.data):
            raise ArchiveError("header truncated")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def byte(self):
        return self.take(1)[0]

    def number(self):
        # 7zFormat.txt: the count of leading 1 bits in the first byte is the
        # count of extra little-endian bytes; the rest of the first byte is
        # the high part.
        first = self.byte()
        mask = 0x80
        value = 0
        for i in range(8):
            if not first & mask:
                return value | ((first & (mask - 1)) << (8 * i))
            value |= self.byte() << (8 * i)
            mask >>= 1
        return value

    def count(self):
        """A number used as an item count. It cannot exceed the bytes left."""
        n = self.number()
        if n > len(self.data) - self.pos + 1:
            raise ArchiveError("header claims %d items in %d bytes"
                               % (n, len(self.data) - self.pos))
        return n

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def bits(self, n):
        raw = self.take((n + 7) // 8)
        return [bool(raw[i // 8] & (0x80 >> (i % 8))) for i in range(n)]

    def expect(self, nid, what):
        got = self.byte()
        if got != nid:
            raise ArchiveError("expected %s (0x%02x), found 0x%02x"
                               % (what, nid, got))


def _digests(buf, n):
    defined = [True] * n if buf.byte() else buf.bits(n)
    return [buf.u32() if d else None for d in defined]


class Folder(object):
    """One coder chain and the unpacked sizes of its outputs."""

    def __init__(self, coders, bound_outs):
        self.coders = coders            # [(id bytes, props bytes, ins, outs)]
        self.bound_outs = bound_outs    # out indexes fed into another coder
        self.unpack_sizes = []
        self.crc = None

    def size(self):
        free = [s for i, s in enumerate(self.unpack_sizes)
                if i not in self.bound_outs]
        return free[0] if free else 0


def _folder(buf):
    coders = []
    total_in = total_out = 0
    for _ in range(buf.count()):
        flag = buf.byte()
        if flag & 0x80:
            raise Unsupported("alternative coder methods")
        codec = buf.take(flag & 0x0F)
        ins = outs = 1
        if flag & 0x10:
            ins, outs = buf.count(), buf.count()
        props = buf.take(buf.number()) if flag & 0x20 else b""
        coders.append((codec, props, ins, outs))
        total_in += ins
        total_out += outs
    bound = set()
    for _ in range(total_out - 1):
        buf.number()
        bound.add(buf.number())
    packed = total_in - (total_out - 1)
    if packed > 1:
        for _ in range(packed):
            buf.number()
    return Folder(coders, bound)


class Streams(object):
    def __init__(self):
        self.pack_pos = 0
        self.pack_sizes = []
        self.folders = []
        self.substreams = []   # per folder: [(size, crc)]


def _streams(buf):
    st = Streams()
    nid = buf.byte()
    if nid == K_PACK_INFO:
        st.pack_pos = buf.number()
        n = buf.count()
        nid = buf.byte()
        if nid == K_SIZE:
            st.pack_sizes = [buf.number() for _ in range(n)]
            nid = buf.byte()
        if nid == K_CRC:
            _digests(buf, n)
            nid = buf.byte()
        if nid != K_END:
            raise ArchiveError("pack info does not end")
        nid = buf.byte()
    if nid == K_UNPACK_INFO:
        buf.expect(K_FOLDER, "folder list")
        n = buf.count()
        if buf.byte():
            raise Unsupported("folders stored outside the header")
        st.folders = [_folder(buf) for _ in range(n)]
        buf.expect(K_CODERS_UNPACK_SIZE, "unpack sizes")
        for f in st.folders:
            f.unpack_sizes = [buf.number()
                              for _ in range(sum(c[3] for c in f.coders))]
        nid = buf.byte()
        if nid == K_CRC:
            for f, crc in zip(st.folders, _digests(buf, n)):
                f.crc = crc
            nid = buf.byte()
        if nid != K_END:
            raise ArchiveError("unpack info does not end")
        nid = buf.byte()
    st.substreams = [[(f.size(), f.crc)] for f in st.folders]
    if nid == K_SUBSTREAMS:
        _substreams(buf, st)
        nid = buf.byte()
    if nid != K_END:
        raise ArchiveError("streams info does not end")
    return st


def _substreams(buf, st):
    nums = [1] * len(st.folders)
    nid = buf.byte()
    if nid == K_NUM_UNPACK_STREAM:
        nums = [buf.count() for _ in st.folders]
        nid = buf.byte()
    sizes = []
    for f, n in zip(st.folders, nums):
        if n == 0:
            sizes.append([])
        elif nid == K_SIZE:
            part = [buf.number() for _ in range(n - 1)]
            last = f.size() - sum(part)
            if last < 0:
                raise ArchiveError("substream sizes exceed their folder")
            sizes.append(part + [last])
        elif n == 1:
            sizes.append([f.size()])
        else:
            raise ArchiveError("a folder of %d streams has no sizes" % n)
    if nid == K_SIZE:
        nid = buf.byte()
    # A folder holding exactly one stream with a folder CRC already has its
    # digest. Every other stream's digest comes from this list, in order.
    unknown = sum(n for f, n in zip(st.folders, nums)
                  if not (n == 1 and f.crc is not None))
    crcs = [None] * unknown
    if nid == K_CRC:
        crcs = _digests(buf, unknown)
        nid = buf.byte()
    if nid != K_END:
        raise ArchiveError("substreams info does not end")
    it = iter(crcs)
    st.substreams = []
    for f, n, part in zip(st.folders, nums, sizes):
        if n == 1 and f.crc is not None:
            st.substreams.append([(part[0], f.crc)])
        else:
            st.substreams.append([(s, next(it)) for s in part])


class Entry(object):
    """One archive member. folder is None for a directory or empty file."""

    def __init__(self, name, size, folder, offset, crc, is_dir):
        self.name = name
        self.size = size
        self.folder = folder
        self.offset = offset
        self.crc = crc
        self.is_dir = is_dir


def _files(buf, st):
    n = buf.count()
    names = None
    empty_stream = [False] * n
    empty_file = []
    while True:
        prop = buf.byte()
        if prop == K_END:
            break
        data = _Buf(buf.take(buf.number()))
        if prop == K_EMPTY_STREAM:
            empty_stream = data.bits(n)
        elif prop == K_EMPTY_FILE:
            empty_file = data.bits(sum(empty_stream))
        elif prop == K_NAME:
            if data.byte():
                raise Unsupported("file names stored outside the header")
            raw = data.take(len(data.data) - 1)
            names = raw.decode("utf-16-le", "replace").split(u"\x00")[:n]
    if names is None or len(names) != n:
        raise ArchiveError("file names missing from the header")
    streams = [(fi, off, size, crc)
               for fi, subs in enumerate(st.substreams)
               for off, size, crc in _offsets(subs)]
    if sum(1 for e in empty_stream if not e) != len(streams):
        raise ArchiveError("file count does not match stream count")
    entries = []
    nxt = iter(streams)
    empties = iter(empty_file + [False] * n)
    for name, empty in zip(names, empty_stream):
        name = name.replace("\\", "/")
        if empty:
            entries.append(Entry(name, 0, None, 0, None, not next(empties)))
        else:
            fi, off, size, crc = next(nxt)
            entries.append(Entry(name, size, fi, off, crc, False))
    return entries


def _offsets(subs):
    off = 0
    for size, crc in subs:
        yield off, size, crc
        off += size


class Archive(object):
    def __init__(self, streams, entries):
        self.streams = streams
        self.entries = entries

    def find(self, name):
        for e in self.entries:
            if e.name == name and not e.is_dir:
                return e
        return None

    def under(self, prefix):
        """Members below one in-package path, compared without case."""
        p = prefix.replace("\\", "/").strip("/").lower() + "/"
        return [e for e in self.entries
                if e.name.lower().startswith(p) and not e.is_dir]


def _coder_filter(codec, props):
    """The lzma module filter for one 7z coder, or None for Copy."""
    if codec == CODER_COPY:
        return None
    if codec == CODER_LZMA:
        if len(props) != 5:
            raise ArchiveError("LZMA properties are %d bytes, not 5"
                               % len(props))
        d = props[0]
        if d >= 9 * 5 * 5:
            raise ArchiveError("LZMA properties byte %d is out of range" % d)
        return {"id": lzma.FILTER_LZMA1, "lc": d % 9, "lp": (d // 9) % 5,
                "pb": d // 45, "dict_size": struct.unpack("<I", props[1:])[0]}
    if codec == CODER_LZMA2:
        if len(props) != 1 or props[0] > 40:
            raise ArchiveError("LZMA2 properties are not one byte under 41")
        p = props[0]
        size = 0xFFFFFFFF if p == 40 else (2 | (p & 1)) << (p // 2 + 11)
        return {"id": lzma.FILTER_LZMA2, "dict_size": size}
    if codec == CODER_AES:
        raise Unsupported("the archive is encrypted")
    raise Unsupported("coder %s is not decoded by this reader"
                      % (codec.hex() or "(empty)"))


def _unpack(fh, st, index, need):
    """The first `need` bytes of folder `index`, streamed from the file."""
    folder = st.folders[index]
    if len(folder.coders) != 1:
        raise Unsupported("a chain of %d coders" % len(folder.coders))
    codec, props, _, _ = folder.coders[0]
    filt = _coder_filter(codec, props)
    if index >= len(st.pack_sizes):
        raise ArchiveError("folder %d has no packed stream" % index)
    start = 32 + st.pack_pos + sum(st.pack_sizes[:index])
    left = st.pack_sizes[index]
    fh.seek(start)
    if filt is None:
        out = fh.read(min(need, left))
    else:
        dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[filt])
        parts = []
        got = 0
        try:
            # Each call either fills the request, reaches the end of the
            # stream, or uses up its input. So while bytes are still owed,
            # the decoder always wants more input, never an empty call.
            while got < need and not dec.eof:
                if not left:
                    break
                chunk = fh.read(min(CHUNK, left))
                if not chunk:
                    break
                left -= len(chunk)
                piece = dec.decompress(chunk, max_length=need - got)
                parts.append(piece)
                got += len(piece)
        except lzma.LZMAError as exc:
            raise ArchiveError("packed data is corrupt: %s" % exc)
        out = b"".join(parts)
    if len(out) < need:
        raise ArchiveError("packed data ends after %d of %d bytes"
                           % (len(out), need))
    return out


def read_archive(fh):
    """Parse the header of a 7z archive. Reads no member data."""
    fh.seek(0)
    sig = fh.read(32)
    if len(sig) < 32 or sig[:6] != MAGIC:
        raise ArchiveError("not a 7z archive (a staged .sd always is one)")
    if sig[6] != 0:
        raise Unsupported("7z format version %d.%d" % (sig[6], sig[7]))
    if zlib.crc32(sig[12:32]) & 0xFFFFFFFF != struct.unpack("<I", sig[8:12])[0]:
        raise ArchiveError("start header CRC does not match")
    offset, size, crc = struct.unpack("<QQI", sig[12:32])
    if size == 0:
        return Archive(Streams(), [])
    if size > MAX_HEADER_BYTES:
        raise ArchiveError("header of %d bytes is over the %d byte limit"
                           % (size, MAX_HEADER_BYTES))
    fh.seek(32 + offset)
    header = fh.read(size)
    if len(header) != size:
        raise ArchiveError("archive is truncated before its header")
    if zlib.crc32(header) & 0xFFFFFFFF != crc:
        raise ArchiveError("header CRC does not match")
    for _ in range(4):
        buf = _Buf(header)
        nid = buf.byte()
        if nid == K_HEADER:
            return _header(buf)
        if nid != K_ENCODED_HEADER:
            raise ArchiveError("unknown header type 0x%02x" % nid)
        st = _streams(buf)
        if not st.folders:
            raise ArchiveError("encoded header has no folder")
        need = st.folders[0].size()
        if need > MAX_HEADER_BYTES:
            raise ArchiveError("encoded header unpacks to %d bytes" % need)
        header = _unpack(fh, st, 0, need)
        _check_crc(header, st.folders[0].crc, "encoded header")
    raise ArchiveError("header is encoded more than four times")


def _header(buf):
    nid = buf.byte()
    if nid == K_ARCHIVE_PROPERTIES:
        while buf.byte():
            buf.take(buf.number())
        nid = buf.byte()
    if nid == K_ADDITIONAL_STREAMS:
        raise Unsupported("additional header streams")
    st = Streams()
    if nid == K_MAIN_STREAMS:
        st = _streams(buf)
        nid = buf.byte()
    entries = []
    if nid == K_FILES_INFO:
        entries = _files(buf, st)
        nid = buf.byte()
    if nid != K_END:
        raise ArchiveError("header does not end")
    return Archive(st, entries)


def _check_crc(data, crc, what):
    if crc is not None and zlib.crc32(data) & 0xFFFFFFFF != crc:
        raise ArchiveError("%s CRC does not match" % what)


def read_member(fh, archive, entry):
    """The bytes of one member, CRC-checked."""
    if entry.folder is None:
        return b""
    if entry.offset + entry.size > MAX_MANIFEST_BYTES:
        raise Unsupported("%s ends %d bytes into its block, over the %d byte "
                          "limit" % (entry.name, entry.offset + entry.size,
                                     MAX_MANIFEST_BYTES))
    data = _unpack(fh, archive.streams, entry.folder,
                   entry.offset + entry.size)[entry.offset:]
    _check_crc(data, entry.crc, entry.name)
    return data


# =============================================================================
# Manifest. Bytes in, databases out.
# =============================================================================

def _text(el, tag):
    found = el.find(tag)
    if found is None or found.text is None:
        return None
    return found.text.strip()


def parse_manifest(data):
    """[{name, by_reference, connection, in_package, datasets}] per database.

    by_reference is True, False, or None when the element is absent or holds
    anything else. The connection is the OnServerConnectionString, the one the
    server uses. OnPremiseConnectionString records the publishing machine and
    is never read.
    """
    flat = data.lower().replace(b"\x00", b"")
    if b"<!doctype" in flat or b"<!entity" in flat:
        # The stdlib parser expands internal entities. A manifest never
        # declares any, so one that does is refused before it is parsed.
        raise ManifestError("manifest declares a DTD or entity; refused")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ManifestError("manifest is not well-formed XML: %s" % exc)
    if root.tag != "SVCManifest":
        raise ManifestError("root element is %s, not SVCManifest" % root.tag)
    out = []
    for db in root.findall("./Databases/SVCDatabase"):
        flag = (_text(db, "ByReference") or "").lower()
        out.append({
            "name": _text(db, "Name") or "(unnamed)",
            "by_reference": {"true": True, "false": False}.get(flag),
            "connection": _text(db, "OnServerConnectionString") or "",
            "in_package": _text(db, "InPackagePath") or "",
            "datasets": [_text(ds, "Name") or "(unnamed)"
                         for ds in db.findall("./Datasets/SVCDataset")],
        })
    return out


def _host_part(value):
    """The host in a SERVER-style value: drop \\instance, ,port, /service."""
    host = re.split(r"[\\,/]", value.strip())[0]
    m = re.match(r"^(.+):\d+$", host)
    return m.group(1) if m else host


def hosts_in(connection):
    """[(key, host)] for every key in a connection string that names a host.

    SERVER and INSTANCE are documented workspace connection properties.
    DB_CONNECTION_PROPERTIES carries the same host in every SQL Server
    connection ArcGIS Pro 3.6 staged here. DATABASE names a host only when it
    is a UNC path to a file geodatabase on a share. Values are never kept,
    only the host part, because the same string holds the password.
    """
    found = []
    for part in connection.split(";"):
        key, sep, value = part.partition("=")
        key = key.strip().upper()
        value = value.strip()
        if not sep or not value:
            continue
        if key in ("SERVER", "DB_CONNECTION_PROPERTIES"):
            host = _host_part(value)
        elif key == "INSTANCE":
            pieces = value.split(":", 2)
            if len(pieces) == 3 and pieces[0].lower() == "sde":
                host = _host_part(pieces[2])
            else:
                # A bare port such as 5151 is an application-server
                # connection, which names no database host here.
                continue
        elif key == "DATABASE" and value.startswith("\\\\"):
            host = value[2:].split("\\")[0]
        else:
            continue
        if host:
            found.append((key, host))
    return found


# The verdict for one database.
OK, RAW_HOST, COPIED, COPY_ALLOWED, UNKNOWN = (
    "OK", "RAW HOST", "COPIED", "COPY ALLOWED", "NO VERDICT")


def judge_database(db, expect_hosts, allow_copied, packed_files=None):
    """(class, reason) for one parsed database. Pure."""
    expect = set(h.lower() for h in expect_hosts)
    names = ", ".join(db["datasets"]) or "(none listed)"
    if db["by_reference"] is False:
        where = db["in_package"] or "(no in-package path)"
        if packed_files is None:
            count = "file count unknown"
        else:
            count = "%d file(s) in the .sd" % packed_files
        reason = ("data travels inside the .sd: %s, %s; dataset(s): %s"
                  % (where, count, names))
        return (COPY_ALLOWED if allow_copied else COPIED), reason
    if db["by_reference"] is None:
        return UNKNOWN, "ByReference is missing or not true/false"
    if not db["connection"]:
        return UNKNOWN, "referenced, but the manifest has no server connection"
    hosts = hosts_in(db["connection"])
    bad = sorted(set("%s=%s" % (k, h) for k, h in hosts
                     if h.lower() not in expect))
    if bad:
        return RAW_HOST, ("live connection names %s, not an expected host; "
                          "dataset(s): %s" % (", ".join(bad), names))
    if hosts:
        return OK, ("referenced through %s"
                    % ", ".join(sorted(set(h for _, h in hosts))))
    return OK, "referenced, and the connection names no host"


def judge(dbs, expect_hosts, allow_copied, packed=None):
    """(exit code, [(name, class, reason)]) for one manifest. Pure."""
    packed = packed or {}
    rows = []
    for db in dbs:
        cls, reason = judge_database(db, expect_hosts, allow_copied,
                                     packed.get(db["name"]))
        rows.append((db["name"], cls, reason))
    classes = set(r[1] for r in rows)
    if classes & set([RAW_HOST, COPIED]):
        return EXIT_FLAGGED, rows
    if not rows:
        rows.append(("-", UNKNOWN, "the manifest lists no data connection, "
                                   "so there is nothing to check"))
        return EXIT_NO_VERDICT, rows
    if UNKNOWN in classes:
        return EXIT_NO_VERDICT, rows
    return EXIT_PASS, rows


def combine(codes):
    """One exit code for many files: a flag outranks a missing verdict."""
    if EXIT_FLAGGED in codes:
        return EXIT_FLAGGED
    if EXIT_NO_VERDICT in codes:
        return EXIT_NO_VERDICT
    return EXIT_PASS


# =============================================================================
# I/O: one file to one verdict.
# =============================================================================

def sevenzip_manifest(command, path):
    """manifest.xml through an external 7-Zip, for archives this reader refuses.

    No -r: without it 7-Zip matches the name at the archive root only, so a
    manifest.xml inside copied data is not concatenated onto this one.
    stdin is closed so a password prompt ends instead of waiting.
    """
    argv = list(command) + ["e", "-so", "-y", path, MANIFEST_NAME]
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=SEVENZIP_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManifestError("--sevenzip could not run: %s" % exc)
    if proc.returncode != 0 or not proc.stdout:
        raise ManifestError("--sevenzip exited %d with %d bytes"
                            % (proc.returncode, len(proc.stdout)))
    return proc.stdout


def check_file(path, expect_hosts, allow_copied, sevenzip=None):
    """{path, exit, rows, reader} for one .sd. Never raises on bad input."""
    result = {"path": path, "reader": "stdlib"}
    packed = None
    try:
        with open(path, "rb") as fh:
            try:
                archive = read_archive(fh)
                entry = archive.find(MANIFEST_NAME)
                if entry is None:
                    raise ManifestError("no %s at the archive root"
                                        % MANIFEST_NAME)
                data = read_member(fh, archive, entry)
                dbs = parse_manifest(data)
                packed = dict((d["name"], len(archive.under(d["in_package"])))
                              for d in dbs if d["in_package"])
            except Unsupported as exc:
                if not sevenzip:
                    raise ArchiveError("%s; pass --sevenzip to read it with "
                                       "7-Zip" % exc)
                result["reader"] = "sevenzip (%s)" % exc
                dbs = parse_manifest(sevenzip_manifest(sevenzip, path))
    except (ArchiveError, ManifestError, IOError, OSError) as exc:
        result["exit"] = EXIT_NO_VERDICT
        result["rows"] = [("-", UNKNOWN, str(exc))]
        return result
    result["exit"], result["rows"] = judge(dbs, expect_hosts, allow_copied,
                                           packed)
    return result


VERDICT_WORD = {EXIT_PASS: "PASS", EXIT_FLAGGED: "FLAGGED",
                EXIT_NO_VERDICT: "NO VERDICT"}


def describe(result):
    lines = ["%s: %s" % (result["path"], VERDICT_WORD[result["exit"]])]
    if result["reader"] != "stdlib":
        lines.append("  read with %s" % result["reader"])
    for name, cls, reason in result["rows"]:
        lines.append("  %-12s %s: %s" % (cls, name, reason))
    return lines


# =============================================================================
# Self-test
# =============================================================================

def _fixture_manifest(dbs):
    """A synthetic manifest shaped like the ones ArcGIS Pro 3.6 stages.

    dbs: [(name, by_reference, on_server, in_package, [datasets])]. The
    on-premise string always names a host nobody expects, so a test that
    passes proves the tool never read it.
    """
    parts = ["<SVCManifest xsi:type='typens:SVCManifest' "
             "xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance' "
             "xmlns:typens='http://www.esri.com/schemas/ArcGIS/3.6.0'>",
             "<Databases xsi:type='typens:ArrayOfSVCDatabase'>"]
    for name, by_ref, conn, pkg, datasets in dbs:
        parts.append("<SVCDatabase xsi:type='typens:SVCDatabase'>")
        parts.append("<Name>%s</Name><InPackagePath>%s</InPackagePath>"
                     % (name, pkg))
        parts.append("<OnPremiseConnectionString>SERVER=author-pc.example.net;"
                     "DATABASE=scratch</OnPremiseConnectionString>")
        parts.append("<Datasets xsi:type='typens:ArrayOfSVCDataset'>")
        for ds in datasets:
            parts.append("<SVCDataset xsi:type='typens:SVCDataset'>"
                         "<Name>%s</Name><DatasetType>esriDTFeatureClass"
                         "</DatasetType></SVCDataset>" % ds)
        parts.append("</Datasets>")
        if by_ref is not None:
            parts.append("<ByReference>%s</ByReference>" % by_ref)
        if conn is not None:
            parts.append("<OnServerConnectionString>%s"
                         "</OnServerConnectionString>" % conn)
        parts.append("</SVCDatabase>")
    parts.append("</Databases></SVCManifest>")
    return "".join(parts).encode("utf-8")


ALIAS = "gisdb.example.org"
RAW = "sqlnode2.example.org"
SECRET = "c2VjcmV0LW5vdC1yZWFs"


def _sde(host, secret=SECRET):
    return ("ENCRYPTED_PASSWORD=%s;SERVER=%s;INSTANCE=sde:sqlserver:%s;"
            "DBCLIENT=sqlserver;DB_CONNECTION_PROPERTIES=%s;DATABASE=assets;"
            "USER=reader;AUTHENTICATION_MODE=DBMS"
            % (secret, host, host, host))


def _num(n):
    """7z UINT64 encoding, the inverse of _Buf.number, for test archives."""
    for extra in range(8):
        if n < 1 << (7 * (extra + 1)):
            lead = (0xFF << (8 - extra)) & 0xFF
            low = n & ((1 << (8 * extra)) - 1)
            return bytes([lead | (n >> (8 * extra))]) + \
                low.to_bytes(extra, "little")
    return b"\xff" + n.to_bytes(8, "little")


def _bits(flags):
    out = bytearray((len(flags) + 7) // 8)
    for i, f in enumerate(flags):
        if f:
            out[i // 8] |= 0x80 >> (i % 8)
    return bytes(out)


def _build_7z(members, encode_header=False, solid=False, coder="copy",
              header_tail=b""):
    """A 7z archive from [(name, bytes or None for a directory)].

    The test archives exercise the reader's own branches. Archives made by
    7-Zip itself are embedded separately, so the reader is not only ever
    checked against its own writer.
    """
    files = [(n, d) for n, d in members if d]
    blocks = [b"".join(d for _, d in files)] if solid and files else \
        [d for _, d in files]
    packed = []
    for raw in blocks:
        if coder == "lzma2":
            packed.append(lzma.compress(raw, format=lzma.FORMAT_RAW,
                                        filters=[{"id": lzma.FILTER_LZMA2}]))
        else:
            packed.append(raw)
    if coder == "lzma2":
        codec = b"\x21\x21\x01\x10"  # id 21, one props byte = 16 (1 MiB)
    else:
        codec = b"\x01\x00"
    body = b"".join(packed)

    def streams(pack_pos, pack_sizes, folder_sizes, folder_crcs, subs,
                codec=codec):
        s = bytearray([K_PACK_INFO]) + _num(pack_pos) + _num(len(pack_sizes))
        s += bytes([K_SIZE]) + b"".join(_num(p) for p in pack_sizes)
        s += bytes([K_END, K_UNPACK_INFO, K_FOLDER]) + _num(len(folder_sizes))
        s += b"\x00" + b"".join(b"\x01" + codec for _ in folder_sizes)
        s += bytes([K_CODERS_UNPACK_SIZE])
        s += b"".join(_num(n) for n in folder_sizes)
        if folder_crcs is not None:
            s += bytes([K_CRC, 1]) + b"".join(struct.pack("<I", c)
                                              for c in folder_crcs)
        s += bytes([K_END])
        if subs is not None:
            s += bytes([K_SUBSTREAMS, K_NUM_UNPACK_STREAM]) + _num(len(subs))
            s += bytes([K_SIZE]) + b"".join(_num(len(d)) for d in subs[:-1])
            s += bytes([K_CRC, 1]) + b"".join(
                struct.pack("<I", zlib.crc32(d) & 0xFFFFFFFF) for d in subs)
            s += bytes([K_END])
        return bytes(s + bytes([K_END]))

    if solid and files:
        main = streams(0, [len(p) for p in packed], [len(blocks[0])], None,
                       [d for _, d in files])
    else:
        main = streams(0, [len(p) for p in packed], [len(b) for b in blocks],
                       [zlib.crc32(b) & 0xFFFFFFFF for b in blocks], None)
    names = b"".join(n.encode("utf-16-le") + b"\x00\x00" for n, _ in members)
    empty = [not d for _, d in members]
    fi = bytearray([K_FILES_INFO]) + _num(len(members))
    if any(empty):
        fi += bytes([K_EMPTY_STREAM]) + _num(len(_bits(empty))) + _bits(empty)
        fi += bytes([K_EMPTY_FILE]) + _num(len(_bits([False] * sum(empty))))
        fi += _bits([d is not None for _, d in members if not d])
    fi += bytes([K_NAME]) + _num(len(names) + 1) + b"\x00" + names
    fi += bytes([K_END])
    header = bytes([K_HEADER, K_MAIN_STREAMS]) + main + bytes(fi) + \
        header_tail + bytes([K_END])
    if encode_header:
        enc = header
        hdr_pos = len(body)
        body += enc
        header = bytes([K_ENCODED_HEADER]) + streams(
            hdr_pos, [len(enc)], [len(enc)],
            [zlib.crc32(enc) & 0xFFFFFFFF], None, b"\x01\x00")
    start = struct.pack("<QQI", len(body), len(header),
                        zlib.crc32(header) & 0xFFFFFFFF)
    return (MAGIC + b"\x00\x04" + struct.pack("<I", zlib.crc32(start)
                                              & 0xFFFFFFFF)
            + start + body + header)


# Archives made by 7-Zip 24.08 (x64) from synthetic files, base64. Each holds
# manifest.xml built by _fixture_manifest, plus the members named below.
# Regenerate with the commands in the README's "Test archives" section.
REAL_7Z = {
    'pro_like': (
        'N3q8ryccAARjJpGEMQMAAAAAAAAjAAAAAAAAAF2Xfq0AORlKzT9vn9x2i7e8X/IE'
        '4TeiEuKVpcdyDPRbqaRjylaPRDBtWegvAP0mKjvgKWZP5gJ4Ej33kLAoGF4M1FmM'
        'Xw7G4tDH4AACgDa/YAAAAB4UxsQ0uOHWEmEh0pxs7mwx9TK5YVmJLlSxG88GsCx5'
        'NaS6WkT99Vuy5Oz9gVxdQ0rzuycfTWr/FnejF4vL1P92tgW2GofIfY/+5DlY1WBu'
        'V8rPhbWJ7bdL2VMzrGydXwUPUiBOSydsTY6hW52AnnIHHqiPwUQcW/3nl6/dDxSl'
        'rNofX/e/FGfCWe2MnkIYaOyV15PsVs3h4rjwL82RpLe/KeY1Yu+kbypff+/ufXXP'
        'A8Sgx+VMqR9TuK/eZ8ELsnF2s8SEZSYQTVzUbpmumRIk1L7aKnxMDou8MoVZ97W8'
        'pGLKhIimA0hjmqTsv2FkJkqCB62fpcdKopwq2EdVIKJphCqaE3cpmpN5eMwrFVou'
        'gi7hFw2vaBKfIUpOpuHbEny5L9osFrtM/fKJNhreoDAYUBM9d7r25/dHspt79vOZ'
        'bQtJ7m7tZO/oQpzGyy0n5QFpKO5xPEKbcTjZrRMzop5tmruqedthrxxKf1eNFVqn'
        'aQ4cKosFW/9LuxGB1tX5qTtSpWXwTLuI4eItZmWZP8a4awB/shzE3GMjdx9wtcFf'
        'GHZKgpn8d4V8x96Ru+GQYNkLIb+O1sJr0G15ofW6BTRfwMSU77USu0JovXwlAdXo'
        'ePLcjC42cCTemy//kMoQP0Ocrl+Jn7I/qlNEASitU6QSeyPPR1YGH3ILFLhwh4UL'
        '+w6twfnkdyPWiPCzckeXlLG45m9G/kB1nu4AAIEzB65PonIuYm3cBO933UB7dTJw'
        'xPmyba55fTTnypyjOO0knYgwjG/PcQuQ1R3fhpQfy+ZZ3oH2hDHrZjtNxvqiUIl0'
        'W9fxgifNTUMSeW8v+LeX9qt8u+0JekRRiDK3iBTpbNEEFdn2lOhj0yf8ZUSz+dVk'
        '9z+L+AlQwPHnsvnXPEN5J9+/4ypUWP+Fji5JEeXh43AOvxcsFIEycr2VvVRYnDaE'
        'CgGtjiiIJiA8BjWec6tP3hRXQLrruw0nt+IBl8RNuWvlFwaCagEJgMcABwsBAAEj'
        'AwEBBV0AEAAADIG+CgHLWFtuAAA='
    ),
    'lzma2_solid': (
        'N3q8ryccAAQB0avQ9QEAAAAAAACiAAAAAAAAALYzO67gA/8B7V0AHhTGxDS44dYS'
        'YSHSnGzubDH1MrlhWYkuVLEbzwawLHk1pLpaRP31W7Lk7P2BXF1DSvO7Jx9Nav8W'
        'd6MXi8vU/3a2BbYah8h9j/7kOVjVYG5Xys+FtYntt0vZUzOsbJ1fBQ9SIE5LJ2xN'
        'jqFbnYCecgceqI/BRBxb/eeXr90PFKWs2h9f978UZ8JZ7YyeQhho7JXXk+xWw1um'
        'PTouMlo/IQFgbufuZmL6t7zSMVAjrLRYDilZgysc0WOW4+6mCqZNl6D0Mb0zACY7'
        'suZMFfuRzhiNzT4H9ch9LR5EC1mS9mS4p8i3GcLqeyfwSV5m9A4t5AILQjSstJ1I'
        'CI1aloZnXuxk7eGykROJCADhQTSvR687xfOB1g7k5tIUjCIuqLKnjyqbA9LCPnWf'
        'NsE8Lav+romW8jU87z2IhlaEvCu1DgPhu/X+N+a+2qN7SbuW2T65QKaQ8I00m/E3'
        'Cnn4+Gt5J8nvtvys3E7zLqSv/TlHjIRDiKVSGNrKJ4UFksJSacBjgFP5nLrk2hHk'
        '+6URkbvAzxotnmS1prpZ+NvhmrAEqm3AP9El8lc3pPkMGlfQCDo5kwQILgdhjoZF'
        'AwIeXJVpc0rYmD6dBe0q1GEZXHCkXKVvsrtY4yluyJ6riG23zU2eeIOPOcOt7Rmj'
        'TtDwAAABBAYAAQmB9QAHCwEAASEhAQAMhAAACA0CCYOcCgHZ7uK2KPMZfAAABQMO'
        'AYARRQBwADMAMAAAAG0AYQBuAGkAZgBlAHMAdAAuAHgAbQBsAAAAcAAzADAALwBz'
        'AGUAcgB2AGkAYwBlAC4AbQBhAHAAeAAAABQaAQBEjmSS4lfdAUSOZJLiV90BRI5k'
        'kuJX3QEVDgEAEAAAACAAAAAgAAAAAAA='
    ),
    'bcj': (
        'N3q8ryccAASsISQ+0gEAAAAAAAByAAAAAAAAAH+THZMAHhTGxDS44dYSYSHSnGzu'
        'bDH1MrlhWYkuVLEbzwawLHk1pLpaRP31W7Lk7P2BXF1DSvO7Jx9Nav8Wd6MXi8vU'
        '/3a2BbYah8h9j/7kOVjVYG5Xys+FtYntt0vZUzOsbJ1fBQ9SIE5LJ2xNjqFbnYCe'
        'cgceqI/BRBxb/eeXr90PFKWs2h9f978UZ8JZ7YyeQhho7JXXk+xWw1umPTouMlo/'
        'IQFgbufuZmL6t7zSMVAjrLRYDilZgysc0WOW4+6mCqZNl6D0Mb0zACY7suZMFfuR'
        'zhiNzT4H9ch9LR5EC1mS9mS4p8i3GcLqeyfwSV5m9A4t5AILQjSstJ1ICI1aloZn'
        'Xwna+3mFI11Zri4KVgzsMEJR0KluJ+Nee1QZ2Kef01zEl2H08TYCppKH0MYlQh9W'
        'E6Rl3uK32cTlqGeMvnLAO59fmD06+/T52Z9b0y/8Uz0cupEr1hj7GVGUeV48nSvk'
        'R1NCCwNykDMOnU4NIGv+cc+rD/IcHMqJB02HK+f0SFVNgc2GU88YSRV0QpYG+oQJ'
        'QdTUAIBo53KENcrCOC5q1uUV8JzU3607V97qoH8NbmUpoObU/HquVexhrhwqKsrT'
        '5ivReSsjQrZ/IGyNc1+QNG/HAQQGAAEJgdIABwsBAAIjAwEBBV0AEAAABAMDAQMB'
        'AAyDkIOQAAgKAaGA6NwAAAUBGQsAAAAAAAAAAAAAABEbAG0AYQBuAGkAZgBlAHMA'
        'dAAuAHgAbQBsAAAAGQAUCgEA/bVnkuJX3QEVBgEAIAAAAAAA'
    ),
    'aes': (
        'N3q8ryccAAQ8XyFOYAIAAAAAAAAwAAAAAAAAAI0Z+XscuREEx+rrGMZIWqo6Wo9S'
        'm2en8DtvTweW/I9d2VIBbgZJ1/ZZtsOzbSFwdAJ4PIDtBsQEocgvIVgbpDWzwdHp'
        'f7mVrp1JidiZt3bQgUDvoWzD31/q4PWWd7jfJogK6tFFslqwZ5MgBMcFThjss/ED'
        'x2eVJh4UHu+qRWjiHMgG3RNDMKH2TSwOdlOgp3tu7gJ1Nr7AH28DiuSPW7Ot9ysy'
        'M7KoqW53okQgIgYOkU/bllvy7iKY2XAC5BePQQiE/kl4VgaXNv1oEtRlOmFsb/r7'
        'zwdQobx631WDgT8vnd34KQKoG0sZNcbWYKRbqxYu/6r5p1JVDAjBfsevPSaDJRlP'
        'Ig2V+2chdPyyhzzFrWvOD+OMZgR6qawLkWwh4gmz/YwWBYi+rdoqcNmMi5XLYKIP'
        'trkFE9cMe3WnalilXmrnsekhVc+EFwaxOIxujWvFQ6geVo9Bc7mrQKyyKPumQ7IL'
        'HuRfibloENtXiTlylTTSszBVrSX82nVuEWTC6irDTBlevYTUg38h6zooKudKGfTy'
        '/aN6fhDtGtBIsApbP9wOAlYJIVBLpCYtUkiYd2YyclhV9z/KIX6S4rCvqPkMnsEp'
        'LieoYoMZvoxoHSJn/3yJNUrs0xiknpISTMNqHz/3bUgX5D187k1X2rZsHHT7iEaa'
        'M3hlqAFUGQF80NxgJoSgG9h/PDSmYcSrM3FpcLe2I7Lx9M2aN6Ih4nQXfN2skKEX'
        'Zti92WbXylkHriH5RvPDi6xlzOdKcp/77JomjwEmq5rz/6Lpa2wYTMVn85eKkjJ3'
        'LKXo9rP9z3aBMWGrNDBy5xcGgeABCYCAAAcLAQABJAbxBwESUw+nbqYRjtowZ03h'
        'SzTFgecEDHIKAREEQ6MAAA=='
    ),
    'ppmd': (
        'N3q8ryccAAQKC2FJowEAAAAAAABiAAAAAAAAAOq0QZYAPBmVEWNEj5yZ9q/eqeiG'
        'cMGe2AerN7RtmL8ho5SkWhZsBURos6BxaDjNmR4IxdGqXrpZ5Z64d2CLmRLRV3H9'
        'wcii1yH6cS6CRsX/5OVNwLJBz9iqnwfi40iIxU3bjRiJn/KziRy/z0P+P626fvSL'
        '99EsuSl/DFxDlEm0azOJVxxSt7GxtFP5GW/hZG8Fr0a2mUtaIbRzHbfgTYNszzkb'
        'CPM62lhYQ1Qcvk3WAt6QmHabM9x/SGwAZN6VM52kMaJUwujtEyrV7+Bc4iejAfhU'
        '0dXIQVyOKdPdS7dsyRksD6OIvJrfrdEHl4FzVlGdWR8xoglnAJOG6wX9zpGfh0lw'
        'CO+CdgAagddpMiEDlTOQOfPU9ldj9B/92j9POW5Ibbq2M57O1Y1BZk07orKKvPWC'
        'KKfM1/bkkybD7kWXvtcPPjENdIqmW9Fr5CriR+urDosRIGTE4Vwbm1WjTXFdtvKn'
        'aD6BGVq/zLbiXPAl2Fz6PlXyaEe1dsbLqKPh++uCcnIJ/zI/MeqvQCcH91tLVlad'
        'EF1PMYiabQwsiOuPYl2m9jTjBQEEBgABCYGjAAcLAQABIwMEAQUGAAABAAyDkAAI'
        'CgGhgOjcAAAFARkEAAAAABEbAG0AYQBuAGkAZgBlAHMAdAAuAHgAbQBsAAAAGQAU'
        'CgEA2HlukuJX3QEVBgEAIAAAAAAA'
    ),
}


def self_test():
    """Assertions over the reader and the decision core. No network."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label, kind=ValueError, text=None):
        try:
            fn()
        except kind as exc:
            check(text is None or text in str(exc),
                  "%s (message %r)" % (label, str(exc)) if text and
                  text not in str(exc) else label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    print("sdcheck self-test: no ArcGIS, no 7-Zip, no network")
    print("-" * 68)
    tmp = tempfile.mkdtemp(prefix="sdcheck-")
    try:
        return _self_test_body(check, raises, tmp, passed, failed)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _self_test_body(check, raises, tmp, passed, failed):
    import zipfile

    def real(name):
        return base64.b64decode("".join(REAL_7Z[name]))

    def put(name, data):
        path = os.path.join(tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def arc(blob):
        return read_archive(io.BytesIO(blob))

    def member(blob, name):
        fh = io.BytesIO(blob)
        a = read_archive(fh)
        return read_member(fh, a, a.find(name))

    def verdict(dbs, expect=(ALIAS,), allow=False):
        return judge(parse_manifest(_fixture_manifest(dbs)), list(expect),
                     allow)

    def classes(dbs, **kw):
        return [r[1] for r in verdict(dbs, **kw)[1]]

    alias_db = ("assets", "true", _sde(ALIAS), "", ["roads"])
    raw_db = ("assets", "true", _sde(RAW), "", ["hydrants"])
    copy_db = ("demo.gdb", "false", "DATABASE=D:\\srv\\cd\\demo.gdb",
               "cd\\demo.gdb", ["parcels"])

    # ---- the story: why the old guard never gave a verdict
    print("-- the container")
    blob = real("pro_like")
    check(blob[:6] == MAGIC, "a staged .sd starts with the 7z signature")
    check(not zipfile.is_zipfile(io.BytesIO(blob)),
          "zipfile does not recognise a 7z archive, which is why a "
          "zipfile guard never answered  <-- pinned defect")
    raises(lambda: zipfile.ZipFile(io.BytesIO(blob)),
           "and opening it with zipfile raises BadZipFile",
           zipfile.BadZipFile)

    # ---- archives made by 7-Zip itself
    print("-- archives made by 7-Zip 24.08")
    a = arc(blob)
    names = [e.name for e in a.entries]
    check("manifest.xml" in names,
          "a non-solid LZMA archive with an encoded header lists manifest.xml")
    check("cd/demo.gdb/a00000001.gdbtable" in names,
          "member names use forward slashes whatever the archive stored")
    thumb = [e for e in a.entries if e.name == "esriinfo/thumbnail"]
    check(len(thumb) == 1 and thumb[0].is_dir,
          "an empty-stream member that is not an empty file is a directory")
    empty = [e for e in a.entries if e.name == "esriinfo/empty.txt"]
    check(len(empty) == 1 and not empty[0].is_dir and empty[0].size == 0,
          "an empty file is a file of size 0, not a directory")
    check(read_member(io.BytesIO(blob), a, empty[0]) == b"",
          "and reading it returns no bytes without touching a block")
    check(len(a.under("cd\\demo.gdb")) == 2,
          "the in-package path counts the 2 files copied under it")
    dbs = parse_manifest(member(blob, "manifest.xml"))
    check([d["name"] for d in dbs] == ["demo.gdb", "assets"],
          "the LZMA manifest decodes and parses to its two databases")
    check(dbs[0]["by_reference"] is False and dbs[1]["by_reference"] is True,
          "the ByReference flag reads false for the copy and true for the "
          "reference")
    solid = real("lzma2_solid")
    a2 = arc(solid)
    check([e.folder for e in a2.entries if not e.is_dir] == [0, 0],
          "a solid LZMA2 archive holds both files in one block")
    check(member(solid, "p30/service.mapx").startswith(b'{"type"'),
          "the second file in a solid block decodes at its offset")
    check(parse_manifest(member(solid, "manifest.xml"))[0]["name"]
          == "assets", "and so does the manifest before it")
    raises(lambda: member(real("bcj"), "manifest.xml"),
           "a BCJ plus LZMA chain is refused as unsupported", Unsupported,
           "chain of 2 coders")
    raises(lambda: arc(real("aes")),
           "an archive with an encrypted header says it is encrypted",
           Unsupported, "encrypted")
    raises(lambda: member(real("ppmd"), "manifest.xml"),
           "a PPMd member names the coder it cannot decode", Unsupported,
           "coder 030401")

    # ---- archives built here, for the branches 7-Zip never writes
    print("-- reader branches")
    files = [("manifest.xml", _fixture_manifest([alias_db])),
             ("esriinfo", None), ("esriinfo/blank.txt", b""),
             ("p30/a.mapx", b"mapx" * 40), ("p30/b.msd", b"msd" * 30)]
    for label, kw in (("copy coder, plain header", {}),
                      ("copy coder, encoded header", {"encode_header": True}),
                      ("solid copy block", {"solid": True}),
                      ("solid LZMA2 block, encoded header",
                       {"solid": True, "coder": "lzma2",
                        "encode_header": True}),
                      ("one LZMA2 block per file", {"coder": "lzma2"})):
        built = _build_7z(files, **kw)
        ok = all(member(built, n) == d for n, d in files if d)
        check(ok, "%s: every file reads back byte for byte" % label)
    plain = _build_7z(files)
    nums = [0, 0x7F, 0x80, 0x3FFF, 0x4000, 1 << 20, 1 << 35, (1 << 56) - 1,
            1 << 56, (1 << 64) - 1]
    buf = _Buf(b"".join(_num(n) for n in nums))
    check([buf.number() for _ in nums] == nums,
          "numbers of 1 to 9 bytes all decode to what was encoded")
    raises(lambda: _Buf(b"\x80").number(),
           "a number cut short by the end of the header raises",
           ArchiveError, "truncated")
    raises(lambda: _Buf(b"\x7f").count(),
           "a count of 127 items in 0 bytes is refused before allocating "
           "a list  <-- pinned defect", ArchiveError, "claims 127 items")
    check(_Buf(b"\xa0").bits(3) == [True, False, True],
          "bit vectors read most significant bit first")
    raises(lambda: _Buf(b"\x01").expect(K_END, "end"),
           "an unexpected property id is named in the error", ArchiveError,
           "expected end")

    bad = bytearray(plain)
    bad[32 + 5] ^= 0xFF
    raises(lambda: member(bytes(bad), "manifest.xml"),
           "a flipped byte in the manifest fails its CRC  <-- pinned defect",
           ArchiveError, "manifest.xml CRC does not match")
    bad = bytearray(plain)
    bad[-3] ^= 0xFF
    raises(lambda: arc(bytes(bad)), "a flipped header byte fails the "
           "header CRC", ArchiveError, "header CRC does not match")
    bad = bytearray(plain)
    bad[14] ^= 0xFF
    raises(lambda: arc(bytes(bad)), "a flipped start header byte fails its "
           "CRC", ArchiveError, "start header CRC")
    raises(lambda: arc(plain[:-10]), "an archive cut short before its "
           "header is refused", ArchiveError, "truncated")
    raises(lambda: arc(b"PK\x03\x04" + b"\x00" * 40), "a zip file is not "
           "a 7z archive", ArchiveError, "not a 7z archive")
    raises(lambda: arc(b"7z"), "a file shorter than the signature header is "
           "not a 7z archive", ArchiveError, "not a 7z archive")
    v1 = bytearray(plain)
    v1[6] = 1
    raises(lambda: arc(bytes(v1)), "a 7z major version other than 0 is "
           "unsupported", Unsupported, "version 1.4")

    def start_header(offset, size, crc, body=b""):
        start = struct.pack("<QQI", offset, size, crc)
        return (MAGIC + b"\x00\x04"
                + struct.pack("<I", zlib.crc32(start) & 0xFFFFFFFF)
                + start + body)

    check(arc(start_header(0, 0, 0)).entries == [],
          "an archive with no header has no members")
    raises(lambda: arc(start_header(0, MAX_HEADER_BYTES + 1, 0)),
           "a header larger than the limit is refused before it is read",
           ArchiveError, "over the")

    def with_header(header, body=b""):
        return start_header(len(body), len(header),
                            zlib.crc32(header) & 0xFFFFFFFF, body + header)

    raises(lambda: arc(with_header(b"\x09")), "an unknown header type is "
           "refused", ArchiveError, "unknown header type 0x09")
    raises(lambda: arc(with_header(bytes([K_ENCODED_HEADER, K_END]))),
           "an encoded header with no folder is refused", ArchiveError,
           "no folder")
    big = (bytes([K_ENCODED_HEADER, K_PACK_INFO, 0, 1, K_SIZE, 1, K_END,
                  K_UNPACK_INFO, K_FOLDER, 1, 0, 1, 1, 0,
                  K_CODERS_UNPACK_SIZE]) + _num(MAX_HEADER_BYTES + 1)
           + bytes([K_END, K_END]))
    raises(lambda: arc(with_header(big)), "an encoded header that unpacks "
           "past the limit is refused", ArchiveError, "unpacks to")
    loop = bytes([K_ENCODED_HEADER, K_PACK_INFO, 0, 1, K_SIZE, 18, K_END,
                  K_UNPACK_INFO, K_FOLDER, 1, 0, 1, 1, 0,
                  K_CODERS_UNPACK_SIZE, 18, K_END, K_END])
    check(len(loop) == 18, "the self-encoding header is 18 bytes long")
    raises(lambda: arc(with_header(loop, loop)), "a header that encodes "
           "itself forever stops after four rounds  <-- pinned defect",
           ArchiveError, "more than four times")
    crcbad = (bytes([K_ENCODED_HEADER, K_PACK_INFO, 0, 1, K_SIZE, 1, K_END,
                     K_UNPACK_INFO, K_FOLDER, 1, 0, 1, 1, 0,
                     K_CODERS_UNPACK_SIZE, 1, K_CRC, 1])
              + struct.pack("<I", 0) + bytes([K_END, K_END]))
    raises(lambda: arc(with_header(crcbad, b"\x01")), "an encoded header "
           "that fails its CRC is refused", ArchiveError,
           "encoded header CRC")

    def header(*parts):
        return _header(_Buf(bytes(parts)))

    check(header(K_ARCHIVE_PROPERTIES, 7, 1, 0xAA, 0, K_END).entries == [],
          "archive properties are skipped")
    raises(lambda: header(K_ADDITIONAL_STREAMS), "additional header streams "
           "are unsupported", Unsupported, "additional")
    raises(lambda: header(K_SIZE), "a header that does not end is refused",
           ArchiveError, "header does not end")
    raises(lambda: header(K_FILES_INFO, 1, K_END, K_END), "a file list "
           "without names is refused", ArchiveError, "names missing")
    raises(lambda: header(K_FILES_INFO, 1, K_NAME, 1, 1, K_END, K_END),
           "names stored outside the header are unsupported", Unsupported,
           "outside the header")
    raises(lambda: header(K_FILES_INFO, 1, K_NAME, 5, 0, 0x61, 0, 0, 0,
                          K_END, K_END),
           "a member with data but no stream is refused", ArchiveError,
           "does not match stream count")

    def streams(*parts):
        return _streams(_Buf(bytes(parts)))

    st = streams(K_PACK_INFO, 0, 2, K_SIZE, 5, 6, K_CRC, 0, 0x80,
                 1, 2, 3, 4, K_END, K_END)
    check(st.pack_sizes == [5, 6], "pack sizes read with a partial CRC list")
    check(streams(K_PACK_INFO, 0, 0, K_END, K_END).pack_sizes == [],
          "pack info with no sizes and no CRCs is allowed")
    raises(lambda: streams(K_PACK_INFO, 0, 1, K_SIZE, 5, K_FOLDER),
           "pack info that does not end is refused", ArchiveError,
           "pack info does not end")
    raises(lambda: streams(K_UNPACK_INFO, K_FOLDER, 1, 1),
           "folders stored outside the header are unsupported", Unsupported,
           "outside the header")
    raises(lambda: streams(K_UNPACK_INFO, K_CODERS_UNPACK_SIZE),
           "unpack info without a folder list is refused", ArchiveError,
           "expected folder list")
    raises(lambda: streams(K_UNPACK_INFO, K_FOLDER, 1, 0, 1, 1, 0,
                           K_CODERS_UNPACK_SIZE, 5, K_FOLDER),
           "unpack info that does not end is refused", ArchiveError,
           "unpack info does not end")
    raises(lambda: streams(K_FOLDER), "streams info that does not end is "
           "refused", ArchiveError, "streams info does not end")

    raises(lambda: _folder(_Buf(bytes([1, 0x81, 0]))), "alternative coder "
           "methods are unsupported", Unsupported, "alternative")
    chain = _folder(_Buf(bytes([2, 0x14, 3, 3, 1, 0x1B, 4, 1,
                                0x23, 3, 1, 1, 5, 0x5D, 0, 0, 1, 0,
                                0, 1, 1, 2, 3, 4])))
    chain.unpack_sizes = [100, 200]
    check(chain.size() == 100 and chain.bound_outs == set([1]),
          "a four-input chain reads its bind pair and packed streams, and "
          "its size is the unbound output")
    check(Folder([], set()).size() == 0, "a folder with no coders has size 0")

    def subs(sizes_crcs, *parts):
        s = Streams()
        for size, crc in sizes_crcs:
            f = Folder([(CODER_COPY, b"", 1, 1)], set())
            f.unpack_sizes = [size]
            f.crc = crc
            s.folders.append(f)
        _substreams(_Buf(bytes(parts)), s)
        return s.substreams

    check(subs([(10, None)], K_NUM_UNPACK_STREAM, 0, K_END) == [[]],
          "a folder of zero streams holds no files")
    check(subs([(10, None)], K_NUM_UNPACK_STREAM, 1, K_END)
          == [[(10, None)]], "one stream without sizes takes the folder size")
    check(subs([(10, 77)], K_END) == [[(10, 77)]],
          "one stream with a folder CRC keeps that CRC")
    check(subs([(10, None)], K_NUM_UNPACK_STREAM, 2, K_SIZE, 4, K_CRC, 1,
               1, 0, 0, 0, 2, 0, 0, 0, K_END) == [[(4, 1), (6, 2)]],
          "two streams split the folder and take their own CRCs")
    raises(lambda: subs([(10, None)], K_NUM_UNPACK_STREAM, 2, K_END),
           "two streams with no sizes are refused", ArchiveError, "no sizes")
    raises(lambda: subs([(10, None)], K_NUM_UNPACK_STREAM, 2, K_SIZE, 11,
                        K_END),
           "stream sizes larger than their folder are refused", ArchiveError,
           "exceed")
    raises(lambda: subs([(10, None)], K_FOLDER), "substreams info that "
           "does not end is refused", ArchiveError, "substreams info")

    check(_coder_filter(CODER_COPY, b"") is None, "the copy coder needs no "
          "filter")
    f = _coder_filter(CODER_LZMA, bytes([0x5D, 0, 0, 16, 0]))
    check((f["lc"], f["lp"], f["pb"], f["dict_size"]) == (3, 0, 2, 1 << 20),
          "the LZMA properties byte 0x5D decodes to lc 3, lp 0, pb 2")
    raises(lambda: _coder_filter(CODER_LZMA, b"\x5d"), "LZMA properties of "
           "the wrong length are refused", ArchiveError, "not 5")
    raises(lambda: _coder_filter(CODER_LZMA, bytes([225, 0, 0, 0, 0])),
           "an LZMA properties byte past 224 is refused", ArchiveError,
           "out of range")
    check(_coder_filter(CODER_LZMA2, b"\x28")["dict_size"] == 0xFFFFFFFF,
          "the LZMA2 dictionary byte 40 means the largest dictionary")
    check(_coder_filter(CODER_LZMA2, b"\x11")["dict_size"] == 3 << 19,
          "an odd LZMA2 dictionary byte gives three times a power of two")
    raises(lambda: _coder_filter(CODER_LZMA2, b"\x29"), "an LZMA2 "
           "dictionary byte past 40 is refused", ArchiveError, "LZMA2")
    raises(lambda: _coder_filter(b"", b""), "a coder with an empty id is "
           "named as empty", Unsupported, "(empty)")

    def folder_streams(codec, pack, unpack):
        s = Streams()
        f = Folder([codec], set())
        f.unpack_sizes = [unpack]
        s.folders.append(f)
        s.pack_sizes = [pack]
        return s

    lz2 = (CODER_LZMA2, b"\x10", 1, 1)
    data = lzma.compress(b"0123456789", format=lzma.FORMAT_RAW,
                         filters=[{"id": lzma.FILTER_LZMA2}])
    raises(lambda: _unpack(io.BytesIO(b"\x00" * 32 + data),
                           folder_streams(lz2, len(data), 20), 0, 20),
           "a block that ends before its stated size is refused",
           ArchiveError, "ends after 10 of 20")
    raises(lambda: _unpack(io.BytesIO(b"\x00" * 32 + data[:4]),
                           folder_streams(lz2, 4, 10), 0, 10),
           "a block cut short inside the stream is refused", ArchiveError,
           "ends after")
    raises(lambda: _unpack(io.BytesIO(b"\x00" * 32 + data[:4]),
                           folder_streams(lz2, 40, 10), 0, 10),
           "a block that claims more bytes than the file has is refused",
           ArchiveError, "ends after")
    raises(lambda: _unpack(io.BytesIO(b"\x00" * 32 + b"\xff" * 64),
                           folder_streams(lz2, 64, 10), 0, 10),
           "corrupt LZMA2 data is an archive error, not a crash",
           ArchiveError, "corrupt")
    raises(lambda: _unpack(io.BytesIO(b"\x00" * 40),
                           folder_streams((CODER_COPY, b"", 1, 1), 4, 8), 0,
                           8),
           "a stored block shorter than its size is refused", ArchiveError,
           "ends after 4 of 8")
    nopack = folder_streams((CODER_COPY, b"", 1, 1), 4, 4)
    nopack.pack_sizes = []
    raises(lambda: _unpack(io.BytesIO(b""), nopack, 0, 4), "a folder with "
           "no packed stream is refused", ArchiveError, "no packed stream")
    a = arc(plain)
    raises(lambda: read_member(io.BytesIO(plain), a,
                               Entry("huge", MAX_MANIFEST_BYTES + 1, 0, 0,
                                     None, False)),
           "a manifest past the size limit is refused before it is "
           "decompressed  <-- pinned defect", Unsupported, "over the")
    check(arc(_build_7z([("sub/manifest.xml", b"x")])).find(MANIFEST_NAME)
          is None, "a manifest.xml below the root is not the service "
          "manifest  <-- pinned defect")
    check(arc(_build_7z([("manifest.xml", None)])).find(MANIFEST_NAME)
          is None, "a directory named manifest.xml is not a manifest")

    # ---- the manifest
    print("-- the manifest")
    raises(lambda: parse_manifest(b"<!DOCTYPE x [<!ENTITY a 'aaaa'>]>"
                                  b"<SVCManifest>&a;</SVCManifest>"),
           "a manifest that declares an entity is refused before parsing  "
           "<-- pinned defect", ManifestError, "DTD or entity")
    raises(lambda: parse_manifest(u"<!DOCTYPE x><SVCManifest/>"
                                  .encode("utf-16")),
           "and so is a UTF-16 one", ManifestError, "DTD or entity")
    raises(lambda: parse_manifest(b"<SVCManifest>"), "a manifest that is "
           "not well-formed is refused", ManifestError, "not well-formed")
    raises(lambda: parse_manifest(b"<Other/>"), "a root other than "
           "SVCManifest is refused", ManifestError, "not SVCManifest")
    utf16 = _fixture_manifest([alias_db]).decode("ascii").encode("utf-16")
    check(parse_manifest(utf16)[0]["name"] == "assets",
          "a UTF-16 manifest with a byte order mark parses")
    bare = parse_manifest(b"<SVCManifest><Databases><SVCDatabase>"
                          b"<Datasets><SVCDataset/></Datasets>"
                          b"</SVCDatabase></Databases></SVCManifest>")[0]
    check(bare["name"] == "(unnamed)" and bare["datasets"] == ["(unnamed)"],
          "a database and dataset with no name are reported as unnamed")
    check(bare["by_reference"] is None and bare["connection"] == "",
          "and a missing ByReference is neither true nor false")

    # ---- hosts
    print("-- hosts")
    check(hosts_in(_sde(ALIAS)) == [("SERVER", ALIAS), ("INSTANCE", ALIAS),
                                    ("DB_CONNECTION_PROPERTIES", ALIAS)],
          "an SDE connection names its host three times")
    check(hosts_in("SERVER=db1\\inst;INSTANCE=sde:sqlserver:db1,1433")
          == [("SERVER", "db1"), ("INSTANCE", "db1")],
          "a named instance and a port are cut from the host")
    check(hosts_in("INSTANCE=sde:oracle19c:ora1:1521/svc")
          == [("INSTANCE", "ora1")],
          "an Oracle easy-connect instance gives its host")
    check(hosts_in("INSTANCE=sde:postgresql:pg1") == [("INSTANCE", "pg1")],
          "a PostgreSQL instance gives its host")
    check(hosts_in("INSTANCE=5151;SERVER=;DBCLIENT=x;novalue") == [],
          "a bare port, an empty value and a key with no value name no host")
    check(hosts_in("DATABASE=\\\\fs1\\gis\\data.gdb") == [("DATABASE", "fs1")],
          "a UNC path to a file geodatabase names its file server")
    check(hosts_in("DATABASE=C:\\data\\x.gdb") == [],
          "a local path names no host")
    check(hosts_in("SERVER=,1433") == [], "a value that is only a port "
          "names no host")

    # ---- verdicts
    print("-- verdicts")
    code, rows = verdict([alias_db])
    check(code == EXIT_PASS and rows[0][1] == OK,
          "a reference through the expected alias passes")
    check(RAW not in rows[0][2] and "author-pc" not in rows[0][2],
          "the on-premise connection is never read  <-- pinned defect")
    code, rows = verdict([raw_db])
    check(code == EXIT_FLAGGED and rows[0][1] == RAW_HOST,
          "a reference through a raw host is flagged  <-- pinned defect")
    check("hydrants" in rows[0][2], "and the flag names the datasets")
    check(verdict([alias_db], expect=(ALIAS.upper(),))[0] == EXIT_PASS,
          "host names compare without case")
    mixed = ("assets", "true", "SERVER=%s;INSTANCE=sde:sqlserver:%s;"
             "DB_CONNECTION_PROPERTIES=%s" % (ALIAS, ALIAS, RAW), "", ["x"])
    code, rows = verdict([mixed])
    check(code == EXIT_FLAGGED and "DB_CONNECTION_PROPERTIES=" + RAW
          in rows[0][2], "a SERVER through the alias does not hide a raw "
          "host in another key  <-- pinned defect")
    unc = ("share", "true", "DATABASE=\\\\fs1\\gis\\a.gdb", "", ["x"])
    check(classes([unc]) == [RAW_HOST],
          "a file geodatabase on a raw file server is flagged")
    local = ("local", "true", "DATABASE=C:\\data\\a.gdb", "", [])
    code, rows = verdict([local])
    check(code == EXIT_PASS and "names no host" in rows[0][2],
          "a referenced local path passes and says it names no host")
    code, rows = verdict([copy_db])
    check(code == EXIT_FLAGGED and rows[0][1] == COPIED,
          "a copied dataset is flagged  <-- pinned defect")
    check("file count unknown" in rows[0][2] and "parcels" in rows[0][2],
          "and without an archive listing the file count is unknown")
    code, rows = verdict([copy_db], allow=True)
    check(code == EXIT_PASS and rows[0][1] == COPY_ALLOWED,
          "--allow-copied passes a copy and still reports it")
    nopath = ("demo.gdb", "false", "", "", [])
    check("(no in-package path)" in verdict([nopath])[1][0][2]
          and "(none listed)" in verdict([nopath])[1][0][2],
          "a copy with no in-package path and no datasets says so")
    check(classes([("x", None, _sde(ALIAS), "", [])]) == [UNKNOWN],
          "a database without ByReference gets no verdict  <-- pinned defect")
    check(classes([("x", "yes", _sde(ALIAS), "", [])]) == [UNKNOWN],
          "a ByReference that is not true or false gets no verdict")
    code, rows = verdict([("x", "true", None, "", [])])
    check(code == EXIT_NO_VERDICT and "no server connection" in rows[0][2],
          "a reference with no server connection is no verdict, never a "
          "pass  <-- pinned defect")
    code, rows = verdict([])
    check(code == EXIT_NO_VERDICT and rows[0][1] == UNKNOWN,
          "a manifest with no database is no verdict, never a pass  "
          "<-- pinned defect")
    check(verdict([raw_db, ("x", None, "", "", [])])[0] == EXIT_FLAGGED,
          "a flag outranks a missing verdict in the same manifest")
    check(judge(parse_manifest(_fixture_manifest([copy_db])), [ALIAS], False,
                {"demo.gdb": 3})[1][0][2].count("3 file(s)") == 1,
          "a copy reports the files the archive holds under its path")
    check(combine([0, 2]) == 2 and combine([2, 1, 0]) == 1
          and combine([0]) == 0,
          "across files a flag outranks no verdict, which outranks a pass")

    # ---- whole files
    print("-- whole files")
    pro = put("pro_like.sd", blob)
    r = check_file(pro, [ALIAS], False)
    check(r["exit"] == EXIT_FLAGGED and [x[1] for x in r["rows"]]
          == [COPIED, OK], "the 7-Zip archive is flagged for its copy and "
          "passes its reference")
    check("2 file(s)" in r["rows"][0][2],
          "the copy counts the files it found in the archive")
    check(all(RAW not in line for line in describe(r)),
          "a raw host inside copied data is never reported, because data "
          "files are never scanned  <-- pinned defect")
    check(check_file(pro, [ALIAS], True)["exit"] == EXIT_PASS,
          "with --allow-copied the same archive passes")
    out = "\n".join(describe(check_file(put("solid.sd", solid), [ALIAS],
                                        False)))
    check(SECRET not in out and "ENCRYPTED_PASSWORD" not in out,
          "the encrypted password in the connection never reaches the "
          "output  <-- pinned defect")
    check("RAW HOST" in out and "SERVER=" + RAW in out,
          "the output names the raw host and the key that holds it")
    r = check_file(os.path.join(tmp, "missing.sd"), [ALIAS], False)
    check(r["exit"] == EXIT_NO_VERDICT and r["rows"][0][1] == UNKNOWN,
          "a file that does not exist is no verdict")
    r = check_file(put("sub.sd", _build_7z([("sub/manifest.xml", b"x")])),
                   [ALIAS], False)
    check(r["exit"] == EXIT_NO_VERDICT and "no manifest.xml" in
          r["rows"][0][2], "an archive without a root manifest is no verdict")
    bcj = put("bcj.sd", real("bcj"))
    r = check_file(bcj, [ALIAS], False)
    check(r["exit"] == EXIT_NO_VERDICT and "--sevenzip" in r["rows"][0][2],
          "an unsupported archive is no verdict and names --sevenzip")

    stub = put("stub7z.py", (
        "import sys\n"
        "log = open(sys.argv[0] + '.log', 'w')\n"
        "log.write(repr(sys.argv[1:]) + '|' + repr(sys.stdin.read()))\n"
        "log.close()\n"
        "if sys.argv[-2].endswith('fail.sd'):\n"
        "    sys.exit(2)\n"
        "data = open(sys.argv[-2].replace('.sd', '.xml'), 'rb').read()\n"
        "getattr(sys.stdout, 'buffer', sys.stdout).write(data)\n"
    ).encode("ascii"))
    put("bcj.xml", _fixture_manifest([alias_db]))
    r = check_file(bcj, [ALIAS], False, [sys.executable, stub])
    check(r["exit"] == EXIT_PASS and r["reader"].startswith("sevenzip"),
          "--sevenzip reads what the stdlib reader refuses")
    with open(stub + ".log") as fh:
        logged = fh.read()
    check(logged == repr(["e", "-so", "-y", bcj, MANIFEST_NAME]) + "|''",
          "7-Zip is asked for the root manifest only, without -r, with "
          "stdin closed  <-- pinned defect")
    check(any("read with sevenzip" in x for x in describe(r)),
          "the output says which reader produced the verdict")
    fail = put("fail.sd", real("bcj"))
    r = check_file(fail, [ALIAS], False, [sys.executable, stub])
    check(r["exit"] == EXIT_NO_VERDICT and "exited 2" in r["rows"][0][2],
          "a 7-Zip that fails is no verdict, never a pass")
    r = check_file(bcj, [ALIAS], False, [os.path.join(tmp, "no-such-7z")])
    check(r["exit"] == EXIT_NO_VERDICT and "could not run" in
          r["rows"][0][2], "a 7-Zip that is not there is no verdict")

    # ---- command line
    print("-- command line")

    def run(argv):
        out, err = io.StringIO(), io.StringIO()
        saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = main(argv)
        finally:
            sys.stdout, sys.stderr = saved
        return code, out.getvalue(), err.getvalue()

    base = [pro, "--expect-host", ALIAS]
    code, out, _ = run(base)
    check(code == EXIT_FLAGGED and "FLAGGED" in out,
          "the command line exits 1 on a flagged file")
    check("1 file(s): 0 pass, 1 flagged, 0 no verdict" in out,
          "and prints one summary line")
    check(run(base + ["--allow-copied"])[0] == EXIT_PASS,
          "--allow-copied exits 0 on the same file")
    check(run([put("solid4.sd", solid), "--expect-host", ALIAS,
               "--expect-host", RAW])[0] == EXIT_PASS,
          "--expect-host repeats, and every host it names is accepted")
    code, out, _ = run([pro, put("solid2.sd", solid), "--expect-host", ALIAS,
                        "--allow-copied"])
    check(code == EXIT_FLAGGED and "2 file(s): 1 pass, 1 flagged" in out,
          "two files give one exit code and one summary")
    check(run([os.path.join(tmp, "nope.sd"), "--expect-host", ALIAS])[0]
          == EXIT_NO_VERDICT, "a missing file exits 2")
    code, out, _ = run([bcj, "--expect-host", ALIAS, "--sevenzip",
                        os.path.join(tmp, "no-such-7z")])
    check(code == EXIT_NO_VERDICT and "could not run" in out,
          "--sevenzip reaches the fallback from the command line")
    code, _, err = run([pro])
    check(code == EXIT_USAGE and "--expect-host" in err,
          "a run without --expect-host is a usage error")
    check(run(["--expect-host", ALIAS])[0] == EXIT_USAGE,
          "a run without a file is a usage error")
    for flag in ("--ap", "--allow", "--expect", "--self"):
        code, _, err = run(base + [flag])
        check(code == EXIT_USAGE and "unrecognized arguments: %s" % flag
              in err, "a prefix %s is refused, never expanded  "
              "<-- pinned defect" % flag)
    target = os.path.join(tmp, "verdict.json")
    code, out, _ = run(base + ["--out", target])
    check(not os.path.exists(target) and "not written" in out,
          "--out without --apply writes nothing at all  <-- pinned defect")
    check(code == EXIT_FLAGGED, "and the exit code is the verdict's")
    check(run(base + ["--apply"])[0] == EXIT_USAGE,
          "--apply without --out is a usage error")
    code, out, _ = run([put("solid3.sd", solid), "--expect-host", ALIAS,
                        "--out", target, "--apply"])
    with open(target) as fh:
        written = fh.read()
    doc = json.loads(written)
    check(code == EXIT_FLAGGED and doc[0]["verdict"] == "FLAGGED"
          and doc[0]["rows"][0]["class"] == RAW_HOST,
          "--out with --apply writes the verdict as JSON")
    check(SECRET not in written, "and the JSON holds no password  "
          "<-- pinned defect")
    code, _, err = run(base + ["--out", tmp, "--apply"])
    check(code == EXIT_NO_VERDICT and "could not write" in err,
          "an --out that cannot be written exits 2 and says so")
    saved = sys.argv
    sys.argv = ["sdcheck.py", pro, "--expect-host", ALIAS]
    try:
        check(run(None)[0] == EXIT_FLAGGED,
              "main with no argv reads the arguments after the program name")
    finally:
        sys.argv = saved

    # ---- the harness itself
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded")
        raises(lambda: None, "probe: no exception must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
        raises(lambda: _Buf(b"").byte(), "probe: the wrong message must fail",
               ArchiveError, "not this text")
    finally:
        sys.stdout = quiet
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 4, "check() and raises() really do record a failure "
          " <-- pinned defect")

    # ---- the file imports as a module without running the command line
    import runpy
    mod = runpy.run_path(__file__, run_name="sdcheck_imported")
    check(mod["MAGIC"] == MAGIC and "main" in mod,
          "the file imports as a module and runs nothing on import")

    print("-" * 68)
    total = passed[0] + len(failed)
    print("%d assertions, %d failed" % (total, len(failed)))
    print("".join("  FAILED: %s\n" % label for label in failed), end="")
    return 1 if failed else 0


# =============================================================================
# Command line
# =============================================================================

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="sdcheck.py",
        description="Read a staged ArcGIS service definition (.sd) and flag "
                    "a raw host or a copied dataset.",
        epilog="Nothing is written without --apply.",
        allow_abbrev=False,
    )
    ap.add_argument("files", nargs="*", metavar="SD",
                    help="staged .sd files to check")
    ap.add_argument("--expect-host", dest="expect_host", action="append",
                    default=[], metavar="HOST",
                    help="a host a live connection may name, usually the "
                         "database alias. Repeat for more than one.")
    ap.add_argument("--allow-copied", dest="allow_copied",
                    action="store_true",
                    help="report data copied into the .sd, but do not flag it")
    ap.add_argument("--sevenzip", metavar="PATH",
                    help="7-Zip program to read archives the built-in reader "
                         "refuses (an encrypted or BCJ or PPMd archive)")
    ap.add_argument("--out", metavar="FILE",
                    help="write the verdicts as JSON. Needs --apply.")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        # argparse exits 2 on a flag it refuses, and 2 here means no verdict.
        # A mistyped --ap is a usage error, so it gets 64.
        return EXIT_USAGE if exc.code == 2 else exc.code
    if args.self_test:
        return self_test()
    if not args.files:
        print("error: name at least one .sd file, or use --self-test.",
              file=sys.stderr)
        return EXIT_USAGE
    if not args.expect_host:
        print("error: --expect-host is required: name the host or alias a "
              "live connection may use.", file=sys.stderr)
        return EXIT_USAGE
    if args.apply and not args.out:
        print("error: --apply needs --out.", file=sys.stderr)
        return EXIT_USAGE

    sevenzip = [args.sevenzip] if args.sevenzip else None
    results = [check_file(p, args.expect_host, args.allow_copied, sevenzip)
               for p in args.files]
    for r in results:
        for line in describe(r):
            print(line)
    codes = [r["exit"] for r in results]
    print("%d file(s): %d pass, %d flagged, %d no verdict"
          % (len(codes), codes.count(EXIT_PASS), codes.count(EXIT_FLAGGED),
             codes.count(EXIT_NO_VERDICT)))
    code = combine(codes)
    if args.out and not args.apply:
        print("%s not written: pass --apply to write it." % args.out)
    elif args.out:
        doc = [{"path": r["path"], "verdict": VERDICT_WORD[r["exit"]],
                "exit": r["exit"], "reader": r["reader"],
                "rows": [{"database": n, "class": c, "reason": why}
                         for n, c, why in r["rows"]]} for r in results]
        try:
            with open(args.out, "w") as fh:
                json.dump(doc, fh, indent=2)
        except (IOError, OSError) as exc:
            print("error: could not write %s: %s" % (args.out, exc),
                  file=sys.stderr)
            return EXIT_NO_VERDICT
        print("wrote %s" % args.out)
    return code


if __name__ == "__main__":
    sys.exit(main())
