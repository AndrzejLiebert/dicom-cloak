"""dicom-cloak-report: which standard attributes would the allowlist drop? (headers only, names only)

  dicom-cloak-report FOLDER [--config config.yaml] [--per-series 1]

Reads the first N files of every series folder and prints, per attribute path (Sequence>Keyword) and VR,
how many headers contain it although it is not allowlisted. Never prints values. Use it on real data
from each new vendor/software version to decide whether something technically relevant must be added
to standard_keep.yaml (then confirm with tests/test_conversion.py).
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

import pydicom

from . import engine


def dropped_attributes(folder: str, policy, per_series: int = 1) -> tuple[int, Counter]:
    dropped, n = Counter(), 0

    def walk(ds, path, top):
        for e in ds:
            if e.tag.is_private:
                continue
            name = path + (e.keyword or str(e.tag))
            if not policy.allowed(e) and not (top and e.keyword in engine.ENGINE_WRITTEN):
                dropped[(name, e.VR)] += 1
                continue
            if e.VR == "SQ":
                for it in e.value:
                    walk(it, name + ">", False)

    for root, _, files in os.walk(folder):
        for f in sorted(x for x in files if not x.startswith("."))[:per_series]:
            try:
                ds = pydicom.dcmread(os.path.join(root, f), stop_before_pixels=True)
            except Exception:  # noqa: BLE001
                continue
            if str(ds.get("SOPClassUID", "")) not in policy.keep_sop_classes:
                continue  # objects the engine drops entirely (SEG, SR, PR, KOS, screenshots)
            n += 1
            walk(ds, "", True)
    return n, dropped


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dicom-cloak-report", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--config", default=os.environ.get("DICOM_CLOAK_CONFIG", "/opt/dicom_cloak/config/config.yaml"))
    ap.add_argument("--per-series", type=int, default=1)
    a = ap.parse_args(argv)
    n, dropped = dropped_attributes(a.folder, engine.load_policy(a.config), a.per_series)
    print(f"headers inspected: {n}")
    for (name, vr), c in sorted(dropped.items(), key=lambda x: (-x[1], x[0])):
        print(f"{c:6d}  {vr:2s}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
