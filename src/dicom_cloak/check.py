"""dicom-cloak-check: independent leak check of a DICOM folder (headers only; prints field names, never values).

  dicom-cloak-check FOLDER [--config config.yaml] [--require-remapped-uids] [--exclude-series-regex RX ...]

Rules (each reported as FAIL <RULE>: <n> locations):
  NATIONAL_ID  number with a valid national-ID checksum in any text field (PESEL implemented; add others)
  PN           person-name field other than PatientName
  PSEUDONYM    PatientName/PatientID not matching pseudonym_regex
  DATE         DA value (other than a YYYY0101 placeholder);  DATETIME: DT longer than the year
  TIME         TM value (time of day)
  UID_ORIG     UID with a vendor root (Siemens/GE/Philips) or an embedded timestamp; with
               --require-remapped-uids: any instance UID that is not 2.25.<n>
  NOT_ALLOWED  (allowlist mode) standard attribute or sequence not in standard_keep.yaml, at any depth
  FIELD        attribute from standard_remove.yaml present and non-empty (also inside sequences)
  PRIVATE      private element not in the vendor whitelist (output block of this tool allowed)
  NON_IMAGE    SOP class other than MR / Enhanced MR (reports, presentation states, KOS, screenshots)
  EXCLUDED     series matching an exclude regex
  NON_DICOM    non-DICOM files (viewer databases, thumbnails, OS metadata files)
Exit code 1 if anything is found.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict

import pydicom
from pydicom.errors import InvalidDicomError

from . import engine

VENDOR_UID_ROOTS = ("1.3.12.2.1107.", "1.2.840.113619.", "1.3.46.670589.")  # Siemens, GE, Philips
TIMESTAMP_IN_UID = re.compile(r"(?<!\d)(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])([01]\d|2[0-3])[0-5]\d[0-5]\d")
NUM11 = re.compile(r"(?<![\d.])\d{11}(?![\d.])")  # not decimal fractions of numeric values
TEXT_VRS = {"LO", "SH", "LT", "ST", "UT", "PN", "CS", "IS", "DS", "UC", "AE"}


def pesel_valid(s: str) -> bool:
    w = [1, 3, 7, 9, 1, 3, 7, 9, 1, 3]
    return (10 - sum(int(a) * b for a, b in zip(s, w)) % 10) % 10 == int(s[10])


def scan(ds, where, hits, policy, strict_uids, top=True):
    for e in ds:
        kw = e.keyword or str(e.tag)
        if e.tag.is_private:
            if top and e.tag.group == policy.output_private_group:
                continue
            if e.tag.element >= 0x0100 and not engine.is_whitelisted_private(ds, e, policy):
                hits["PRIVATE"].add(f"{where} :: {e.tag}")
            if e.VR != "SQ":
                continue
        if not e.tag.is_private and not policy.allowed(e) and not (top and kw in engine.ENGINE_WRITTEN):
            hits["NOT_ALLOWED"].add(f"{where} :: {kw}")
        if kw in policy.remove_keywords and kw not in engine.REWRITTEN_KEYWORDS and (
                (e.VR == "SQ" and len(e.value)) or (e.VR != "SQ" and str(e.value).strip())):
            hits["FIELD"].add(f"{where} :: {kw}")
        if e.VR == "SQ":
            for item in e.value:
                scan(item, where, hits, policy, strict_uids, top=False)
            continue
        for v in engine._values(e):
            s = str(v).strip()
            if not s:
                continue
            if e.VR == "PN" and not (top and kw == "PatientName"):
                hits["PN"].add(f"{where} :: {kw}")
            elif e.VR == "DA" and not s.endswith("0101"):
                hits["DATE"].add(f"{where} :: {kw}")
            elif e.VR == "TM" and not (policy.relative_clock and kw in policy.relative_tm):
                hits["TIME"].add(f"{where} :: {kw}")
            elif e.VR == "DT" and len(s) > 4:
                hits["DATETIME"].add(f"{where} :: {kw}")
            elif e.VR == "UI" and not s.startswith(engine.STANDARD_UID_ROOT) and kw not in engine.KEEP_UI_KEYWORDS:
                if s.startswith(VENDOR_UID_ROOTS) or (not s.startswith("2.25.") and TIMESTAMP_IN_UID.search(s)) \
                        or (strict_uids and not s.startswith("2.25.")):
                    hits["UID_ORIG"].add(f"{where} :: {kw}")
            if e.VR in TEXT_VRS:
                if any(pesel_valid(m) for m in NUM11.findall(s)):
                    hits["NATIONAL_ID"].add(f"{where} :: {kw}")
    if top:
        for kw in ("PatientName", "PatientID"):
            if not policy.pseudonym_regex.match(str(ds.get(kw, ""))):
                hits["PSEUDONYM"].add(f"{where} :: {kw}")


def check_folder(folder, policy, strict_uids=False, exclude=()):
    hits, n = defaultdict(set), 0
    exclude = [re.compile(r, re.I) for r in exclude] + policy.exclude_series_regex
    for root, _, files in os.walk(folder):
        rel = os.path.relpath(root, folder)
        for f in files:
            p = os.path.join(root, f)
            if rel.split(os.sep)[0] == "_deid_meta":
                continue
            try:
                ds = pydicom.dcmread(p, stop_before_pixels=True)
            except (InvalidDicomError, OSError, ValueError):
                hits["NON_DICOM"].add(os.path.join(rel, f))
                continue
            n += 1
            desc = str(ds.get("SeriesDescription", ""))
            if any(rx.search(desc) for rx in exclude):
                hits["EXCLUDED"].add(rel)
            if str(ds.get("SOPClassUID", "")) not in policy.keep_sop_classes:
                hits["NON_IMAGE"].add(f"{rel} :: {getattr(ds.get('SOPClassUID'), 'name', '?')}")
            scan(ds, rel, hits, policy, strict_uids)
    return n, hits


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dicom-cloak-check", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--config", default=os.environ.get("DICOM_CLOAK_CONFIG", "/opt/dicom_cloak/config/config.yaml"))
    ap.add_argument("--require-remapped-uids", action="store_true")
    ap.add_argument("--exclude-series-regex", action="append", default=[])
    ap.add_argument("--show", type=int, default=5)
    a = ap.parse_args(argv)
    policy = engine.load_policy(a.config)
    n, hits = check_folder(a.folder, policy, a.require_remapped_uids, a.exclude_series_regex)
    print(f"DICOM files scanned: {n}")
    if not hits:
        print("PASS: no leaks found")
        return 0
    for rule in sorted(hits):
        locs = sorted(hits[rule])
        print(f"FAIL {rule}: {len(locs)} locations")
        for loc in locs[:a.show]:
            print(f"    {loc}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
