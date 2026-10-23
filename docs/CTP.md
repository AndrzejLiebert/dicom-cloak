# CTP engine: RSNA CTP DicomAnonymizer with the DICOM Cloak allowlist

For sites that require RSNA CTP. The de-identification is done by CTP's own DicomAnonymizer, configured as
an **allowlist** generated from the same policy files as the Python engine. Its output passes the same
acceptance test (`dicom-cloak-check --require-remapped-uids`).

## Version
- CTPTools25 commit `7db432f` (2025-10-31): CTP and DicomAnonymizerTool (DAT) built for **Java 25**.
  This was the latest build when checked (October 2026).
- Installed by `docker/install_ctp.py`, which pins the commit and checks SHA-256. The installer jars
  are plain zip archives, so no GUI installer is needed.
- Runtime: Eclipse Temurin 25 JRE (amd64 and arm64).
- Upgrading: change the commit and both checksums together, then rerun `tests/test_ctp.py`.

## Two ways to run

| | `dicom-cloak-ctp run` (batch, recommended) | CTP server pipeline (`ctp/config.xml`) |
|---|---|---|
| Anonymizer | CTP DicomAnonymizer through DicomAnonymizerTool | CTP DicomAnonymizer stage |
| Allowlist (standard + vendor private) | yes | yes |
| Pseudonyms, UID remapping, year-only dates, age groups | yes | yes |
| Relative clock, joint DCE timing block, visit numbering | yes (Python pre-pass, lookup table) | no (times removed) |
| Siemens CSA series header / XA protocol | kept after the ASCCONV scrubber (Python post-pass) | removed |
| Rejection notes, `exclude_series_regex` | yes | no |
| Per-file verification (byte search for each study's original identifiers) | yes | run `dicom-cloak-check` on the output |
| Output names | `<pseudonym>/study_NN/<NNNN_series>/IM_*.dcm`, `_deid_meta/*.csv` | `{PatientID}/{StudyInstanceUID}/{SeriesNumber}/<SOPInstanceUID>.dcm` |

In batch mode the run works like this:
1. **Python pre-pass**, headers only. It plans studies, visits and timing with the engine code and
   writes a CTP lookup table:
   - `ptid/` holds pseudonyms;
   - `tm_*/` holds the relative clock;
   - `sec/`, `pos/` and `npos/` hold the joint timing;
   - `role/`, `tref/`, `study/` and `days/` hold the remaining planned values;
   - `agegroup/` maps PatientAge to an age group.

   It also builds a staging tree of symlinks with neutral names, because CTP keeps the input file names.
2. **DAT** applies the generated script to the staging tree. Its output goes to the private WORK
   folder.
3. **Python post-pass**:
   - runs the protocol scrubber;
   - sets the VRs of the engine's private block;
   - drops creators of empty blocks;
   - writes the output, verifies every header and writes the CSV reports.

   WORK is then emptied.

```bash
dicom-cloak-ctp run --src IN --dst OUT --work WORK --pseudonym-map map.csv --uid-key uid_key.hex \
    [--auto-pseudonyms SUBJ_] [--study-registry registry.csv] [--threads N] [--workers N]
dicom-cloak-check OUT --require-remapped-uids
```
The launchers do the same in the `ctp` image with `bash cloak.sh --ctp` / `cloak.bat --ctp` (WORK then stays
inside the container and is discarded with it).

Server pipeline:
```bash
dicom-cloak-ctp script --out /opt/CTP/scripts --pseudonym-map map.csv --uid-key uid_key.hex
cd /opt/CTP && java -jar libraries/CTP.jar   # foreground; Runner.jar start/stop needs java on PATH
```
`config.xml` defines the pipeline: DicomImportService (port 1104), or a DirectoryImportService drop
folder → DicomFilter (MR / Enhanced MR only) → DicomAnonymizer → DirectoryStorageService.

## How the policy maps to the CTP script (`src/dicom_cloak/ctp.py`, `build_script`)
- **Global switches:**
  - `remove.unspecifiedelements` makes it allowlist mode;
  - `remove.privategroups`, `remove.overlays` and `remove.curves` are on;
  - `keep.safeprivateelements` stays **off**.

  CTP's bundled safe-private dictionary has no Siemens or Philips MR entries coded K, so it would lose
  b-values and Philips scaling. The vendor list is used instead.
- **Standard keep list:** one element per keyword.
  - Sequences use `@process()`, so their items pass through the same script. `@keep()` would keep
    nested content unfiltered.
  - PN, DA and AE are removed.
  - TM: AcquisitionTime and ContentTime come from the lookup (relative clock); all other times are
    removed.
  - DT is reduced to the year: `@contents(this,"^(\\d{4})?.*$","$1")`.
  - UIDs: `@hashuid(@UIDROOT,this,<salt carrier>)`, which is `2.25.` + MD5(original UID + secret salt).
    The salt is derived from the UID key. Standard UIDs (1.2.840.10008.) and empty values are kept.
  - PatientName and PatientID: `@lookup(PatientID,ptid)` at the top level and removed inside
    sequences. An unknown PatientID sends the object to quarantine.
  - SOPClassUID: objects of any other class are quarantined.
- **Removal list:** scripted `@remove()` as a second line of defence. Group 0028 attributes outside the
  allowlist are also removed explicitly, because CTP keeps group 0028 unless an element is scripted.
- **Vendor private list:** `gggg[Creator]ee` entries with `@process()`.
  - Creator matching in CTP is exact and case-sensitive, so each creator is listed as written and in
    upper case.
  - Creator elements cannot be addressed by creator, so `(gggg,0010)`–`(gggg,00FF)` are kept only if
    their value matches a whitelisted creator.
  - Entries with `scrub:` are `@keep()` in batch mode, where the post-pass scrubs them, and `@remove()`
    in server mode.
- **The engine's private block** (`0011,10xx`): `@always()` + `@lookup(...)`.

## CTP behaviours to be aware of (checked in the CTP source)
- In a **CTP pipeline**, an object for which the anonymizer returns `@skip()` is **passed on
  unchanged** (`DicomAnonymizer.java`: `status.isSKIP() ; //keep the input object`). In DAT, skip means
  "not written". The script therefore never uses `@skip()`. Unwanted objects are quarantined, and the
  server config filters SOP classes before the anonymizer.
- `@hashuid` without a third argument is unsalted MD5. Anyone holding an original UID could link it to
  its replacement, so a salt is always appended.
- Private element scripts `gggg[Creator]ee` are resolved once per object against the **top-level**
  creators. Private elements inside sequences that use a different block are removed. The checker
  verifies that nothing else slips through.
- Script function arguments consume one level of backslashes: regexes need `\\d`, `\\.`.
- XML comments in `config.xml` must not contain `--`.
- CTP keeps group 0028, group 0002 (rebuilt) and SOPClass/SOPInstance/StudyInstance UIDs unless they
  are scripted.
- DAT always exits 0 and can stay alive after the last file. `run_dat` counts outcomes in the log
  (`-v`) and stops the process.
- Quarantine folders hold **original** objects. Treat them like source data.
- DicomAnonymizerTool writes deflated input without its pixel data. The batch pre-pass therefore stages
  deflated files as an inflated copy, and the post-pass fails any image that comes back without pixel
  data. In a server pipeline, do not send deflated objects.

## Validation
- Synthetic studies (Siemens incl. XA protocol blob, Philips, GE, Enhanced MR, three visits): the CTP
  output is identical to the Python engine's output **element by element, apart from the UID values**.
  This covers:
  - the allowlist, the private keep list and the scrubbed protocol blob;
  - the relative clock, TemporalPositionTimeOffset and the joint timing block.

  The checker and the dcm2niix comparison pass (`tests/test_ctp.py`).
- Real public data: 26 patients, 33 studies, 47,721 files (GE, Philips and Siemens from 5 public
  collections).
  - 0 failures, and the checker passes.
  - dcm2niix output is identical for 25/26 patients. The remaining patient differs exactly as with the
    Python engine: a known dcm2niix mis-assembly of one Siemens series type.
  - CTP and Python outputs are identical apart from UIDs and one CTP addition: SpecificCharacterSet
    `ISO_IR 100` where the source had none.
