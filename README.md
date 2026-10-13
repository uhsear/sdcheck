# sdcheck

Read a staged ArcGIS service definition (`.sd`) and flag a raw host or a copied dataset, before
the file is published.

A server migration depends on one rule. Every service reads its database through an alias, so
that a single DNS change moves every service to the new database server. A publishing script
staged each service, then ran a guard over the `.sd` to prove that the data connection used the
alias. The guard opened the `.sd` with Python's `zipfile`.

A staged `.sd` is not a zip file. It is a 7-Zip archive. `zipfile` raised `BadZipFile` on every
file, so the guard never found a connection string. A missing verdict did not stop the publish.
The check looked like it ran, but it never gave a verdict, for any service.

Two failures pass through a guard like that. A service staged against the raw database host
keeps pointing at the old server after the alias moves. A service whose data was copied into the
`.sd` reads a frozen copy on the server and never sees an edit again. Neither failure raises an
error at publish time. Each one surfaces later, far from the publish that caused it.

`sdcheck` opens the 7-Zip archive with the standard library, reads `manifest.xml`, and gives
each file one of three verdicts: PASS, FLAGGED or NO VERDICT. A NO VERDICT is never a pass.

```
$ python sdcheck.py --self-test
sdcheck self-test: no ArcGIS, no 7-Zip, no network
--------------------------------------------------------------------
-- the container
PASS  a staged .sd starts with the 7z signature
PASS  zipfile does not recognise a 7z archive, which is why a zipfile guard never answered  <-- pinned defect
PASS  and opening it with zipfile raises BadZipFile
-- archives made by 7-Zip 24.08
PASS  a non-solid LZMA archive with an encoded header lists manifest.xml
...
-- verdicts
PASS  a reference through the expected alias passes
PASS  the on-premise connection is never read  <-- pinned defect
PASS  a reference through a raw host is flagged  <-- pinned defect
PASS  and the flag names the datasets
PASS  host names compare without case
PASS  a SERVER through the alias does not hide a raw host in another key  <-- pinned defect
PASS  an Oracle connection through a raw host is flagged, not passed  <-- pinned defect
...
PASS  a copied dataset is flagged  <-- pinned defect
...
PASS  a reference with no server connection is no verdict, never a pass  <-- pinned defect
PASS  a manifest with no database is no verdict, never a pass  <-- pinned defect
...
PASS  a raw host inside copied data is never reported, because data files are never scanned  <-- pinned defect
PASS  with --allow-copied the same archive passes
PASS  the encrypted password in the connection never reaches the output  <-- pinned defect
...
PASS  a prefix --ap is refused, never expanded  <-- pinned defect
...
PASS  --out without --apply writes nothing at all  <-- pinned defect
PASS  a name outside the console code page is escaped, so the run still exits 0 with its summary  <-- pinned defect
...
--------------------------------------------------------------------
177 assertions, 0 failed
```

The full run prints all 177 assertions. Each `...` line above marks a cut.

## Requirements

Python 3.9 or newer and nothing else. The tool needs no `arcpy`, no third-party package, no
7-Zip and no network. The 7z reader uses `struct`, `zlib` and `lzma` from the standard library.

The self-test prints 177 assertions, 0 failed, on each of these interpreters:

| Host | Python | Result |
|---|---|---|
| Windows 11 | 3.13.2 | 177 assertions, 0 failed |
| Ubuntu 24.04 | 3.12.3 | 177 assertions, 0 failed |
| Windows 11 | 3.9.25 | 177 assertions, 0 failed |

Branch coverage of `sdcheck.py` under `coverage run --branch sdcheck.py --self-test` is 100% on
Windows Python 3.13 (1058 statements, 300 branches, none missed). `coverage` is not installed on
the Ubuntu host, so coverage there was not measured.

```
git clone https://github.com/uhsear/sdcheck.git
```

## Usage

Name the host or alias that a live connection may use. Check before you publish.

```
python sdcheck.py --self-test
python sdcheck.py staged.sd --expect-host gisdb.example.org
python sdcheck.py a.sd b.sd --expect-host gisdb.example.org --expect-host gisdb2.example.org
python sdcheck.py staged.sd --expect-host gisdb.example.org --allow-copied
python sdcheck.py staged.sd --expect-host gisdb.example.org --sevenzip "C:\Program Files\7-Zip\7z.exe"
python sdcheck.py staged.sd --expect-host gisdb.example.org --out verdict.json --apply
```

| Flag | Default | What it does |
|---|---|---|
| `SD` | none | One or more staged `.sd` files. Required unless `--self-test`. |
| `--expect-host` | none | A host a live connection may name. Repeat it for each alias. Required. |
| `--allow-copied` | off | Report data copied into the `.sd` as COPY ALLOWED, and do not flag it. |
| `--sevenzip` | off | A 7-Zip program to read an archive that the built-in reader refuses. |
| `--out` | off | Write the verdicts as JSON to this file. Needs `--apply`. |
| `--apply` | off | Write `--out`. Without it, nothing is written. |
| `--self-test` | off | Run the offline assertions and exit. |

Every parser flag must be spelled in full. A prefix such as `--ap` or `--allow` is refused with
exit 64, and the self-test pins that refusal.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Every file passed. |
| 1 | At least one file was flagged. |
| 2 | No file was flagged, but at least one got no verdict. An `--out` that could not be written also exits 2. |
| 64 | Usage error. |

A flag outranks a missing verdict, in one manifest and across files. A scheduled job can gate
on any non-zero exit.

## What it checks

The tool reads one member, `manifest.xml` at the archive root, and nothing else. For each
`SVCDatabase` in the manifest it gives one class:

| Class | When | Verdict |
|---|---|---|
| `OK` | `ByReference` is true and every host in the server connection is expected. | PASS |
| `RAW HOST` | `ByReference` is true and the server connection names any other host. | FLAGGED |
| `COPIED` | `ByReference` is false: the data travels inside the `.sd`. | FLAGGED |
| `COPY ALLOWED` | The same, with `--allow-copied`. | PASS |
| `NO VERDICT` | `ByReference` is missing, the reference has no server connection, or the connection has a part the tool cannot read (see **Which keys**). | NO VERDICT |

A manifest with no database at all is NO VERDICT. A manifest that cannot be read is NO VERDICT.
So is a file that raises an unexpected error: that file gets NO VERDICT, and the run goes on to
the next file. A file or dataset name outside the console code page prints as `\uXXXX` escapes,
so a redirected run never fails on a name.

**Which connection.** Esri's Administrator API reference for the service manifest defines
`onServerConnectionString` as the "Path to data location after publishing completes", and
`onPremiseConnectionString` as the "Path to publisher data location". The tool reads the
server string only. The publisher string records the machine that staged the file, and the
self-test pins that a raw host there is never reported.

**Which keys.** In the server string the tool reads the host from four keys:

| Key | Host taken from |
|---|---|
| `SERVER` | The value, without a `\instance`, `,port` or `:port` part. |
| `INSTANCE` | The part after `sde:<dbms>:`, cut the same way. A bare port such as `5151` names no host. |
| `DB_CONNECTION_PROPERTIES` | The value, cut the same way. |
| `DATABASE` | The server of a UNC path such as `\\fs1\gis\data.gdb`. A local path names no host. |

The tool also fails safe on keys it does not read. These keys carry no host and are allowed:
`AUTHENTICATION_MODE`, `BRANCH`, `DBCLIENT`, `ENCRYPTED_PASSWORD`, `ENCRYPTED_PASSWORD_UTF8`,
`HISTORICAL_NAME`, `HISTORICAL_TIMESTAMP`, `IS_GEODATABASE`, `USER` and `VERSION`. Any other key
(for example `HOSTNAME=` or `DATASOURCE=`), a part with no `=`, and a host key whose value gives no
host (for example `INSTANCE=sde:sqlserver`) make the database NO VERDICT, because a host could
hide there. A connection that holds no host and no `DATABASE` path is also NO VERDICT. A
raw host elsewhere in the same string is still FLAGGED. The reason names the key, never the value.
An Oracle-style `//host:1521/service` value gives its host, and a `DATABASE` UNC path is read with
`\` or `/` slashes, or in the `\\?\UNC\` form.

The old guard read `SERVER` only. A connection with `SERVER` set to the alias and
`DB_CONNECTION_PROPERTIES` set to the raw host passed it. Here, that connection is flagged.

**What it never prints.** The server string also holds `ENCRYPTED_PASSWORD`. The output and
the JSON carry host names only. The self-test greps both for the password and fails if it
appears.

**What it never scans.** The tool does not search the whole archive for host names. Copied
file geodatabase tables can hold old host strings as residual bytes. A test archive carries a
raw host inside a copied table, and the self-test pins that the tool does not report it.

## Evidence from ArcGIS Pro

The test archives in the self-test are synthetic. Before the reader was written, ArcGIS Pro
3.6 staged four real `.sd` files on this workstation for a cross-check. Each came from a map
with one synthetic point layer, through `arcpy.sharing.CreateSharingDraft("STANDALONE_SERVER",
"MAP_SERVICE", ...)` with `offline = True`, then `arcpy.server.StageService`. The database was
an empty SQL Server 2022 container. These files are not in the repository.

| File | How it was staged | Verdict with `--expect-host localhost` |
|---|---|---|
| `copied.sd` | File geodatabase, `copyDataToServer = True` | FLAGGED: `COPIED`, 59 file(s) in the `.sd` |
| `byref.sd` | SQL Server reached as `localhost,14352` | PASS: referenced through localhost |
| `byip.sd` | The same server reached as `127.0.0.1,14352` | FLAGGED: `RAW HOST` in `SERVER`, `INSTANCE` and `DB_CONNECTION_PROPERTIES` |
| `fgdbref.sd` | Local file geodatabase, `copyDataToServer = False` | PASS: the connection names no host |

7-Zip 24.08 lists 59 files under `cd\synthetic.gdb` in `copied.sd`, the same count the tool
reports. Each Pro archive was non-solid, used `LZMA:20`, and had an encoded header. The same
four files gave the same verdicts on Windows (Python 3.13.2) and on Ubuntu (Python 3.12.3).

```
$ python sdcheck.py byip.sd --expect-host localhost
byip.sd: FLAGGED
  RAW HOST     synthdb: live connection names DB_CONNECTION_PROPERTIES=127.0.0.1, INSTANCE=127.0.0.1, SERVER=127.0.0.1, not an expected host; dataset(s): PTS (query layer)
1 file(s): 0 pass, 1 flagged, 0 no verdict
```

The analyzer did not warn about the copy. In every one of these offline staging runs, the only
analyzer warning was 24078, for the basemap layer. Esri documents warning 24011 for data that
"will be copied to the server", but these runs had no server to compare against. The staged
`.sd` itself was the only record that the data was copied.

## The reader

`.sd` is not a documented format. The 7-Zip signature and 7-Zip's own listing show that Pro
writes a 7z archive. The reader follows 7-Zip's `DOC/7zFormat.txt` and `DOC/Methods.txt`.

| It reads | It refuses, with NO VERDICT |
|---|---|
| Plain headers and encoded (compressed) headers | A chain of two or more coders, such as BCJ plus LZMA |
| LZMA, LZMA2 and stored (Copy) members | PPMd, or any coder other than the three on the left |
| Solid and non-solid archives | An encrypted archive (7zAES) |
| Empty files and directories | Names or folders stored outside the header |
| CRC checks on the start header, the header and the member | A manifest that ends more than 64 MB into its block, refused before decompression |

The `--sevenzip` fallback runs only when the reader refuses an archive as unsupported. It runs
`7z e -so -y <file> manifest.xml`, without `-r`, so 7-Zip matches the name at the archive root
only. Stdin is closed, so a password prompt ends instead of waiting. The fallback was run for
real with 7-Zip 24.08 on Windows and 7-Zip 23.01 on Ubuntu. Both read the BCJ and PPMd test
archives and passed them. Both exited 255 on the encrypted one, which gave NO VERDICT.

A manifest that declares a DTD or an entity is refused before it is parsed. Python's XML
documentation warns that Expat versions lower than 2.7.2 may be vulnerable to entity expansion
attacks. None of the four Pro manifests here declares either.

## The existing alternatives

- **7-Zip and grep.** `7z e -so staged.sd manifest.xml` and a search for `SERVER=` works when
  7-Zip is installed. It is the right first look at one file. A search for `SERVER=` misses the
  other host keys and does not read `ByReference`. With `-r`, 7-Zip 24.08 and 23.01 both join
  every `manifest.xml` in the archive into one stream. A two-level test archive showed this.
- **The Administrator API service manifest.** ArcGIS Server publishes
  `/services/<name>.<type>/iteminfo/manifest/manifest.json` for each published service. It is
  authoritative, and it covers a whole site. Esri's
  [ops-server-config](https://github.com/Esri/ops-server-config) `ListServiceWorkspaces.py`
  lists it for every service on a site, and does that well. It needs a running server and an
  administrator sign-in, and it reads the service after it is published. `sdcheck` reads the file before it is uploaded.
- **[py7zr](https://github.com/miurahr/py7zr).** A complete 7z library with more coders,
  including PPMd, BCJ and AES. Use it to extract a whole archive. `sdcheck` needs one member,
  so it reads that member with the standard library and adds no dependency.

A `gh search repos sdcheck` on 2026-10-09 found no tool that checks a service definition. Every
match was an SD-card checker or an unrelated project.

## Limits

- Hosts compare by name, without case. The tool does not resolve DNS. An alias and the raw
  host it points to are two different names, which is the point.
- The host cut drops the text after the first `\`, `,` or `/`, then a trailing `:port`. An
  IPv6 literal does not survive that cut.
- Only Pro 3.6 `.sd` files were tested. Files staged by ArcMap or by older Pro releases were
  not, so their manifest layout is not proven here.
- `DB_CONNECTION_PROPERTIES` is not on Esri's workspace properties page. It is read because it
  carried the host in every SQL Server connection that Pro 3.6 staged here.
- Pro refused to stage a map that held only a basemap (analyzer error 00102). So the NO VERDICT for a
  manifest with no database comes from a synthetic manifest only. A service that truly reads no
  data, such as some geoprocessing services, gets NO VERDICT.
- The tool reads the first child element of each kind in a manifest. With two `ByReference`
  elements in one database, only the first counts. Text after a child element inside the
  connection string is dropped, so `SERVER=ok;<b/>INSTANCE=sde:sqlserver:rawhost` passes.
- With two `manifest.xml` members at the archive root, the tool reads the first one. `7z e -so`
  joins both into one stream (measured with 7-Zip 24.08).
- Values are compared as written. `SERVER="gisdb.example.org"`, `SERVER=tcp:gisdb.example.org,1433`,
  `gisdb.example.org.` and `SERVER={gisdb.example.org}` are all flagged as `RAW HOST`, even when
  they name the expected host, so write `--expect-host` in the spelling the connection uses. A
  port in `--expect-host` flags every connection. This fails safe.
- A flagged file plus an `--out` that cannot be written exits 2, not 1. The flag is still
  printed, but a gate that tests only for exit 1 misses it.
- `--out` with `--apply` overwrites any file it names, including an input `.sd` (measured:
  `--out keep.sd --apply` replaced `keep.sd` with JSON).
- With `--sevenzip`, a `.sd` path that starts with `-` is read by 7-Zip as a switch and gives
  NO VERDICT (7-Zip exited 7). Spaces, `@` and `[ ]` in the path worked. The fallback reads no file list, so a copied
  dataset's file count is `unknown`. Its output is not size-limited, because
  `MAX_MANIFEST_BYTES` applies to the built-in reader only. On Windows a bare `--sevenzip 7z` can
  resolve to a `7z.exe` in the current directory, so give a full path.
- Two databases with the same `Name` (or two with no name) share one in-package file count,
  because the count is keyed by name.
- The host check was cross-checked only against the SQL Server and file geodatabase connections
  that Pro 3.6 staged. Oracle, PostgreSQL and other DBMS forms are covered by synthetic strings.
  A form the tool does not know gives NO VERDICT, never a pass.
- The hard-link, self-extracting (SFX prefix) and multi-volume archive cases were not tested.
- `--self-test` exits 1 when an assertion fails, the same code as FLAGGED. It also writes a
  temporary directory and a stub script outside `--apply`, and removes them at the end.
- The reader holds the whole header, up to 64 MB, in memory. It does not cap the memory that the
  stream counts of a hostile header can ask for.
- The 120 second timeout on `--sevenzip` is not exercised by the self-test.
- Python's `lzma` documentation pairs `FILTER_LZMA1` with `FORMAT_ALONE`. This reader uses it
  with `FORMAT_RAW`. All three interpreters above decode the four Pro archives and the 7-Zip
  test archives that way.

## Test archives

Six archives in `REAL_7Z` were made by 7-Zip 24.08 (x64). Each folder held a `manifest.xml`
from `_fixture_manifest`, plus the synthetic members the self-test names. The commands were:

```
7z a -t7z pro_like.sd * -m0=LZMA:d20 -ms=off
7z a -t7z lzma2_solid.sd * -m0=LZMA2 -ms=on -mhc=off
7z a -t7z bcj.sd * -m0=BCJ -m1=LZMA -mhc=off
7z a -t7z aes.sd * -mhe=on -p<password>
7z a -t7z ppmd.sd * -m0=PPMd -mhc=off
7z a -t7z -m0=Copy -mhc=off -ms=off copy7.sd manifest.xml
```

`copy7.sd` holds only `manifest.xml`. 7-Zip stores the member CRC in the substreams and writes no
folder CRC, so the self-test corrupts a byte there and expects `CRC does not match`.

`_build_7z` in the self-test writes further archives for the branches that 7-Zip does not
write. The 7-Zip archives keep the reader honest against a writer it did not come from.

## Sources

- 7-Zip, [`DOC/7zFormat.txt`](https://github.com/ip7z/7zip/blob/main/DOC/7zFormat.txt) and
  [`DOC/Methods.txt`](https://github.com/ip7z/7zip/blob/main/DOC/Methods.txt): headers, number
  encoding, property ids and coder ids.
- Esri, [Service Manifest](https://developers.arcgis.com/rest/enterprise-administration/server/servicemanifest/)
  (Administrator API): `byReference`, `onServerConnectionString` and
  `onPremiseConnectionString`.
- Esri, [MapServiceDraft](https://doc.esri.com/en/arcgis-pro/latest/arcpy/sharing/mapservicedraft-class.html):
  `offline`, `offlineTarget` and `copyDataToServer`.
- Esri, [Stage Service](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/server/stage-service.html).
- Esri, [Workspace properties](https://doc.esri.com/en/arcgis-pro/latest/arcpy/functions/workspace-properties.html):
  the `connectionProperties` keys `server`, `instance`, `database`, `dbClient`, `user`, `version`,
  `branch`, `authentication_mode`, `historical_name`, `historical_timestamp` and `is_geodatabase`.
  The page names `server` as the key that carries a host. It does not list `ENCRYPTED_PASSWORD`,
  `ENCRYPTED_PASSWORD_UTF8` or `DB_CONNECTION_PROPERTIES`, which Pro 3.6 wrote in every SQL Server
  connection staged here. It does not describe Oracle `//host` values, so that form is covered by
  a synthetic string only.
- Esri, [Analyzer warning 24011](https://doc.esri.com/en/arcgis-pro/latest/help/sharing/analyzer-warning-messages/24011-data-source-is-not-registered-with-the-server-and-data-will-be-copied-to-the-server.html).
- Python, [`lzma`](https://docs.python.org/3/library/lzma.html): `FORMAT_RAW`, filter options
  and `max_length`.
- Python, [XML security](https://docs.python.org/3/library/xml.html).

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [stalehost](https://github.com/uhsear/stalehost) - find workstation files that still name the host you are retiring
- [svcsource](https://github.com/uhsear/svcsource) - report the database behind every service already published
- [svcdrift](https://github.com/uhsear/svcdrift) - diff a published feature service against the dataset it came from
