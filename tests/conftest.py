"""Shared fixture: run the engine once on synthetic Siemens/Philips/GE/Enhanced studies."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

from dicom_cloak import cli  # noqa: E402
import synthetic as syn  # noqa: E402

CONFIG = os.environ.get("DICOM_CLOAK_CONFIG", os.path.join(ROOT, "config", "config.yaml"))


@pytest.fixture(scope="session")
def run_dirs(tmp_path_factory):
    src = tmp_path_factory.mktemp("src")
    dst = tmp_path_factory.mktemp("dst")
    syn.make_siemens(str(src / "siemens"))
    syn.make_philips(str(src / "philips"))
    syn.make_ge(str(src / "ge"))
    syn.make_enhanced(str(src / "enhanced"))
    syn.make_multivisit(str(src / "multivisit"))
    mp = tmp_path_factory.mktemp("map") / "map.csv"
    mp.write_text("original_patient_id,pseudonym\nORIG-S-1,SUBJ_001\nORIG-P-1,SUBJ_002\nORIG-G-1,SUBJ_003\nORIG-E-1,SUBJ_004\nORIG-M-1,SUBJ_005\n")
    key = tmp_path_factory.mktemp("key") / "uid_key.hex"
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--src", str(src), "--dst", str(dst), "--config", CONFIG, "--pseudonym-map", str(mp),
                  "--uid-key", str(key), "--workers", "2"])
    assert e.value.code == 0, "engine reported failures"
    return src, dst


