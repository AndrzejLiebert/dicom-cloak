"""CTP target: generated DicomAnonymizer script (always) and the full CTP run (needs Java + DicomAnonymizerTool:
set DAT_HOME to the folder holding DAT.jar, and JAVA if java is not on PATH; the Docker 'ctp' image has both)."""
import os
import shutil
import sys
import xml.etree.ElementTree as ET

import pydicom
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)

from dicom_cloak import check, ctp, engine  # noqa: E402

CONFIG = os.environ.get("DICOM_CLOAK_CONFIG", os.path.join(os.path.dirname(HERE), "config", "config.yaml"))
SALT = "0123456789abcdef" * 4


@pytest.fixture(scope="module")
def script():
    root = ET.fromstring(ctp.build_script(CONFIG, SALT, scrub_later=False))
    return root, {e.get("t"): (e.get("n"), e.text) for e in root.findall("e")}


def test_global_allowlist_switches(script):
    root, _ = script
    assert {r.get("t") for r in root.findall("r")} == {"privategroups", "unspecifiedelements", "overlays", "curves"}
    assert not root.findall("k")                       # no keep-group / keep-safe-private shortcuts
    assert [p.text for p in root.findall("p") if p.get("t") == "UIDSALT"] == [SALT]


def test_every_allowlisted_attribute_scripted_with_its_value_rule(script):
    _, el = script
    policy = engine.load_policy(CONFIG)
    by_kw = {n: s for n, s in el.values()}
    for kw in policy.allow_keywords | policy.allow_sequences:
        assert kw in by_kw, kw
    assert by_kw["PatientName"].startswith("@select(){@lookup(PatientID,ptid)}")
    assert "@quarantine()" in by_kw["SOPClassUID"]
    assert "@hashuid(@UIDROOT,this," in by_kw["SeriesInstanceUID"]
    assert by_kw["AcquisitionDateTime"].startswith("@contents(this,")
    assert by_kw["AcquisitionTime"] == "@lookup(SOPInstanceUID,tm_AcquisitionTime,remove)"
    for n, s in el.values():
        if n in policy.allow_sequences - engine.REWRITTEN_KEYWORDS:
            assert s == "@process()", n                 # never @keep(): nested content is filtered too
    for kw in policy.remove_keywords - engine.REWRITTEN_KEYWORDS:
        assert by_kw.get(kw, "@remove()") == "@remove()", kw


def test_never_skip(script):
    _, el = script
    # in a CTP pipeline a skipped object passes on unchanged: the script must quarantine instead
    assert not any("@skip()" in s for _, s in el.values())


def test_vendor_private_by_creator_with_spelling_variants(script):
    _, el = script
    assert el["2001[Philips Imaging DD 001]03"][1] == "@process()"
    assert el["2001[PHILIPS IMAGING DD 001]03"][1] == "@process()"
    assert el["0019[GEMS_ACQU_01]9E"][1] == "@process()"
    # protocol blobs need the Python scrubber: removed in a plain CTP pipeline
    assert el["0029[SIEMENS CSA HEADER]20"][1] == "@remove()"
    assert el["0021[SIEMENS MR SDS 01]19"][1] == "@remove()"
    kept_later = ET.fromstring(ctp.build_script(CONFIG, SALT, scrub_later=True))
    assert {e.get("t"): e.text for e in kept_later.findall("e")}["0029[SIEMENS CSA HEADER]20"] == "@keep()"
    creators = [t for t in el if t.startswith("0029") and len(t) == 8]
    assert len(creators) == 0xF0                         # every block 0x10..0xFF of a whitelisted group
    assert el["00290010"][1].startswith('@if(this,matches,"(?i)') and "CSA" in el["00290010"][1]


def test_age_table_and_lookup_escaping(tmp_path):
    policy = engine.load_policy(CONFIG)
    table = ctp.age_table(policy)
    for age in ("017Y", "030Y", "031Y", "056Y", "081Y", "006M", "100D"):
        assert table[f"agegroup/{age}"] == engine.age_group(age, policy.age_bins)
    p = tmp_path / "lut.properties"
    ctp.write_lookup(str(p), {"ptid/ID 1=x:y": "SUBJ_1", "ptid/Zoë": "SUBJ_2"})
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    text = p.read_text("latin-1")
    assert "ptid/ID\\ 1\\=x\\:y=SUBJ_1" in text and "ptid/Zo\\u00eb=SUBJ_2" in text


def _dat():
    home = os.environ.get("DAT_HOME", "/opt/DicomAnonymizerTool")
    java = os.environ.get("JAVA") or shutil.which("java")
    return (home, java) if java and os.path.exists(os.path.join(home, "DAT.jar")) else (None, None)


def _flat(ds, pre=""):
    out = {}
    for e in ds:
        if e.tag == 0x7FE00010 or e.keyword == "DeidentificationMethod":
            continue
        k = pre + (e.keyword or str(e.tag))
        if e.VR == "SQ":
            out[k] = len(e.value)
            for i, it in enumerate(e.value):
                out.update(_flat(it, f"{k}[{i}]."))
        elif e.VR == "UI" and str(e.value).startswith("2.25."):
            out[k] = "2.25.*"                            # remapped by design with different keyed hashes
        else:
            out[k] = (e.VR, bytes(e.value) if isinstance(e.value, (bytes, bytearray)) else str(e.value))
    return out


def test_ctp_run_matches_python_engine(run_dirs, tmp_path):
    home, java = _dat()
    if not home:
        pytest.skip("DicomAnonymizerTool / Java not available (set DAT_HOME, JAVA)")
    src, py_out = run_dirs
    out, work = tmp_path / "out", tmp_path / "work"
    mp = tmp_path / "map.csv"
    mp.write_text("original_patient_id,pseudonym\nORIG-S-1,SUBJ_001\nORIG-P-1,SUBJ_002\nORIG-G-1,SUBJ_003\n"
                  "ORIG-E-1,SUBJ_004\nORIG-M-1,SUBJ_005\n")
    with pytest.raises(SystemExit) as e:
        ctp.main(["run", "--src", str(src), "--dst", str(out), "--work", str(work), "--config", CONFIG,
                  "--pseudonym-map", str(mp), "--uid-key", str(tmp_path / "k.hex"), "--dat-home", home,
                  "--java", java, "--threads", "2", "--workers", "2"])
    assert e.value.code == 0
    assert not os.path.exists(work / "ctp_out") and not os.path.exists(work / "lookup-table.properties")
    policy = engine.load_policy(CONFIG)
    n, hits = check.check_folder(str(out), policy, strict_uids=True)
    assert n > 0 and not hits, dict(hits)
    py_files = sorted(os.path.relpath(os.path.join(r, f), py_out) for r, _, fs in os.walk(py_out)
                      for f in fs if f.endswith(".dcm"))
    ctp_files = sorted(os.path.relpath(os.path.join(r, f), out) for r, _, fs in os.walk(out)
                       for f in fs if f.endswith(".dcm"))
    assert py_files == ctp_files
    for rel in py_files:   # same allowlist, same values; only the UID hash differs
        assert _flat(pydicom.dcmread(os.path.join(py_out, rel))) == _flat(pydicom.dcmread(os.path.join(out, rel))), rel


@pytest.mark.parametrize("syntax", ["implicit_vr", "deflated", "rle_lossless"])
def test_ctp_run_transfer_syntaxes_and_auto_pseudonyms(tmp_path, syntax):
    home, java = _dat()
    if not home:
        pytest.skip("DicomAnonymizerTool / Java not available (set DAT_HOME, JAVA)")
    import synthetic as syn
    from pydicom.uid import DeflatedExplicitVRLittleEndian, ImplicitVRLittleEndian, RLELossless
    from test_inputs import _convert
    src, out, work = tmp_path / "src", tmp_path / "out", tmp_path / "work"
    syn.make_siemens(str(src / "s"))
    syn.make_ge(str(src / "g"))
    _convert(src, {"implicit_vr": ImplicitVRLittleEndian, "deflated": DeflatedExplicitVRLittleEndian,
                   "rle_lossless": RLELossless}[syntax])
    mp = tmp_path / "map.csv"
    with pytest.raises(SystemExit) as e:
        ctp.main(["run", "--src", str(src), "--dst", str(out), "--work", str(work), "--config", CONFIG,
                  "--pseudonym-map", str(mp), "--auto-pseudonyms", "SUBJ_", "--uid-key", str(tmp_path / "k.hex"),
                  "--dat-home", home, "--java", java, "--threads", "2", "--workers", "2"])
    assert e.value.code == 0
    assert sorted(os.listdir(out)) == ["SUBJ_0001", "SUBJ_0002", "_deid_meta"]
    n, hits = check.check_folder(str(out), engine.load_policy(CONFIG), strict_uids=True)
    assert n > 0 and not hits, dict(hits)
    for r, _, fs in os.walk(out):
        for f in fs:
            if f.endswith(".dcm"):
                ds = pydicom.dcmread(os.path.join(r, f))
                assert ds.file_meta.TransferSyntaxUID != DeflatedExplicitVRLittleEndian
                assert ds.pixel_array.size > 0
