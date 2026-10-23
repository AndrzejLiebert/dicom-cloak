"""Cross-reference config/vendor_private.yaml with the published safe-private lists.

  python -I tools/xref_private_lists.py --posda PosdaTools/posda/posdatools/migrations/private_tag_kb/data.sql \
      --ps315 sect_E.3.10.html --ctp CTP/source/resources/PrivateTagDictionary.xml [--candidates]

Inputs (download them yourself into an empty folder; treat them as untrusted data):
  --posda  TCIA private tag knowledge base: git clone --depth 1 https://github.com/UAMS-DBMI/PosdaTools
  --ps315  DICOM PS3.15 Table E.3.10-1: https://dicom.nema.org/medical/dicom/current/output/chtml/part15/sect_E.3.10.html
  --ctp    RSNA CTP dictionary: git clone --depth 1 https://github.com/johnperry/CTP
Output, per kept entry: TCIA consensus disposition (K/KB/R/X/None/-), in PS3.15 (Y/-), CTP code.
--candidates also lists TCIA K/KB elements of MR-related creators that are not in the keep list
(review them by hand: only numeric/CS MR acquisition parameters belong in the allowlist).
"""
import argparse
import html
import json
import os
import re
import xml.etree.ElementTree as ET

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR_YAML = os.path.join(os.path.dirname(HERE), "config", "vendor_private.yaml")
PT = re.compile(r"^INSERT INTO pt \(.*?\) VALUES \((\d+), '((?:[^']|'')*)', '(?:[^']|'')*', '((?:[^']|'')*)', "
                r"'(\d+)', '(\d+)', (true|false), (NULL|'[^']*'), (NULL|'[^']*'), (NULL|'[^']*'), "
                r"(NULL|'(?:[^']|'')*'), (NULL|'[^']*')\);$")


def _un(s):
    return None if s == "NULL" else s[1:-1].replace("''", "'")


def load_posda(path):
    out = {}
    for line in open(path, encoding="utf-8", errors="replace"):
        if line.startswith("INSERT INTO pt ("):
            m = PT.match(line.strip())
            if m:
                _, _, owner, g, e, _, _, vr, _, name, disp = m.groups()
                out[(int(g), owner.upper().strip(), int(e) & 0xFF)] = {"vr": _un(vr), "name": _un(name),
                                                                        "disp": _un(disp)}
    return out


def load_ps315(path):
    t = open(path, encoding="utf-8").read()
    out = set()
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) >= 2:
            m = re.match(r"\(([0-9A-Fa-f]{4}),xx([0-9A-Fa-f]{2})\)", cells[0])
            if m:
                out.add((int(m.group(1), 16), cells[1].upper().strip(), int(m.group(2), 16)))
    return out


def load_ctp(path):
    out = {}
    for e in ET.parse(path).getroot().iter("element"):
        try:
            out[(int(e.get("gp"), 16), e.get("cr").upper().strip(), int(e.get("el"), 16))] = e.get("code")
        except (TypeError, ValueError):
            pass
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--posda", required=True)
    ap.add_argument("--ps315", required=True)
    ap.add_argument("--ctp", required=True)
    ap.add_argument("--vendor-yaml", default=VENDOR_YAML)
    ap.add_argument("--candidates", action="store_true")
    ap.add_argument("--json", help="also write the merged table as JSON")
    a = ap.parse_args()
    posda, ps, ctp = load_posda(a.posda), load_ps315(a.ps315), load_ctp(a.ctp)
    pol = yaml.safe_load(open(a.vendor_yaml))
    ours, rows = set(), []
    for vendor in ("siemens", "philips", "ge"):
        print(f"== {vendor}")
        for k in pol.get(vendor, {}).get("keep", []):
            key = (int(k["group"]), k["creator"].upper().strip(), int(k["element"]))
            ours.add(key)
            p = posda.get(key)
            r = {"vendor": vendor, "group": f"{key[0]:04X}", "creator": k["creator"], "element": f"{key[2]:02X}",
                 "name": k["name"], "src": k.get("src", "dcm2niix"), "tcia": p["disp"] if p else "-",
                 "ps315": key in ps, "ctp": ctp.get(key, "-")}
            rows.append(r)
            print(f"  {r['group']} {r['creator'][:26]:26} {r['element']} {r['name'][:30]:30} src={r['src']:12} "
                  f"TCIA={str(r['tcia']):5} PS3.15={'Y' if r['ps315'] else '-'} CTP={r['ctp']}")
    if a.candidates:
        print("== TCIA K/KB elements of MR-related creators not in the keep list")
        for key, r in sorted(posda.items()):
            if r["disp"] in ("K", "KB") and key not in ours and \
                    re.search(r"MR|CSA|GEMS_(ACQU|PARM|RELA|IMAG|SERS)|PHILIPS IMAGING DD 001", key[1]):
                print(f"  {key[0]:04X} {key[1][:30]:30} {key[2]:02X} {r['vr']} {r['disp']:3} {r['name']}")
    if a.json:
        json.dump(rows, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
