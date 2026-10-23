"""Input variants (transfer syntaxes) and the run options a site uses day to day (automatic pseudonyms,
secret files kept out of the output)."""
import csv
import os
import sys

import numpy as np
import pydicom
import pytest
from pydicom.uid import DeflatedExplicitVRLittleEndian, ExplicitVRLittleEndian, ImplicitVRLittleEndian, RLELossless

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)

from dicom_cloak import check, cli, engine  # noqa: E402
from conftest import CONFIG  # noqa: E402
import synthetic as syn  # noqa: E402


def _files(d):
    return sorted(os.path.join(r, f) for r, _, fs in os.walk(d) for f in fs if f.endswith(".dcm"))


def _run(src, dst, tmp, *extra):
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--src", str(src), "--dst", str(dst), "--config", CONFIG, "--uid-key", str(tmp / "k.hex"),
                  "--workers", "1", *extra])
    return e.value.code


def _header_bytes(path):
    with open(path, "rb") as fh:
        pydicom.dcmread(fh, stop_before_pixels=True)
        end = fh.tell()
        fh.seek(0)
        return fh.read(end)


def _convert(src_dir, syntax):
    for p in _files(src_dir):
        ds = pydicom.dcmread(p)
        if syntax == RLELossless:
            ds.compress(RLELossless)
        else:
            ds.file_meta.TransferSyntaxUID = syntax
            ds.save_as(p, implicit_vr=syntax == ImplicitVRLittleEndian, little_endian=True,
                       enforce_file_format=True)
            continue
        ds.save_as(p, enforce_file_format=True)


@pytest.mark.parametrize("syntax", [ImplicitVRLittleEndian, DeflatedExplicitVRLittleEndian, RLELossless],
                         ids=["implicit_vr", "deflated", "rle_lossless"])
def test_transfer_syntaxes(tmp_path, syntax):
    src, dst = tmp_path / "src", tmp_path / "dst"
    syn.make_siemens(str(src / "s"))
    syn.make_philips(str(src / "p"))
    pixels = {}
    for p in _files(src):
        ds = pydicom.dcmread(p)
        pixels[str(ds.SOPInstanceUID)] = ds.pixel_array.copy()
    _convert(src, syntax)
    assert {str(pydicom.dcmread(p, stop_before_pixels=True).file_meta.TransferSyntaxUID) for p in _files(src)} \
        == {syntax}
    mp = tmp_path / "map.csv"
    mp.write_text("original_patient_id,pseudonym\nORIG-S-1,SUBJ_001\nORIG-P-1,SUBJ_002\n")
    assert _run(src, dst, tmp_path, "--pseudonym-map", str(mp)) == 0
    n, hits = check.check_folder(str(dst), engine.load_policy(CONFIG), strict_uids=True)
    assert n == len(pixels) and not hits, dict(hits)
    seen_b_value = False
    for p in _files(dst):
        raw = _header_bytes(p)   # deflated input is written inflated, so this search sees the real bytes
        for leak in ("Doe^Jane", syn.NATIONAL_ID, syn.SERIAL, syn.STATION, "General Hospital", "1.3.12.2.1107"):
            assert leak.encode() not in raw, (leak, os.path.relpath(p, dst))
        ds = pydicom.dcmread(p)
        assert ds.file_meta.TransferSyntaxUID != DeflatedExplicitVRLittleEndian
        # pixel values identical to the original (no lossy step, any transfer syntax)
        assert any(np.array_equal(ds.pixel_array, v) for v in pixels.values())
        seen_b_value |= any(e.tag.is_private and e.tag.group == 0x0019 for e in ds)
    assert seen_b_value, "vendor private b-value lost"


def test_auto_pseudonyms_create_and_extend_map(tmp_path):
    src1, src2 = tmp_path / "in1", tmp_path / "in2"
    syn.make_siemens(str(src1 / "a"))
    syn.make_philips(str(src1 / "b"))
    syn.make_ge(str(src2 / "c"))
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    mp = secrets / "pseudonym_map.csv"
    assert _run(src1, tmp_path / "out1", tmp_path, "--pseudonym-map", str(mp), "--auto-pseudonyms", "SUBJ_") == 0
    first = {r["original_patient_id"]: r["pseudonym"] for r in csv.DictReader(open(mp))}
    assert first == {"ORIG-P-1": "SUBJ_0001", "ORIG-S-1": "SUBJ_0002"}
    assert oct(os.stat(mp).st_mode & 0o777) == "0o600"
    assert sorted(os.listdir(tmp_path / "out1")) == ["SUBJ_0001", "SUBJ_0002", "_deid_meta"]
    # second run: known patients keep their pseudonym, a new one gets the next number
    syn.make_siemens(str(src2 / "a"))
    assert _run(src2, tmp_path / "out2", tmp_path, "--pseudonym-map", str(mp), "--auto-pseudonyms", "SUBJ_") == 0
    second = {r["original_patient_id"]: r["pseudonym"] for r in csv.DictReader(open(mp))}
    assert second == {**first, "ORIG-G-1": "SUBJ_0003"}
    assert sorted(os.listdir(tmp_path / "out2")) == ["SUBJ_0002", "SUBJ_0003", "_deid_meta"]


def test_hand_written_map_with_excel_bom_and_auto_fill(tmp_path):
    src = tmp_path / "in"
    syn.make_siemens(str(src / "a"))
    syn.make_philips(str(src / "b"))
    mp = tmp_path / "map.csv"
    mp.write_bytes("﻿original_patient_id,pseudonym\r\nORIG-S-1,BREAST_17\r\n".encode("utf-8"))
    assert _run(src, tmp_path / "out", tmp_path, "--pseudonym-map", str(mp), "--auto-pseudonyms", "BREAST_") == 0
    assert {r["original_patient_id"]: r["pseudonym"] for r in csv.DictReader(open(mp, encoding="utf-8-sig"))} \
        == {"ORIG-S-1": "BREAST_17", "ORIG-P-1": "BREAST_0018"}


def test_unmapped_patient_without_auto_fails(tmp_path):
    src = tmp_path / "in"
    syn.make_siemens(str(src / "a"))
    mp = tmp_path / "map.csv"
    mp.write_text("original_patient_id,pseudonym\nSOMEONE-ELSE,SUBJ_9\n")
    assert _run(src, tmp_path / "out", tmp_path, "--pseudonym-map", str(mp)) == 1
    assert not _files(tmp_path / "out")


@pytest.mark.parametrize("inside", ["map", "key", "dst_in_src"])
def test_secret_files_never_inside_output(tmp_path, inside):
    src, dst = tmp_path / "in", tmp_path / "out"
    syn.make_ge(str(src))
    mp, key = tmp_path / "map.csv", tmp_path / "k.hex"
    if inside == "map":
        mp = dst / "map.csv"
    if inside == "key":
        key = dst / "k.hex"
    if inside == "dst_in_src":
        dst = src / "out"
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--src", str(src), "--dst", str(dst), "--config", CONFIG, "--uid-key", str(key),
                  "--pseudonym-map", str(mp), "--auto-pseudonyms", "SUBJ_", "--workers", "1"])
    assert e.value.code not in (0, None)
    assert not os.path.exists(mp) and not os.path.exists(key)


def test_map_from_excel_with_semicolons_and_duplicate_check(tmp_path):
    src = tmp_path / "in"
    syn.make_siemens(str(src / "a"))
    mp = tmp_path / "map.csv"
    mp.write_bytes("﻿original_patient_id;pseudonym\r\nORIG-S-1;BREAST_01\r\n;\r\n".encode("utf-8"))
    assert _run(src, tmp_path / "out", tmp_path, "--pseudonym-map", str(mp)) == 0
    assert os.listdir(tmp_path / "out" / "BREAST_01")
    mp.write_text("original_patient_id,pseudonym\nORIG-S-1,BREAST_01\nOTHER-1,BREAST_01\n")
    with pytest.raises(SystemExit, match="more than one patient"):
        cli.main(["run", "--src", str(src), "--dst", str(tmp_path / "out2"), "--config", CONFIG,
                  "--pseudonym-map", str(mp), "--uid-key", str(tmp_path / "k.hex")])
