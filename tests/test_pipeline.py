"""End-to-end tests on synthetic Siemens / Philips / GE studies (run: pytest -q tests)."""
import csv
import os
import sys

import pydicom
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

from dicom_cloak import check, engine  # noqa: E402
from conftest import CONFIG  # noqa: E402
import synthetic as syn  # noqa: E402

LEAKS = [syn.NATIONAL_ID, syn.HOSPITAL_ID, syn.ACCESSION, "Smith-Jones", syn.SERIAL, syn.STATION,
         "General Hospital", "Main St", "C50", "Doe^Jane", "ORIG-", syn.DATE, "1.3.12.2.1107", "1.3.46.670589",
         "1.2.840.113619", "breast tumour", "reformatted for"]


def _files(d):
    return [os.path.join(r, f) for r, _, fs in os.walk(d) for f in fs if f.endswith(".dcm")]


def test_pesel_fixture_is_valid():
    assert check.pesel_valid(syn.NATIONAL_ID)


def test_no_planted_identifier_in_any_output_header(run_dirs):
    _, dst = run_dirs
    for p in _files(dst):
        with open(p, "rb") as fh:
            pydicom.dcmread(fh, stop_before_pixels=True)
            end = fh.tell()
            fh.seek(0)
            raw = fh.read(end)
        for leak in LEAKS:
            assert leak.encode() not in raw, f"{leak!r} leaked into {os.path.relpath(p, dst)}"


def test_output_filenames_are_neutral(run_dirs):
    _, dst = run_dirs
    assert not [p for p in _files(dst) if "Doe" in p or "Jane" in p]


def test_independent_checker(run_dirs):
    src, dst = run_dirs
    policy = engine.load_policy(CONFIG)
    n, hits = check.check_folder(str(dst), policy, strict_uids=True)
    assert n > 0 and not hits, dict(hits)
    _, hits_src = check.check_folder(str(src), policy)
    for rule in ("NATIONAL_ID", "PN", "DATE", "UID_ORIG", "FIELD", "NON_IMAGE", "NON_DICOM"):
        assert rule in hits_src, f"checker missed {rule} in the raw input"


def test_pixel_data_unchanged_and_frame_of_reference_shared(run_dirs):
    src, dst = run_dirs
    src_pix = {pydicom.dcmread(p).PixelData for p in _files(src) if os.path.basename(os.path.dirname(p)) != "sr"}
    fors = {}
    for p in _files(dst):
        ds = pydicom.dcmread(p)
        assert ds.PixelData in src_pix
        fors.setdefault((ds.PatientID, ds.StudyInstanceUID), set()).add(ds.FrameOfReferenceUID)  # one per visit
    assert all(len(v) == 1 and next(iter(v)).startswith("2.25.") for v in fors.values()), fors


def test_vendor_whitelist(run_dirs):
    _, dst = run_dirs
    seen = {"siemens_b": False, "csa": False, "philips_scale": False, "ge_b": False, "xa_scrubbed": False}
    for p in _files(dst):
        ds = pydicom.dcmread(p)
        groups = {(e.tag.group, engine.private_creator_of(ds, e.tag)) for e in ds if e.tag.is_private
                  and e.tag.element >= 0x0100}
        seen["siemens_b"] |= (0x0019, "SIEMENS MR HEADER") in groups
        seen["csa"] |= (0x0029, "SIEMENS CSA HEADER") in groups
        seen["philips_scale"] |= (0x2005, "Philips MR Imaging DD 001") in groups
        seen["ge_b"] |= (0x0043, "GEMS_PARM_01") in groups
        for e in ds:
            if e.tag.is_private and e.tag.group == 0x0021 and engine.private_creator_of(ds, e.tag) == "SIEMENS MR SDS 01":
                txt = bytes(e.value).decode("latin-1")
                assert "Doe" not in txt and syn.SERIAL not in txt and "sPat.lAccelFactPE = 2" in txt
                seen["xa_scrubbed"] = True
        assert (0x0009, "GEMS_IDEN_01") not in groups and (0x0051, "SIEMENS MR HEADER") not in groups
    assert all(seen.values()), seen


def test_joint_timing(run_dirs):
    _, dst = run_dirs
    rows = list(csv.DictReader(open(os.path.join(dst, "_deid_meta", "series_timing.csv"))))
    s = {r["series_folder"]: r for r in rows if r["pseudonym"] == "SUBJ_001"}
    get = lambda part: next(r for k, r in s.items() if part in k)  # noqa: E731
    assert get("native")["temporal_positions"] == "1" and get("native")["seconds_after_reference_start"] == "0.0"
    f, w = get("TTC=20.0s_F"), get("TTC=20.0s_W")
    assert f["temporal_positions"] == w["temporal_positions"] == "2", "Dixon F/W must share one position"
    assert f["series_name_tags"] == "TTC_s=20.0"
    assert get("0020_")["temporal_positions"] == "3" and get("0021_")["temporal_positions"] == "4"
    assert get("TTP")["role"] == "derived_map" and get("TTP")["temporal_positions"] == ""
    assert get("0020_")["n_temporal_positions"] == "4"
    p = next(r for r in rows if r["pseudonym"] == "SUBJ_002")
    assert p["temporal_positions"] == "1 2 3" and p["seconds_after_reference_end"] == "140.5"
    assert p["time_source"] == "FrameAcquisitionDateTime", "constant AcquisitionTime must not be used"


def test_patient_table(run_dirs):
    _, dst = run_dirs
    rows = {r["pseudonym"]: r for r in csv.DictReader(open(os.path.join(dst, "_deid_meta", "patients.csv")))
            if r["study"] == "1"}
    assert set(rows) == {"SUBJ_001", "SUBJ_002", "SUBJ_003", "SUBJ_004", "SUBJ_005"}
    assert all(r["age_group"] == "41-50" and r["acquisition_year"] == "2025" and r["n_problems"] == "0"
               for r in rows.values())
    excl = list(csv.DictReader(open(os.path.join(dst, "_deid_meta", "excluded.csv"))))
    assert any("SOP class" in r["reason"] for r in excl)


def test_age_bins():
    b = [18, 30, 40, 50, 60, 70, 80]
    assert [engine.age_group(a, b) for a in ("017Y", "018Y", "030Y", "031Y", "050Y", "080Y", "081Y", "006M", "")] \
        == ["<18", "18-30", "18-30", "31-40", "41-50", "71-80", "81+", "<18", "unknown"]


def test_allowlist_removes_unlisted_identifiers_that_blocklist_misses(run_dirs, tmp_path):
    """DerivationDescription / AdmittingDiagnosesDescription / per-frame FrameComments are in no removal
    list. Allowlist mode drops them; blocklist mode would keep at least the free-text one."""
    src, dst = run_dirs
    for p in _files(dst):
        ds = pydicom.dcmread(p, stop_before_pixels=True)
        assert "DerivationDescription" not in ds and "AdmittingDiagnosesDescription" not in ds
        for f in ds.get("PerFrameFunctionalGroupsSequence", []):
            assert all("FrameComments" not in fc for fc in f.get("FrameContentSequence", []))
    # same input in legacy blocklist mode: the free-text derivation note survives
    import yaml
    cfg = yaml.safe_load(open(CONFIG))
    cfg["mode"] = "blocklist"
    for k in ("standard_keep", "standard_remove", "vendor_private"):
        cfg[k] = os.path.join(os.path.dirname(CONFIG), cfg[k])
    bl = tmp_path / "blocklist.yaml"
    bl.write_text(yaml.safe_dump(cfg))
    policy = engine.load_policy(str(bl))
    plans = engine.plan_studies([(p, pydicom.dcmread(p, stop_before_pixels=True))
                                 for p in _files(os.path.join(src, "siemens"))], lambda pid: "SUBJ_X", policy)
    out = tmp_path / "bl_out"
    engine.write_study(plans[0], str(out), engine.UIDMapper(b"k" * 32), policy)
    leaked = [p for p in _files(out) if "DerivationDescription" in pydicom.dcmread(p, stop_before_pixels=True)]
    assert leaked, "expected the blocklist to miss DerivationDescription (documents why allowlist is default)"


def test_checker_flags_not_allowed_on_raw_input(run_dirs):
    src, _ = run_dirs
    _, hits = check.check_folder(str(src), engine.load_policy(CONFIG))
    assert any("DerivationDescription" in h for h in hits["NOT_ALLOWED"])


def test_time_source_choice():
    same = [{"AcquisitionTime": 0.0, "ContentTime": float(t)} for t in (0, 0, 7, 7, 14, 14)]
    assert engine.choose_time_source(same, [1, 1, 2, 2, 3, 3]) == "ContentTime"
    none = [{"AcquisitionTime": 0.0}] * 4
    assert engine.choose_time_source(none, [1, 1, 2, 2]) is None          # -> TPI order, no seconds
    assert engine.choose_time_source([{"AcquisitionTime": 5.0}] * 3, [None] * 3) == "AcquisitionTime"


def _visits(dst, pseudo="SUBJ_005"):
    rows = [r for r in csv.DictReader(open(os.path.join(dst, "_deid_meta", "patients.csv"))) if r["pseudonym"] == pseudo]
    return {r["timepoint_id"]: (int(r["study"]), r["days_since_first_study"]) for r in rows}


def test_multivisit_chronological_in_one_run(run_dirs):
    _, dst = run_dirs
    assert _visits(dst) == {"T0": (1, "0"), "T1": (2, "50"), "T2": (3, "161")}
    for p in _files(os.path.join(dst, "SUBJ_005")):
        ds = pydicom.dcmread(p, stop_before_pixels=True)
        assert "ClinicalTrialTimePointDescription" not in ds and ds.ClinicalTrialTimePointID in ("T0", "T1", "T2")
        assert str(ds.get("StudyDate", "")) == "" and str(ds.AcquisitionDateTime) == "2025"
        study = int(os.path.relpath(p, os.path.join(dst, "SUBJ_005")).split(os.sep)[0].split("_")[1])
        assert study == {"T0": 1, "T1": 2, "T2": 3}[ds.ClinicalTrialTimePointID]


def test_multivisit_across_runs_with_registry(tmp_path):
    from dicom_cloak import cli
    key, reg = tmp_path / "k.hex", tmp_path / "registry.csv"
    mp = tmp_path / "map.csv"
    mp.write_text("original_patient_id,pseudonym\nORIG-M-1,SUBJ_005\n")
    runs = [[syn.VISITS[1]], [syn.VISITS[0], syn.VISITS[2]]]   # T1 first, later T0 (earlier!) and T2
    outs = []
    for k, vis in enumerate(runs):
        src, dst = tmp_path / f"src{k}", tmp_path / f"dst{k}"
        syn.make_multivisit(str(src), visits=vis)
        with pytest.raises(SystemExit) as e:
            cli.main(["run", "--src", str(src), "--dst", str(dst), "--config", CONFIG, "--pseudonym-map", str(mp),
                      "--uid-key", str(key), "--study-registry", str(reg), "--workers", "1"])
        assert e.value.code == 0
        outs.append(_visits(str(dst)))
    assert outs[0] == {"T1": (1, "0")}
    # T1 keeps number 1 and day 0 (already released); T0 predates it -> next number, negative days
    assert outs[1] == {"T0": (2, "-50"), "T2": (3, "111")}
    assert oct(os.stat(reg).st_mode & 0o777) == "0o600"
    with pytest.raises(SystemExit):
        cli.main(["run", "--src", str(tmp_path / "src0"), "--dst", str(tmp_path / "d"), "--config", CONFIG,
                  "--pseudonym-map", str(mp), "--uid-key", str(key), "--study-registry",
                  str(tmp_path / "d" / "registry.csv")])


def test_relative_clock_preserves_intervals_hides_time_of_day(run_dirs):
    _, dst = run_dirs
    times = {}
    for p in _files(os.path.join(dst, "SUBJ_001")):
        ds = pydicom.dcmread(p, stop_before_pixels=True)
        if ds.get("AcquisitionTime"):
            folder = os.path.basename(os.path.dirname(p))
            times.setdefault(folder, set()).add(str(ds.AcquisitionTime))
    t = lambda part: next(v for k, v in times.items() if part in k)  # noqa: E731
    secs = lambda tm: int(tm[:2]) * 3600 + int(tm[2:4]) * 60 + float(tm[4:])  # noqa: E731
    native, post = min(secs(x) for x in t("native")), min(secs(x) for x in t("0020_"))
    assert native == 1 * 3600 and abs((post - native) - 200.0) < 1e-3   # 01:00:00 = study start
    assert not any(x.startswith("1015") for v in times.values() for x in v)   # real 10:15 never appears


def test_vendor_temporal_fields_untouched_joint_position_private(run_dirs):
    """dcm2niix assembles 4D volumes from TemporalPositionIdentifier: never rewrite it."""
    src, dst = run_dirs
    orig = {}
    for p in _files(os.path.join(src, "philips")):
        ds = pydicom.dcmread(p, stop_before_pixels=True)
        orig[int(ds.InstanceNumber)] = (ds.TemporalPositionIdentifier, ds.NumberOfTemporalPositions)
    for p in _files(os.path.join(dst, "SUBJ_002")):
        ds = pydicom.dcmread(p, stop_before_pixels=True)
        assert (ds.TemporalPositionIdentifier, ds.NumberOfTemporalPositions) == orig[int(ds.InstanceNumber)]
    for p in _files(os.path.join(dst, "SUBJ_001")):
        ds = pydicom.dcmread(p, stop_before_pixels=True)
        assert "TemporalPositionIdentifier" not in ds          # Siemens synthetic had none: none added
        blk = ds.private_block(0x0011, "DICOM CLOAK 1.0")
        if "native" in p:
            assert blk[engine.PRIV_JOINT_POSITION].value == "1" and blk[engine.PRIV_N_JOINT].value == "4"
