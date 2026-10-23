# Technical reference

For maintainers and IT staff. Users start with the `README.md` in the project root. This document
covers the package `dicom-cloak` (Python 3.10+, pydicom 3.0.2), its two engines, the policy files, the
tests, how to change the policy, and the validation done so far. The CTP engine is described in
`docs/CTP.md`; external references and versions are in `docs/SOURCES.md`.

Layout:

| Path | Content |
|---|---|
| `cloak.sh`, `cloak.bat` + `cloak.ps1` | launchers for macOS/Linux and Windows (build image, run, check) |
| `src/dicom_cloak/engine.py` | policy loading, cleaning, timing, writing, per-file verification |
| `src/dicom_cloak/cli.py` | `dicom-cloak run` |
| `src/dicom_cloak/check.py` | `dicom-cloak-check`, the independent acceptance test |
| `src/dicom_cloak/scrub.py` | allowlist scrubber for Siemens protocol blobs |
| `src/dicom_cloak/ctp.py` | `dicom-cloak-ctp run / script` |
| `src/dicom_cloak/compare.py` | `dicom-cloak-compare`: dcm2niix before/after |
| `src/dicom_cloak/report.py` | `dicom-cloak-report`: attributes the allowlist would drop |
| `config/` | policy files and run configuration (mounted into the container by the launchers) |
| `ctp/config.xml` | CTP server pipeline |
| `docker/` | `Dockerfile` (targets `python`, `ctp`), `install_ctp.py` (pinned CTP download) |
| `tests/` | synthetic data and tests |
| `tools/` | `xref_private_lists.py`, `compare_engines.py`, `validate_public.sh` |

## 1. Installation for development

### Native Python (Linux or macOS)
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install ".[test]"            # pydicom==3.0.2, numpy, pyyaml, pytest, nibabel, dcm2niix (PyPI wheel)
python -m pytest -q tests        # 35 pass + 4 skipped (CTP runs) without Java/DAT
```
The dcm2niix wheel puts a `dcm2niix` binary into the venv's `bin`, which must be on PATH for
`test_conversion.py` and `dicom-cloak-compare`. Without it that test is skipped. Natively, pass
`--config config/config.yaml` to the commands (inside Docker the default config is used).

### CTP runtime without Docker (for `dicom-cloak-ctp` and the CTP tests)
```bash
mkdir -p ~/opt && cd ~/opt
# Java 25 JRE, no system install. On macOS use .../mac/aarch64/... and Contents/Home/bin/java
curl -fsSL -o jre25.tar.gz "https://api.adoptium.net/v3/binary/latest/25/ga/linux/x64/jre/hotspot/normal/eclipse?project=jdk"
tar xzf jre25.tar.gz          # -> jdk-25.x.y+z-jre/bin/java
python -I /path/to/dicom-cloak/docker/install_ctp.py ~/opt   # pinned + SHA-256
export DAT_HOME=~/opt/DicomAnonymizerTool JAVA=$(ls -d ~/opt/jdk-25*)/bin/java
cd /path/to/dicom-cloak && python -m pytest -q tests     # 39 pass
```

### Docker
```bash
docker build -f docker/Dockerfile --target python -t dicom-cloak:1.0.0 .
docker build -f docker/Dockerfile --target ctp    -t dicom-cloak:1.0.0-ctp .
docker run --rm --network none --entrypoint python dicom-cloak:1.0.0     -m pytest -q -p no:cacheprovider tests
docker run --rm --network none --entrypoint python dicom-cloak:1.0.0-ctp -m pytest -q -p no:cacheprovider tests
# multi-architecture images for a registry (Intel/AMD and Apple Silicon):
docker buildx build --platform linux/amd64,linux/arm64 -f docker/Dockerfile --target python -t <registry>/dicom-cloak:1.0.0 --push .
```
- The launchers tag images as `dicom-cloak:<version>` and `dicom-cloak:<version>-ctp`, with the version
  from `src/dicom_cloak/__init__.py`. After changing the code, run them once with `--rebuild`.
- The `ctp` image needs network access at build time (`install_ctp.py`). Runs use `--network none`.
- All dependencies exist for linux/arm64 (dcm2niix, numpy and pyyaml wheels; `python:3.11-slim` and
  `eclipse-temurin:25-jre`), so Docker Desktop on Apple Silicon builds the image natively.
- The launchers mount `config/` over the image's config, so edits to `config/config.yaml` apply without
  a rebuild.
- Manual run without the launcher (Linux/macOS):
  ```bash
  docker run --rm --network none --user "$(id -u):$(id -g)" \
    -v "$IN":/in:ro -v "$OUT":/out -v "$SECRETS":/secrets dicom-cloak:1.0.0 \
    run --src /in --dst /out --pseudonym-map /secrets/pseudonym_map.csv --auto-pseudonyms SUBJ_ \
        --uid-key /secrets/uid_key.hex --study-registry /secrets/study_registry.csv
  docker run --rm --network none -v "$OUT":/out:ro --entrypoint dicom-cloak-check dicom-cloak:1.0.0 \
    /out --require-remapped-uids
  ```

### Public test data (TCIA)
- Use the NBIA Data Retriever (CLI) or the `tcia_utils` Python package with a manifest. Collections
  used for validation are listed in `docs/SOURCES.md`.
- Folder names from the retriever can contain literal double quotes (`"EA1141"`): quote paths in loops.
- Pick a few patients per scanner, vendor and software line. Use symlink trees (`cp -rs`) instead of
  copies, then run `tools/validate_public.sh IN WORKROOT` (long-running: use tmux or screen, and delete
  WORKROOT afterwards).
- Rough timing on a 12-core workstation: 47.7k files take about 7.5 min with the CTP engine and a
  similar time with the Python engine; the checker is single-process and takes about 5 min.

## 2. Commands (console scripts in `pyproject.toml`)

| Command | Module | Purpose |
|---|---|---|
| `dicom-cloak run` | `cli.py` → `engine.py` | Python engine: de-identify a DICOM tree into a clean copy |
| `dicom-cloak-ctp run` | `ctp.py` | CTP engine: same result, CTP DicomAnonymizer does the de-identification |
| `dicom-cloak-ctp script` | `ctp.py` | CTP script, lookup table and filter script for a CTP server pipeline |
| `dicom-cloak-check FOLDER` | `check.py` | independent acceptance test (headers only, prints field names only) |
| `dicom-cloak-compare ORIG DEID` | `compare.py` | dcm2niix before/after, paired by voxel content |
| `dicom-cloak-report FOLDER` | `report.py` | which standard attributes the allowlist would drop (names only) |

Common options:
- `--config` (default `$DICOM_CLOAK_CONFIG`, in Docker `/opt/dicom_cloak/config/config.yaml`).
- `--pseudonym-map map.csv`, a CSV with header `original_patient_id,pseudonym`. Alternatively
  `--use-existing-patient-id`, when PatientID already is a pseudonym matching `pseudonym_regex`.
- `--auto-pseudonyms PREFIX` (with `--pseudonym-map`): patients missing from the map get the next free
  `PREFIXnnnn` (e.g. `SUBJ_0001`); the map file is created or extended with mode 600. Existing entries
  never change, so a patient keeps the same pseudonym in later runs. The launchers always use this.
- The map, the key and the registry must live outside `--dst`, and `--dst` must not be inside `--src`;
  the run refuses to start otherwise.
- `--uid-key key.hex`, at least 16 bytes of hex. It is created with mode 600 if missing or empty.
  Keep it for every run of the same project, so UIDs stay consistent across exports.
- `--study-registry registry.csv`, which continues visit numbering across runs. It is secret because
  it holds original dates, and it must live outside `--dst`.
- `--workers N` sets the number of processes. CTP also takes `--threads`, `--dat-home`, `--java`,
  `--work` and `--keep-work`.

Exit codes:
- `run`: 1 if any study fails or any written file fails verification.
- `check`: 1 if any rule fires.
- `compare`: 1 if any original volume has no identical counterpart.

## 3. Python engine data flow (`engine.py`)

```
scan (headers only, cli.group_by_patient: all studies of a patient go to one worker)
 └─ plan_studies(headers, pseudonym_of, policy, registry, uidmap)        -> [StudyPlan]
      - rejection notes (KOS 1.2.840.10008.5.1.4.1.1.88.59): referenced instances dropped
      - SOP class filter (standard_remove.yaml sop_classes.keep: MR, Enhanced MR)
      - exclude_series_regex (SeriesDescription/ProtocolName)
      - classify(): derived_map | mip | subtraction | role from series_roles regexes
      - collect_forbidden(): original identifying values of the study (bytes) for the byte search
      - visits: chronological per pseudonym; registry keeps numbers and the day-0 anchor
      - choose_time_source() per series, build_timing() over the study (joint scheme)
 └─ write_study(plan, dst, uidmap, policy)
      per file: clean_dataset() -> pseudonym, de-id attributes, TemporalPositionTimeOffset,
                add_private() block -> save under neutral name -> verify_file()
      returns patient_row, series_rows, excluded_rows, problems
CSV: OUT/_deid_meta/patients.csv, series_timing.csv, excluded.csv
```

### clean_dataset (recursive, any depth)
- Private elements are kept only if `(group, creator lower-case, element byte)` is in the vendor
  allowlist.
  - An entry with `scrub:` goes through `scrub.SCRUBBERS[...]`. If the scrubber returns None, the
    element is removed.
  - Kept private sequences are cleaned recursively with the standard rules.
  - Creator elements are kept only while their block still holds kept content.
- Overlays (60xx) and curves (50xx) are removed. So is any keyword in `standard_remove.yaml`, and, in
  allowlist mode, anything not in `standard_keep.yaml`.
- VR rules:

  | VR | Rule |
  |---|---|
  | PN | removed; top-level PatientName is set to the pseudonym |
  | DA, AE | removed |
  | TM | removed, except AcquisitionTime/ContentTime → relative clock |
  | DT | first 4 characters (the year) |
  | UI | remapped by `UIDMapper` unless the keyword is in `KEEP_UI_KEYWORDS` or the value starts with 1.2.840.10008. |
- UIDMapper: `2.25.` + int(HMAC-SHA256(key, uid)[:16]). It is consistent across runs with the same key,
  and FrameOfReference sharing is preserved.
- Relative clock (`config.yaml relative_clock`, default "010000"): time = base + (absolute acquisition
  time − study anchor). Intervals stay exact, which dcm2niix needs for SliceTiming and for the TR of
  dynamics; the real time of day is hidden. The base is small because dcm2niix stores HHMMSS as
  float32.
- File meta: MediaStorageSOPInstanceUID is set to the new SOPInstanceUID. Private, AE and other
  non-core elements are removed.

### Timing (joint DCE scheme)
- **Time source per series**, first usable in this order: AcquisitionDateTime →
  FrameAcquisitionDateTime → Date+AcquisitionTime → ContentDate+ContentTime →
  TemporalPositionTimeOffset. A source is usable if it is present in every file and, for
  multi-position series, gives a distinct start time per TemporalPositionIdentifier. Philips classic
  DCE repeats the series start in AcquisitionTime, so this check matters.
- **Volumes**:
  - Philips/GE style (several TPIs in one series): grouped by TPI.
  - Siemens style (one series per volume): files clustered by time gaps larger than
    `timing_cluster_gap_s` (2 s).
  - Volumes of reference + dynamic series that start within 2 s of each other share one joint
    position. Dixon F/W/in/opp images of one frame are the typical case.
- **t0**: the earliest reference (native) file. If there is none, the earliest dynamic file.
  `timing_reference` records which: reference_series, first_dynamic_volume,
  temporal_position_identifier_only or none.
- Subtraction series take the position of the volume acquired at their time. Derived maps and MIPs
  never get a position.
- Series with several TPIs but no separating time source keep the vendor TPI order and get no seconds,
  rather than invented offsets.
- The vendor TemporalPositionIdentifier and NumberOfTemporalPositions are **never rewritten**: dcm2niix
  uses them to assemble 4D volumes. The joint position goes into the private block.

### Engine private block (group `output_private_group` 0x0011, creator "DICOM CLOAK 1.0", block 0x10)
| element | VR | content |
|---|---|---|
| 0x01 | LO | age group (`age_bins` [18,30,40,50,60,70,80] → `<18`, `18-30`, `31-40`, …, `71-80`, `81+`, `unknown`) |
| 0x02 | DS | seconds after the reference start (`%.3f`) |
| 0x03 | LO | role: reference / dynamic / subtraction / mip / derived_map / other |
| 0x05 | LO | timing reference (see above) |
| 0x06 | LO | series-name tags, e.g. `TTC_s=20.0` (`series_name_tags` regexes) |
| 0x07 | IS | study (visit) index, 1 = first visit |
| 0x08 | IS | days since the patient's first visit (may be negative for a visit added later that predates it) |
| 0x09 | IS | joint DCE temporal position |
| 0x0A | IS | number of joint positions in the study |
TemporalPositionTimeOffset (FD) = seconds after reference, as a standard attribute.

### Output layout
`OUT/<pseudonym>/study_NN/<NNNN_SeriesDescription-sanitised>/IM_<InstanceNumber:05>_<k:05>.dcm`.
File names never derive from source file names.

CSV columns:
- `patients.csv`: pseudonym, study, days_since_first_study, timepoint_id (ClinicalTrialTimePointID),
  sex, age_group, acquisition_year, timing_reference, n_temporal_positions, n_series, n_files,
  n_problems.
- `series_timing.csv`: pseudonym, study, series_folder, role, temporal_positions,
  n_temporal_positions, seconds_after_reference_start/end, series_name_tags, time_source,
  timing_reference, n_files.
- `excluded.csv`: pseudonym, study, series_description, reason, n_files.

### Verification (`verify_file`, every written file, header bytes only)
- Byte search of the header for the study's original identifying values (`collect_forbidden`):
  - PN/DA/DT/TM/AE values, identifying keywords and original UIDs;
  - values of 6 or more characters, PN of 4 or more;
  - placeholders such as 000000 skipped;
  - the engine's own new 2.25 UIDs masked first, because random digits can contain short originals.
- Rule walk:
  - allowlist and private whitelist;
  - no PN except PatientName; no DA/AE; no TM except the relative ones; DT at most 4 characters;
  - every UID either 2.25.* or standard;
  - no removal keyword; PatientName/ID equal to the pseudonym; PatientIdentityRemoved = YES.

## 4. CTP engine (`ctp.py`), see also `docs/CTP.md`
```
pre-pass  _plan_job: the same plan_studies() -> lookup entries + staging symlinks with neutral names
script    build_script(config, salt, scrub_later=True) + filter_script(), written to WORK (mode 600)
CTP       run_dat(): java -jar DAT.jar -in WORK/stage -out WORK/ctp_out -f filter -da script -lut lut -n T -v
post-pass _post_job: scrub_private() (protocol blobs; drop creators of empty blocks),
          normalize_output_block() (VRs of 0011 block), save to OUT, engine.verify_file(), CSV rows
```
Lookup table key types (`keyType/key=value`, java.util.Properties escaping, ISO-8859-1 with \uXXXX):

| key type | keyed by | value |
|---|---|---|
| `ptid/` | original PatientID | pseudonym |
| `agegroup/` | `nnnY`, `nnnM`, `nnnW`, `nnnD` (all 0–999) | age group |
| `study/`, `tref/`, `days/` | original StudyInstanceUID | visit index, timing reference, days since first visit |
| `role/`, `stags/` | original SeriesInstanceUID | role, series-name tags |
| `sec/`, `pos/`, `npos/`, `tm_AcquisitionTime/`, `tm_ContentTime/` | original SOPInstanceUID | seconds, joint position, number of positions, relative-clock times |

The UID salt is HMAC-SHA256(uid key, "dicom_cloak ctp uid salt"). It is put in the script as
`<p t="UIDSALT">` and fed to `@hashuid` through the salt carrier element (0011,10FF), which is never
written. Result: `2.25.` + decimal MD5(original UID + salt).
The script mapping and the CTP pitfalls are described in `docs/CTP.md`.

## 5. Policy files (`config/`)
- `config.yaml` holds:
  - `mode` (allowlist | blocklist) and the file names of the three policy files;
  - `vendors` [siemens, philips, ge];
  - `output_private_group` and `output_private_creator`;
  - `relative_clock` and `relative_time_attributes`;
  - `pseudonym_regex` and `age_bins`;
  - `exclude_series_regex`, the site-specific do-not-send series;
  - `series_roles` (ordered regexes; first match wins) and `series_name_tags`;
  - `derived_imagetype_tokens`, `derived_name_regex` and `timing_cluster_gap_s`.
- `standard_keep.yaml` has `keep: {category: [keywords]}` and `keep_sequences: [keywords]`. The policy
  refuses to load if a keyword is both kept and removed.
- `standard_remove.yaml` has `remove: {category: [keywords]}` and `sop_classes.keep`.
- `vendor_private.yaml` has `<vendor>: keep: [ {group, creator, element, name, why, src?, scrub?} ]`,
  plus `remove_explicitly` and `notes`.
  - The element is the low byte (the xx block is assigned per file).
  - `src` is tcia-K, tcia-KB, ps3.15 or ps3.15+tcia; no `src` means the entry comes from the dcm2niix
    source.
  - `scrub` is siemens_ascconv (XA/XB text protocol) or siemens_csa_series (CSA2 SV10 container).

## 6. Siemens protocol scrubber (`scrub.py`)
- Only the `### ASCCONV BEGIN ... END ###` block is kept from MrPhoenixProtocol / the XA protocol text.
- A line is kept if its key's group is in `ASCCONV_KEEP_GROUPS`, its top-level key is in
  `ASCCONV_KEEP_TOP` (matched on the group, so `alTR[0]` counts), or the key is in `STRING_KEYS`.
- Values must be numeric. Allowed strings are those in `STRING_KEYS` or `STRING_KEY_PATTERNS`. A string
  containing 8 consecutive digits is dropped (dates), except the sProtConsistencyInfo baseline
  strings. Strings longer than 66 characters are dropped.
- XA output starts with `<XProtocol>\n`, because dcm2niix skips the first byte of a text protocol; it
  is padded to even length.
- CSA2: the series header is parsed and rebuilt with only MrPhoenixProtocol, MrProtocolVersion and
  MrProtocol, each filtered. The result is readable by nibabel's csareader.

## 7. Tests (`tests/`)

Run with `python -m pytest -q tests`. 39 tests; the 4 CTP run tests are skipped without Java 25 and
DicomAnonymizerTool (the `ctp` image has both, so all 39 run there).
- `synthetic.py` builds the synthetic studies. Siemens has an XA blob, CSA, planted leaks in nested
  sequences, OriginalAttributesSequence and a TWIST/Dixon TTC series. The others are Philips with a
  constant AcquisitionTime, GE DWI plus an SR, Enhanced MR, and three visits. Geometry and scaling are
  non-default on purpose.
- `test_pipeline.py`: leaks removed, timing, whitelists, the checker failing on raw input and passing
  on output, multi-visit.
- `test_inputs.py`: implicit VR, deflated and RLE-compressed input (pixel values unchanged, deflated
  output written inflated); automatic pseudonyms across runs; secret files refused inside the output.
- `test_conversion.py`: dcm2niix identical before/after; skipped without dcm2niix/nibabel.
- `test_scrub.py`: scrubber.
- `test_ctp.py`: script invariants; a full CTP run equal to the Python output apart from UIDs; CTP runs
  on implicit VR, deflated and RLE input with automatic pseudonyms. The runs need `DAT_HOME` and `JAVA`.

## 8. Changing the policy
1. Allowlist first: to keep a new attribute, add it to `config/standard_keep.yaml` (right category) and
   confirm that `tests/test_conversion.py` still passes. To drop one, remove it and check that the
   conversion test still passes. Mutation check: remove a geometry or scaling attribute temporarily;
   the conversion test must then fail.
2. Edit the YAML, not the code, when possible. Add a synthetic case for the new rule to
   `tests/synthetic.py` and an assertion to `tests/test_pipeline.py`.
3. New vendor private tag: add it to `config/vendor_private.yaml` with group, **private creator**,
   element byte and the reason. Verify every entry against the dcm2niix source
   (`console/nii_dicom.cpp`) and the vendor conformance statement. Kept private values still go through
   the byte search, because free-text blobs can carry identifiers.
4. New leak class: add the planted leak to the synthetic data and confirm that both the engine's
   verifier and `dicom-cloak-check` catch it on the raw input.
5. CTP: both engines read the same YAML. Never hand-edit a generated CTP script; change the policy and
   regenerate it, then run `tests/test_ctp.py` with DAT. CTP output must equal Python output apart from
   the UIDs. Never use `@skip()` in a CTP script (in a pipeline it passes the original object on); use
   `@quarantine()`.
6. Run `dicom-cloak-report FOLDER` on data from each new vendor or software version to see which
   standard attributes the allowlist drops (names only), and add only technically relevant ones.

## 9. Validating on real data (per vendor / software line)
1. Copy a few original studies (public collections, or data you are allowed to process) into a
   scratch input folder.
2. `dicom-cloak run ...` (and/or `dicom-cloak-ctp run ...`), then `dicom-cloak-check OUT
   --require-remapped-uids`, then for each study
   `dicom-cloak-compare --config config/config.yaml ORIG_STUDY OUT/<pseudonym>`.
3. Any technical difference: find which private/standard tag dcm2niix reads (search the dcm2niix
   source `console/nii_dicom*.cpp` for the field) and extend the allowlists. Never "fix" a difference
   by loosening the leak checks.
4. A series that dcm2niix mis-assembles in BOTH conversions is a dcm2niix issue, not a
   de-identification issue.

Acceptance checklist for a real extraction:
- [ ] pseudonym map complete (or `--auto-pseudonyms`); pseudonyms match `pseudonym_regex`
- [ ] `exclude_series_regex` set for the site's test / do-not-send series
- [ ] engine: 0 studies failed, `patients.csv` n_problems = 0 everywhere
- [ ] `dicom-cloak-check --require-remapped-uids`: PASS
- [ ] dcm2niix before/after comparison on a sample of real series
- [ ] one person views one MIP / derived map / screenshot per protocol for burned-in text
- [ ] originals deleted or access-restricted after acceptance

## 10. Validation results (version 1.0.0)
- Tests: 39 pass in the `ctp` image (Python 3.11, pydicom 3.0.2, dcm2niix 1.0.20260724, Temurin 25 +
  DicomAnonymizerTool); 35 pass + 4 CTP tests skipped natively and in the `python` image.
- Docker images built and tested on linux/amd64. linux/arm64 is supported by all dependencies but has
  not been run on Apple Silicon hardware yet.
- Launchers: `cloak.sh` tested end to end on Linux (Python and CTP engine, paths with spaces, repeated
  runs with stable pseudonyms) and under bash 3.2 (the macOS version). `cloak.ps1` tested under
  PowerShell 7 with a stub Docker; it has not yet been run on Windows with Docker Desktop.
- Python engine, public TCIA data: 19 studies from ACRIN-6698/BMMR2, AMBL, QIN-BREAST-02 and EA1141
  (GE Signa HDxt / Optima MR450w / Discovery MR750; Philips Achieva / Ingenia sw 3.2-5.1; Siemens
  Avanto B17 / Espree / Verio B19 / Aera E11; plus derived CAD series): 0 failures, checker PASS on
  31,123 files, 18/19 studies dcm2niix-identical (the remaining one contains a series that dcm2niix
  mis-assembles from the originals too). Duke-Breast-Cancer-MRI scanner subset (11 patients, 8,854
  files): 11/11 identical.
- CTP engine, public data: 26 patients, 33 studies, 47,721 files: 0 failures, checker PASS,
  dcm2niix-identical for 25/26 patients (same known dcm2niix case). Engine comparison: identical apart
  from UIDs and SpecificCharacterSet `ISO_IR 100`, which CTP inserts where it was missing.
- Siemens XA/XB: one original Vida XB10 export: 0 leak hits after the protocol scrubber, all volumes
  dcm2niix-identical.
- Multi-visit: chronological visit numbers, days since the first visit, ClinicalTrialTimePointID and
  `--study-registry` across runs, tested synthetically and on a real 3-visit patient.
- CTP server pipeline (`ctp/config.xml`): directory import tested headless on synthetic data; checker
  PASS; a non-MR object was quarantined by the filter.

## 11. Known limitations
- Burned-in text in pixel data (screenshots, secondary captures, annotated MIPs) is not detected; only
  MR and Enhanced MR objects are kept, but a person must still look at sample images.
- Public test data is pre-anonymized (Siemens CSA headers stripped). Not yet confirmed on raw vendor
  originals: the classic CSA series-header scrubber on VB/VD/VE data; Siemens XA Enhanced data with all
  dynamics inside one object; the TCIA/PS3.15 private entries on raw Philips/GE originals.
- Dynamics stored as frames of ONE Enhanced MR object get one time offset per file, not per frame.
- Big-endian (retired) transfer syntaxes are untested. Implicit VR, deflated (written inflated) and
  RLE / encapsulated input are tested.
- CTP engine: nested TM attributes are removed rather than relative-clocked; multi-valued UIDs are
  hashed as one string; private sequences read as UN are kept as raw bytes. None of these was seen in
  practice. DicomAnonymizerTool drops the pixel data of deflated files, so the pre-pass stages them
  inflated and the post-pass fails any image that comes back without pixel data.
- CTP server pipeline over the DICOM network (C-STORE into port 1104) is configured but has only been
  tested with the directory import.
- The national-ID rule in `check.py` implements the Polish PESEL checksum only; add other countries' IDs
  there if needed.
- Siemens b-values that survive only in SequenceName (e.g. `*ep_b800t`, when CSA and standard diffusion
  tags are missing) are not recovered.
