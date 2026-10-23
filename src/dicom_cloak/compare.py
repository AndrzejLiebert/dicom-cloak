"""dicom-cloak-compare: is the de-identified study technically identical after dcm2niix conversion?

  dicom-cloak-compare ORIGINAL_STUDY_DIR DEIDENTIFIED_STUDY_DIR [--keep-tmp]

Converts both trees with dcm2niix and compares every output volume. Outputs are matched by content,
not by file name (dcm2niix suffixes depend on UID order, which changes with UID remapping):
  voxel-data hash + shape + affine + bval/bvec + technical sidecar fields.
Prints counts and the technical differences only (no values from identifying fields, no images).
Exit code 1 if any original volume has no identical counterpart.
Requires dcm2niix on PATH and nibabel.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

# Equipment names do not affect the images and can legitimately change: dcm2niix reads nested values,
# e.g. from a ContributingEquipmentSequence an earlier anonymizer added (then the ORIGINAL sidecar is wrong).
INFO_KEYS = ["Manufacturer", "ManufacturersModelName", "SoftwareVersions"]
TECH_KEYS = ["Modality", "MagneticFieldStrength", "ImageType",
             "SliceThickness", "SpacingBetweenSlices", "RepetitionTime", "EchoTime", "FlipAngle", "InversionTime",
             "PhaseEncodingDirection", "PhaseEncodingAxis", "InPlanePhaseEncodingDirectionDICOM", "SeriesNumber",
             "ProtocolName", "SeriesDescription", "SequenceName", "ScanningSequence", "SequenceVariant",
             "PhilipsRescaleSlope", "PhilipsRescaleIntercept", "PhilipsScaleSlope", "UsePhilipsFloatNotDisplayScaling",
             "DiffusionScheme", "SliceTiming", "EffectiveEchoSpacing", "TotalReadoutTime", "ParallelReductionFactorInPlane",
             "PixelBandwidth", "ReceiveCoilName", "EchoTrainLength", "PercentPhaseFOV", "AcquisitionMatrixPE",
             "ReconMatrixPE", "MRAcquisitionType", "ScanOptions", "TemporalPositionIdentifier"]


def convert(src, out):
    os.makedirs(out, exist_ok=True)
    r = subprocess.run(["dcm2niix", "-z", "n", "-b", "y", "-ba", "y", "-f", "%s_%e_%z", "-o", out, src],
                       capture_output=True, text=True)
    return r.returncode


def kept_tree(orig_dir, config, tmp):
    """Symlink tree of the original files the engine would keep (SOP class + series exclusion), so that
    deliberately excluded objects (SEG, SR, screenshots, ...) are not reported as missing."""
    import re

    import pydicom
    from . import engine
    pol = engine.load_policy(config)
    out = os.path.join(tmp, "orig_kept")
    n = 0
    for r, _, fs in os.walk(orig_dir):
        for f in fs:
            p = os.path.join(r, f)
            try:
                ds = pydicom.dcmread(p, stop_before_pixels=True, specific_tags=["SOPClassUID", "SeriesDescription",
                                                                                 "ProtocolName"])
            except Exception:  # noqa: BLE001
                continue
            desc = str(ds.get("SeriesDescription", "") or ds.get("ProtocolName", ""))
            if str(ds.get("SOPClassUID", "")) not in pol.keep_sop_classes or any(
                    rx.search(desc) for rx in pol.exclude_series_regex):
                continue
            dst = os.path.join(out, os.path.relpath(p, orig_dir))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.symlink(os.path.abspath(p), dst)
            n += 1
    return out, n


def _load_json(path):
    try:
        return json.load(open(path)), None
    except json.JSONDecodeError as e:   # dcm2niix occasionally writes invalid JSON (e.g. empty arrays/values)
        return {}, f"invalid sidecar JSON ({e.msg} at line {e.lineno})"


def volumes(d):
    import nibabel as nib
    out = []
    for f in sorted(os.listdir(d)):
        if not f.endswith(".nii"):
            continue
        stem = f[:-4]
        img = nib.load(os.path.join(d, f))
        data = np.asanyarray(img.dataobj)
        side, jerr = {}, None
        if os.path.exists(os.path.join(d, stem + ".json")):
            side, jerr = _load_json(os.path.join(d, stem + ".json"))
        extra = {ext: open(os.path.join(d, stem + ext)).read() for ext in (".bval", ".bvec")
                 if os.path.exists(os.path.join(d, stem + ext))}
        out.append({"file": f, "shape": tuple(data.shape), "affine": np.round(img.affine, 4),
                    "hash": hashlib.sha256(np.ascontiguousarray(data).tobytes()).hexdigest(),
                    "dtype": str(data.dtype), "tech": {k: side.get(k) for k in TECH_KEYS},
                    "info": {k: side.get(k) for k in INFO_KEYS}, "extra": extra,
                    "json_error": jerr})
    return out


def _close_lists(a, b, tol):
    return (isinstance(a, list) and isinstance(b, list) and len(a) == len(b)
            and all(abs(x - y) <= tol for x, y in zip(a, b)))


def compare(orig_dir, deid_dir, keep_tmp=False, config=None):
    tmp = tempfile.mkdtemp(prefix="deid_cmp_")
    try:
        if config:
            orig_dir, _ = kept_tree(orig_dir, config, tmp)
        convert(orig_dir, os.path.join(tmp, "a"))
        convert(deid_dir, os.path.join(tmp, "b"))
        va, vb = volumes(os.path.join(tmp, "a")), volumes(os.path.join(tmp, "b"))
        by_hash = {}
        for v in vb:
            by_hash.setdefault(v["hash"], []).append(v)
        problems, matched, notes = [], 0, set()
        for v in va:
            cands = by_hash.get(v["hash"], [])
            if not cands and "_ROI" in v["file"]:
                notes.add("info (not a failure): overlay ROI planes are removed by design (60xx overlays)")
                matched += 1
                continue
            if not cands:
                problems.append(f"{v['file']}: no de-identified volume with identical voxel data "
                                f"(SeriesNumber {v['tech'].get('SeriesNumber')}, {v['tech'].get('SeriesDescription')})")
                continue
            best = min(cands, key=lambda c: sum(c["tech"][k] != v["tech"][k] for k in TECH_KEYS))
            cands.remove(best)
            matched += 1
            if best["shape"] != v["shape"] or not np.allclose(best["affine"], v["affine"], atol=1e-3):
                problems.append(f"{v['file']}: geometry differs")
            for ext in set(v["extra"]) | set(best["extra"]):
                if v["extra"].get(ext) != best["extra"].get(ext):
                    problems.append(f"{v['file']}: {ext} differs")
            for side, vol in (("original", v), ("de-identified", best)):
                if vol["json_error"]:
                    problems.append(f"{vol['file']}: {side} {vol['json_error']}")
            for k in INFO_KEYS:
                if v["info"][k] != best["info"][k]:
                    notes.add(f"info (not a failure): sidecar {k} {v['info'][k]!r} -> {best['info'][k]!r}")
            for k in TECH_KEYS:
                if k == "SliceTiming" and _close_lists(v["tech"][k], best["tech"][k], 0.020):
                    continue  # dcm2niix keeps HHMMSS in float32: rounding up to ~12 ms depends on the clock value (larger real clock = coarser)
                if v["tech"][k] != best["tech"][k]:
                    problems.append(f"{v['file']}: sidecar {k}: {v['tech'][k]!r} -> {best['tech'][k]!r}")
        unmatched_b = sum(len(c) for c in by_hash.values())
        return {"original_volumes": len(va), "deid_volumes": len(vb), "matched": matched,
                "extra_deid_volumes": unmatched_b, "problems": problems, "notes": sorted(notes)}
    finally:
        if not keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dicom-cloak-compare", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("original")
    ap.add_argument("deidentified")
    ap.add_argument("--keep-tmp", action="store_true")
    ap.add_argument("--config", default=os.environ.get("DICOM_CLOAK_CONFIG"),
                    help="policy: compare only originals the engine keeps (SOP class, excluded series)")
    a = ap.parse_args(argv)
    if shutil.which("dcm2niix") is None:
        sys.exit("dcm2niix not found on PATH")
    r = compare(a.original, a.deidentified, a.keep_tmp, a.config)
    print(f"original volumes: {r['original_volumes']}  de-identified volumes: {r['deid_volumes']}  "
          f"matched identical: {r['matched']}  extra in de-identified: {r['extra_deid_volumes']}")
    for p in r["problems"]:
        print("  " + p)
    for n in r.get("notes", []):
        print("  " + n)
    ok = not r["problems"] and r["matched"] == r["original_volumes"] and r["extra_deid_volumes"] == 0
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
