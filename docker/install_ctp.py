"""Install RSNA DicomAnonymizerTool (headless CTP anonymizer) and CTP from a pinned CTPTools25 commit.

  python install_ctp.py /opt [--config ctp/config.xml]

Downloads the installer jars from github.com/johnperry/CTPTools25 at a fixed commit, checks their
SHA-256 and unpacks them (the installers are plain zip archives; no GUI needed):
  /opt/DicomAnonymizerTool/DAT.jar ...   used by `dicom-cloak-ctp run`
  /opt/CTP/libraries/CTP.jar ...         server pipeline (ctp/config.xml), started with
                                         `java -jar libraries/CTP.jar` from /opt/CTP
Upgrading: change COMMIT and both SHA256 values together, then rerun tests/test_ctp.py.
"""
import hashlib
import os
import sys
import urllib.request
import zipfile

COMMIT = "7db432fe9fd5a6f25df6926f091af8f6d0c8c18a"   # CTPTools25 "Initial Java 25 build", 2025-10-31
JARS = {   # name -> (sha256, folder inside the installer)
    "DicomAnonymizerTool-installer.jar": ("a4ea937f215ac14f25409ae331e0cc18910769b6b888e6d75167391aa829eee1",
                                          "DicomAnonymizerTool/"),
    "CTP-installer.jar": ("e979e04c725650677babbfa9dff2bca4b968faf6fcfdc002bd75a167ff88a489", "CTP/"),
}
URL = "https://raw.githubusercontent.com/johnperry/CTPTools25/{commit}/{name}"


def main():
    dest = sys.argv[1]
    config = sys.argv[sys.argv.index("--config") + 1] if "--config" in sys.argv else None
    for name, (sha, folder) in JARS.items():
        path = os.path.join("/tmp", name)
        urllib.request.urlretrieve(URL.format(commit=COMMIT, name=name), path)
        got = hashlib.sha256(open(path, "rb").read()).hexdigest()
        if got != sha:
            sys.exit(f"checksum mismatch for {name}: {got}")
        with zipfile.ZipFile(path) as z:
            for m in z.namelist():
                if m.startswith(folder) and not m.endswith("/"):
                    if ".." in m.split("/"):
                        sys.exit(f"unsafe path in {name}: {m}")
                    z.extract(m, dest)
        os.remove(path)
        print(f"installed {folder} from {name} ({sha[:12]}...)")
    if config:
        with open(config) as src, open(os.path.join(dest, "CTP", "config.xml"), "w") as dst:
            dst.write(src.read())


if __name__ == "__main__":
    main()
