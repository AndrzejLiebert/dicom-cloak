# Sources and how each was used

Everything below is public. Re-check versions when upgrading. `tools/xref_private_lists.py`
reproduces the private-tag cross-reference from the raw sources.

## Conversion requirements (what must survive)
- **dcm2niix** (github.com/rordenlab/dcm2niix), last checked at commit 8282dbf (2026-07-24);
  `console/nii_dicom.cpp` and `console/nii_dicom_batch.cpp`.
  - All vendor private tags dcm2niix reads were taken from `nii_dicom.cpp`, restricted to acquisition,
    diffusion, timing, geometry and scaling. This list is the first source of
    `config/vendor_private.yaml`.
  - Specifics that mattered:
    - Philips PrepulseDelay is 2001,xx1B, not 0x09. Both are kept now: 0x09 is the image-level
      variant from PS3.15/TCIA.
    - GE b-values are only accepted when InternalPulseSequenceName (0019,109E) is EPI2.
    - Siemens XA ImaPATModeText (0051,1011) is needed, or ParallelReductionFactorInPlane is wrong.
    - dcm2niix reads some GE tags at fixed block 0x10, e.g. 0043,1039.
  - dcm2niix skips the first byte of a text protocol blob (`phoenixOffsetCSASeriesHeader` returns
    EXIT_FAILURE=1, which is used as an offset). That is why the scrubbed XA protocol starts with
    `<XProtocol>\n`.
  - The ASCCONV keys dcm2niix reads (`siemensCsaAscii`) defined `ASCCONV_KEEP_*` in `scrub.py`.
- PyPI wheel `dcm2niix` v1.0.20260724 was used in the tests.

## Published safe-private lists (second source for the vendor allowlist)
- **DICOM PS3.15 Annex E, Table E.3.10-1 "Safe Private Attributes"**:
  https://dicom.nema.org/medical/dicom/current/output/chtml/part15/sect_E.3.10.html (2026c edition).
  - It is a sample list; vendors do not guarantee it. 479 rows were parsed from the HTML table.
  - MR-relevant content:
    - Philips Imaging DD 001 (184 entries, many of them presentation-related);
    - Philips MR Imaging DD 001: 2005,xx0D/0E (scale intercept/slope);
    - SIEMENS MR HEADER: 0019,xx0C/0D/0E/27 (diffusion);
    - GEMS_ACQU_01: 0019,xx9E; GEMS_PARM_01: 0043,xx39/6F; GEMS_SERS_01: 0025,xx07.
  - Related change proposals: CP-2017 (Philips additions), CP-1672 (Siemens ultrasound).
- **TCIA private tag knowledge base**: https://wiki.cancerimagingarchive.net/display/Public/De-identification+Knowledge+Base
  It is now included in the Posda Private Tag Dictionary. Raw data is in github.com/UAMS-DBMI/PosdaTools,
  file `posda/posdatools/migrations/private_tag_kb/data.sql`.
  - Use the `INSERT INTO pt (...)` rows; the column `pt_consensus_disposition` holds K, KB, R, X, K3
    or Z.
  - 11,166 rows. Counts by vendor creator:

    | Vendor | K | KB | R | X | no disposition |
    |---|---|---|---|---|---|
    | GE | 356 | 215 | 221 | 116 | 1,056 |
    | Philips | 10 | 26 | 16 | 15 | 1,900 |
    | Siemens | 134 | 3 | 17 | 42 | 2,964 |

  - TCIA marks the Siemens CSA headers X and several GE diffusion/user-data fields R.
- **RSNA CTP bundled dictionary**: `source/resources/PrivateTagDictionary.xml` in github.com/johnperry/CTP.
  This is what `keep.safeprivateelements` uses. It has 1,002 entries, coded K 500, R 195, X 182,
  KB 122. It is mostly GE CT/MR; Siemens entries are only X/R/KB, there is 1 Philips entry, and it has
  no Siemens or Philips MR entries coded K. It is not used.
- Selection rule applied: add TCIA K/KB and PS3.15 entries only if they are MR
  acquisition parameters with numeric or CS values. That added 30 Philips and 118 GE entries, marked
  `src:`. CT detector, X-ray, presentation-state, window/level, biopsy, free-text and SQ entries were
  skipped (e.g. Philips StackSequence).

## CTP / DicomAnonymizerTool
- Releases: github.com/johnperry/CTPTools25 (Java 25 builds), commit
  7db432fe9fd5a6f25df6926f091af8f6d0c8c18a (2025-10-31). SHA-256:
  - CTP-installer.jar: e979e04c725650677babbfa9dff2bca4b968faf6fcfdc002bd75a167ff88a489
  - DicomAnonymizerTool-installer.jar: a4ea937f215ac14f25409ae331e0cc18910769b6b888e6d75167391aa829eee1
  - The CTP.jar manifest is dated 2025.10.31, Java-Version 25.
- Source code: github.com/johnperry/CTP (master dc89901 "Update to build on Java 25", 2025-10-31),
  github.com/johnperry/DicomAnonymizerTool, github.com/johnperry/Util. Files read to confirm behaviour:
  - `stdstages/anonymizer/dicom/DICOMAnonymizer.java`: processElements order (rpg → rue → overlays →
    script), always-kept groups 0028/0002, function table, `@hashuid`, `@lookup` actions
    (keep/remove/empty/default/ignore), DeIdentificationMethodCodeSeq `reset`;
  - `DICOMAnonymizerContext.java`: script keys, private `gggg[creator]ee` resolution against the root
    dataset, `remove.unspecifiedelements`;
  - `objects/DicomObject.java` `getPrivateElementTag`: exact, case-sensitive creator match; the creator
    element itself cannot be addressed;
  - `anonymizer/dicom/FnCall.java`: the backslash escape is consumed inside arguments; `{}` clauses;
  - `stdstages/DicomAnonymizer.java`: **SKIP passes the input object on unchanged**;
  - `stdstages/DicomFilter.java`: non-matching objects are quarantined and not passed on;
  - `stdstages/DirectoryStorageService.java`: `structure` uses `{Keyword}` / `(gggg,eeee)`
    placeholders; the default file name is SOPInstanceUID;
  - `util/DigestUtil.java` `getUSMD5`: decimal of the unsigned MD5;
  - `dicomanonymizertool/DicomAnonymizerTool.java`: CLI (-in -out -f -da -lut -p -e -n -v); it always
    exits 0 and can hang after the last file.
- MIRC wiki documentation: https://mircwiki.rsna.org/index.php?title=The_CTP_DICOM_Anonymizer ,
  …=The_DICOM_Anonymizer_Keep_Safe_Private_Elements_Feature , …=The_CTP_DICOM_Filter ,
  …=CTP_Articles . The wiki says creator matching is not case-sensitive; the current code compares
  exactly.
- Java runtime: Eclipse Temurin 25 JRE. Tested 25.0.5+8, from
  https://api.adoptium.net/v3/binary/latest/25/ga/linux/x64/jre/hotspot/normal/eclipse ; Docker image
  `eclipse-temurin:25-jre`.

## Public test data used for validation (TCIA, all already de-identified by TCIA)
- **ACRIN-6698 / I-SPY2 (BMMR2)**: GE, Philips and Siemens DCE + DWI, multiple visits per patient.
  Contains the series type that dcm2niix mis-assembles even from the originals (Siemens fl3d1 with
  InstanceNumber repeated per phase, e.g. series 61800/71800).
- **Advanced-MRI-Breast-Lesions (AMBL)**: GE.
- **QIN-BREAST-02**: Philips sagittal DCE.
- **EA1141 (abbreviated breast MRI)**: GE Optima MR450w, Philips Achieva, Siemens Espree.
- **Duke-Breast-Cancer-MRI**: 11 patients picked one per scanner/field strength (GE SIGNA
  EXCITE/HDx/HDxt/Optima, Siemens Trio/TrioTim/Avanto/Skyra). License CC BY-NC.
- **ODELIA**: NIfTI only, so not usable for DICOM tests.

Because TCIA data is pre-anonymized (CSA headers stripped, dates shifted), it tests conversion
fidelity and residual leaks but not raw private tags. Raw vendor data is needed for that (see Known limitations in `docs/TECHNICAL.md`).

## Other
- OsiriX/Horos anonymization presets: a format description exists at
  http://chestradiologists.org/AddAnonScheme.rtf . Viewer-based exports should be re-run through this
  tool, because viewer presets typically keep nested sequences, private tags and non-image objects.
- PESEL checksum, the national-ID rule in `check.py`: weights 1,3,7,9,1,3,7,9,1,3; check digit
  (10 − sum mod 10) mod 10. Add other national IDs there if needed.
