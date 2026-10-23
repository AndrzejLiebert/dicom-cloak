"""Regression: dcm2niix output must be technically identical before and after de-identification.

For every source series: convert source and de-identified copy, then compare image shape, affine,
voxel data, bval/bvec and technical BIDS sidecar fields. Skipped if dcm2niix or nibabel is missing.
"""
import json
import os
import shutil
import subprocess
import tempfile

import numpy as np
import pydicom
import pytest

nib = pytest.importorskip("nibabel")
DCM2NIIX = shutil.which("dcm2niix")
pytestmark = pytest.mark.skipif(DCM2NIIX is None, reason="dcm2niix not installed")

TECH_KEYS = ["Modality", "MagneticFieldStrength", "Manufacturer", "ManufacturersModelName", "ImageType",
             "SliceThickness", "SpacingBetweenSlices", "RepetitionTime", "EchoTime", "FlipAngle",
             "PhaseEncodingDirection", "InPlanePhaseEncodingDirectionDICOM", "SeriesNumber", "ProtocolName",
             "SeriesDescription", "SequenceName", "PhilipsRescaleSlope", "PhilipsRescaleIntercept",
             "PhilipsScaleSlope", "UsePhilipsFloatNotDisplayScaling", "DiffusionScheme"]


def _series_dirs(root):
    """{(SeriesDescription, SeriesNumber): folder} for folders holding MR images."""
    out = {}
    for r, _, fs in os.walk(root):
        dcm = [f for f in fs if not f.startswith(".")]
        if not dcm:
            continue
        try:
            ds = pydicom.dcmread(os.path.join(r, sorted(dcm)[0]), stop_before_pixels=True)
        except Exception:  # noqa: BLE001
            continue
        if str(ds.get("SOPClassUID", "")).startswith("1.2.840.10008.5.1.4.1.1.4"):
            out[(str(ds.get("SeriesDescription", "")), int(ds.get("SeriesNumber", 0)))] = r
    return out


def _convert(folder, out):
    os.makedirs(out, exist_ok=True)
    subprocess.run([DCM2NIIX, "-z", "n", "-b", "y", "-ba", "y", "-f", "%s_%e", "-o", out, folder],
                   check=True, capture_output=True)
    return {os.path.splitext(f)[0]: os.path.join(out, f) for f in os.listdir(out)}


def _content_key(json_path):
    """dcm2niix suffixes (_a, _b, ...) for series sharing a SeriesNumber depend on UID sort order, which
    changes with UID remapping: pair outputs by content instead of by file name."""
    j = json.load(open(json_path))
    return (j.get("SeriesNumber"), j.get("SequenceName"), j.get("EchoNumber"), tuple(j.get("ImageType", [])),
            j.get("EchoTime"))


def _pair_by_content(d):
    out = {}
    for f in os.listdir(d):
        if f.endswith(".json"):
            out.setdefault(_content_key(os.path.join(d, f)), []).append(f[:-5])
    return out


def test_dcm2niix_before_after_identical(run_dirs):
    src, dst = run_dirs
    before, after = _series_dirs(str(src)), _series_dirs(str(dst))
    assert set(before) == set(after), "series lost or added by de-identification"
    tmp = tempfile.mkdtemp()
    problems = []
    for key, sdir in sorted(before.items()):
        a = _convert(sdir, os.path.join(tmp, "a", str(key[1])))
        b = _convert(after[key], os.path.join(tmp, "b", str(key[1])))
        d_a, d_b = os.path.dirname(next(iter(a.values()))), os.path.dirname(next(iter(b.values())))
        pa_map, pb_map = _pair_by_content(d_a), _pair_by_content(d_b)
        if set(pa_map) != set(pb_map) or any(len(v) != 1 for v in list(pa_map.values()) + list(pb_map.values())):
            problems.append(f"{key}: outputs cannot be paired by content {sorted(pa_map)} vs {sorted(pb_map)}")
            continue
        pairs = []
        for ck in pa_map:
            stem_a, stem_b = pa_map[ck][0], pb_map[ck][0]
            for ext in (".nii", ".json", ".bval", ".bvec"):
                if os.path.exists(os.path.join(d_a, stem_a + ext)) != os.path.exists(os.path.join(d_b, stem_b + ext)):
                    problems.append(f"{key} {stem_a}{ext}: present on one side only")
                elif os.path.exists(os.path.join(d_a, stem_a + ext)):
                    pairs.append((stem_a + ext, os.path.join(d_a, stem_a + ext), os.path.join(d_b, stem_b + ext)))
        for fn, pa, pb in pairs:
            if fn.endswith(".nii"):
                ia, ib = nib.load(pa), nib.load(pb)
                if ia.shape != ib.shape:
                    problems.append(f"{key} {fn}: shape {ia.shape} vs {ib.shape}")
                if not np.allclose(ia.affine, ib.affine, atol=1e-4):
                    problems.append(f"{key} {fn}: affine differs")
                if not np.array_equal(np.asanyarray(ia.dataobj), np.asanyarray(ib.dataobj)):
                    problems.append(f"{key} {fn}: voxel data differ")
            elif fn.endswith((".bval", ".bvec")):
                if open(pa).read() != open(pb).read():
                    problems.append(f"{key} {fn}: {fn[-4:]} differs")
            elif fn.endswith(".json"):
                ja, jb = json.load(open(pa)), json.load(open(pb))
                for k in TECH_KEYS:
                    if ja.get(k) != jb.get(k):
                        problems.append(f"{key} {fn}: sidecar {k} {ja.get(k)!r} -> {jb.get(k)!r}")
    shutil.rmtree(tmp, ignore_errors=True)
    assert not problems, "\n".join(problems)
