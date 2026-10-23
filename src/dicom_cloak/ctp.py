"""dicom-cloak-ctp: RSNA CTP DicomAnonymizer as the de-identification engine (allowlist mode).

  dicom-cloak-ctp run --src IN --dst OUT --work WORK (--pseudonym-map map.csv | --use-existing-patient-id)
                     [--auto-pseudonyms PREFIX] [--uid-key key.hex] [--study-registry registry.csv] [--dat-home DIR] [--java JAVA]
                     [--threads N] [--workers N] [--keep-work]
  dicom-cloak-ctp script --out DIR [--pseudonym-map map.csv] [--uid-key key.hex]

The CTP DicomAnonymizer (run headless through RSNA's DicomAnonymizerTool, same anonymizer code as the
CTP server) removes everything that is not explicitly allowed: remove.unspecifiedelements,
remove.privategroups, remove.overlays and remove.curves, plus one script line per attribute that may
stay. The script is generated from the SAME policy files as the Python engine (standard_keep.yaml,
standard_remove.yaml, vendor_private.yaml, config.yaml), so both engines apply one allowlist and pass
one acceptance test (dicom-cloak-check).

Python only does what a CTP script cannot:
  pre-pass   headers only: pseudonyms, visit numbering, joint DCE timing and the relative clock are
             computed and handed to CTP as a lookup table; a staging tree of symlinks gives neutral
             output names (CTP keeps input file names)
  post-pass  Siemens protocol scrubber (CSA series header / XA ASCCONV blob, which the script keeps
             only into the private WORK dir), VRs of the engine's private block, per-file verification
             (rules + byte search for the study's original identifiers), CSV reports
`script` writes a script + lookup table for a CTP server pipeline (ctp/config.xml). That path has no
pre-pass: protocol blobs and all times are removed, the joint timing block is not written; pseudonyms,
age groups, UID remapping, year-only dates and the allowlists work the same.

WORK holds secrets (script with the UID salt, lookup table with original IDs, raw CTP output). It must
be outside OUT; it is deleted after a successful run unless --keep-work.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from xml.sax.saxutils import escape, quoteattr

import pydicom
import yaml
from pydicom import datadict
from pydicom.dataelem import DataElement
from pydicom.tag import Tag
from pydicom.uid import DeflatedExplicitVRLittleEndian

from . import cli, engine
from .scrub import SCRUBBERS

SALT_CARRIER = 0x10FF   # (output group, 10FF): never written; feeds the UID salt into @hashuid
TM_KEY = "tm_"          # lookup key type prefix for relative-clock TM attributes
DEID_METHOD_CTP = ["DICOM Cloak allowlist profile (RSNA CTP DicomAnonymizer)"] + engine.DEID_METHOD[1:]
DEID_CODES = "reset/113100/113105/113107/113108/113111"   # replace (not append to) codes of an earlier tool
# VRs of the engine's private block (CTP writes unknown private elements as LO)
OUTPUT_BLOCK_VR = {engine.PRIV_AGE_GROUP: "LO", engine.PRIV_SECONDS: "DS", engine.PRIV_ROLE: "LO",
                   engine.PRIV_TREF: "LO", engine.PRIV_SERIES_TAG: "LO", engine.PRIV_STUDY_INDEX: "IS",
                   engine.PRIV_DAYS: "IS", engine.PRIV_JOINT_POSITION: "IS", engine.PRIV_N_JOINT: "IS"}


# ----------------------------------------------------------------------------- script generation
def _rx(literal: str) -> str:
    """Regex for a literal, with backslashes doubled: CTP's script parser consumes one level."""
    return re.escape(literal).replace("\\", "\\\\")


def vendor_entries(config_path: str) -> list:
    """Vendor whitelist entries with their creator spelling, for the vendors enabled in config.yaml."""
    base = os.path.dirname(os.path.abspath(config_path))
    cfg = yaml.safe_load(open(config_path))
    p = cfg["vendor_private"]
    vend = yaml.safe_load(open(p if os.path.isabs(p) else os.path.join(base, p)))
    return [e for v in cfg.get("vendors", ["siemens", "philips", "ge"]) for e in vend.get(v, {}).get("keep", [])]


def build_script(config_path: str, salt: str, scrub_later: bool, method: list = DEID_METHOD_CTP) -> str:
    """DicomAnonymizer script (XML) implementing the allowlist policy of config_path.

    scrub_later: True = keep the Siemens protocol blobs for the Python post-pass scrubber (dicom-cloak-ctp
    run); False = remove them (CTP server pipeline without post-pass)."""
    policy = engine.load_policy(config_path)
    if policy.mode != "allowlist":
        raise ValueError("the CTP target implements allowlist mode only")
    og = policy.output_private_group
    salt_tag = f"{og:04X}{SALT_CARRIER:04X}"
    hashuid = f"@hashuid(@UIDROOT,this,{salt_tag})"
    uid_std = _rx(engine.STANDARD_UID_ROOT) + ".*"
    sop_rx = "|".join(_rx(u) for u in sorted(policy.keep_sop_classes))
    allowed = policy.allow_keywords | policy.allow_sequences
    rows = []   # (t attribute, name, script)

    def add(t, name, script):
        rows.append((t, name, script))

    def std(kw, script):
        tag = datadict.tag_for_keyword(kw)
        if tag is None:
            raise ValueError(f"unknown DICOM keyword in policy: {kw}")
        add(f"{tag:08X}", kw, script)

    special = {
        "PatientName": "@select(){@lookup(PatientID,ptid)}{@remove()}",
        "PatientID": "@select(){@lookup(this,ptid)}{@remove()}",
        # @quarantine, never @skip: in a CTP server pipeline a skipped object passes on UNCHANGED
        "SOPClassUID": "@select(){@if(this,matches,\"(" + sop_rx + ")\"){@keep()}{@quarantine()}}{@keep()}",
        "PatientIdentityRemoved": "@always()YES",
        "DeidentificationMethod": "@always()" + "\\\\".join(method),   # multi-valued: '\\' -> '\' 
        "DeidentificationMethodCodeSequence": DEID_CODES,
        "LongitudinalTemporalInformationModified": "@always()MODIFIED",
        "TemporalPositionTimeOffset": "@always()@lookup(SOPInstanceUID,sec,keep)",
    }
    for kw in sorted(allowed | engine.REWRITTEN_KEYWORDS):
        tag = datadict.tag_for_keyword(kw)
        vr = datadict.dictionary_VR(tag) if tag is not None else ""
        if kw in special:
            s = special[kw]
        elif vr == "SQ":
            s = "@process()"                       # items filtered with the same script
        elif vr in ("PN", "DA", "AE"):
            s = "@remove()"
        elif vr == "TM":
            s = (f"@lookup(SOPInstanceUID,{TM_KEY}{kw},remove)"
                 if policy.relative_clock and kw in policy.relative_tm else "@remove()")
        elif vr == "DT":
            s = '@contents(this,"^(\\\\d{4})?.*$","$1")'   # year only
        elif vr == "UI" and kw not in engine.KEEP_UI_KEYWORDS:
            s = f'@if(this,isblank){{@keep()}}{{@if(this,matches,"{uid_std}"){{@keep()}}{{{hashuid}}}}}'
        else:
            s = "@keep()"
        std(kw, s)
    # second line of defence (unspecified elements are removed anyway)
    for kw in sorted(policy.remove_keywords - allowed - engine.REWRITTEN_KEYWORDS):
        if datadict.tag_for_keyword(kw) is not None:
            std(kw, "@remove()")
    # CTP always keeps group 0028 unless scripted: remove what the allowlist does not name
    for tag, entry in sorted(datadict.DicomDictionary.items()):
        kw = entry[4]
        if tag >> 16 == 0x0028 and kw and kw not in allowed and kw not in engine.REWRITTEN_KEYWORDS:
            std(kw, "@remove()")
    # vendor private allowlist, addressed by creator; CTP compares creators exactly -> spelling variants
    creators = {}
    for e in vendor_entries(config_path):
        g, el, c = int(e["group"]), int(e["element"]), e["creator"].strip()
        for variant in sorted({c, c.upper()}):
            script = ("@keep()" if scrub_later else "@remove()") if e.get("scrub") else "@process()"
            add(f"{g:04X}[{variant}]{el:02X}", e["name"], script)
            creators.setdefault(g, set()).add(variant)
    # creator elements cannot be addressed by creator: allow (gggg,00bb) only with a whitelisted creator
    for g, names in sorted(creators.items()):
        rx = "(?i)\\\\s*(" + "|".join(_rx(n) for n in sorted(names)) + ")\\\\s*"
        for b in range(0x10, 0x100):
            add(f"{g:04X}00{b:02X}", f"Creator{g:04X}_{b:02X}",
                f'@if(this,matches,"{rx}"){{@keep()}}{{@remove()}}')
    # the engine's own private block (age group, joint timing, visit) at block 0x10
    add(f"{og:04X}0010", "DeidCreator", "@always()" + policy.output_private_creator)
    lookups = {engine.PRIV_AGE_GROUP: "@lookup(PatientAge,agegroup,default,unknown)",
               engine.PRIV_SECONDS: "@lookup(SOPInstanceUID,sec,remove)",
               engine.PRIV_ROLE: "@lookup(SeriesInstanceUID,role,remove)",
               engine.PRIV_TREF: "@lookup(StudyInstanceUID,tref,remove)",
               engine.PRIV_SERIES_TAG: "@lookup(SeriesInstanceUID,stags,remove)",
               engine.PRIV_STUDY_INDEX: "@lookup(StudyInstanceUID,study,remove)",
               engine.PRIV_DAYS: "@lookup(StudyInstanceUID,days,remove)",
               engine.PRIV_JOINT_POSITION: "@lookup(SOPInstanceUID,pos,remove)",
               engine.PRIV_N_JOINT: "@lookup(SOPInstanceUID,npos,remove)"}
    for el, s in sorted(lookups.items()):
        add(f"{og:04X}10{el:02X}", f"Deid{el:02X}", "@always()" + s)
    add(salt_tag, "SaltCarrier", "@if(this,exists){@remove()}{@param(@UIDSALT)}")

    out = ["<script>",
           "<!-- generated by dicom-cloak-ctp from the dicom-cloak policy files; do not edit by hand -->",
           "<p t=\"UIDROOT\">2.25</p>",
           f"<p t=\"UIDSALT\">{escape(salt)}</p>",
           '<r en="T" t="privategroups">Remove private groups (vendor whitelist below)</r>',
           '<r en="T" t="unspecifiedelements">Remove unspecified elements (allowlist)</r>',
           '<r en="T" t="overlays">Remove overlays</r>',
           '<r en="T" t="curves">Remove curves</r>']
    seen = set()
    for t, name, script in rows:
        if t in seen:
            raise ValueError(f"duplicate script element {t}")
        seen.add(t)
        out.append(f'<e en="T" t={quoteattr(t)} n={quoteattr(name)}>{escape(script)}</e>')
    out.append("</script>")
    return "\n".join(out) + "\n"


def filter_script(config_path: str) -> str:
    """CTP DicomFilter script: pass only the kept SOP classes (MR / Enhanced MR); '+' is OR."""
    policy = engine.load_policy(config_path)
    return " + ".join(f'SOPClassUID.equals("{u}")' for u in sorted(policy.keep_sop_classes)) + "\n"


def uid_salt(key_hex: str) -> str:
    """Secret salt for CTP's @hashuid (MD5 of UID + salt), derived from the engine's UID key."""
    return hmac.new(bytes.fromhex(key_hex), b"dicom_cloak ctp uid salt", hashlib.sha256).hexdigest()


# ----------------------------------------------------------------------------- lookup table
def _prop(s: str, key: bool) -> str:
    """java.util.Properties escaping (ISO-8859-1 file, non-ASCII as \\uXXXX)."""
    out = []
    for i, ch in enumerate(s):
        if ch == "\\":
            out.append("\\\\")
        elif ch in "=:#! " and (key or (ch == " " and i == 0)):
            out.append("\\" + ch)
        elif ord(ch) < 32 or ord(ch) > 126:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def age_table(policy) -> dict:
    """PatientAge string -> age group, for every nnnY/M/W/D value (same rule as engine.age_group)."""
    return {f"agegroup/{n:03d}{u}": engine.age_group(f"{n:03d}{u}", policy.age_bins)
            for u in "YMWD" for n in range(1000)}


def write_lookup(path: str, table: dict):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="latin-1") as f:
        f.write("# generated by dicom-cloak-ctp: SECRET (original identifiers); never copy into the output\n")
        for k in sorted(table):
            f.write(f"{_prop(k, True)}={_prop(str(table[k]), False)}\n")
    cli.chmod_private(path)


def write_secret(path: str, text: str):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    cli.chmod_private(path)


# ----------------------------------------------------------------------------- pre-pass
def _plan_job(args):
    """Plan one patient's studies (headers only) -> per study: plan, staged files, lookup entries."""
    paths, config, pseudo_map, use_existing, key_hex, registry = args
    policy = engine.load_policy(config)
    uidmap = engine.UIDMapper(bytes.fromhex(key_hex))
    headers = [(p, pydicom.dcmread(p, stop_before_pixels=True)) for p in paths]
    hdr = dict(headers)

    def pseudonym_of(pid):
        return pid if use_existing else pseudo_map.get(pid)

    out = []
    for plan in engine.plan_studies(headers, pseudonym_of, policy, registry=registry, uidmap=uidmap):
        clock = (engine.RelativeClock(policy.relative_clock, plan.clock_anchor)
                 if policy.relative_clock and plan.clock_anchor is not None else None)
        lut, staged = {}, []
        for s in plan.series:
            first = hdr[s.files[0][0]]
            study_uid = str(first.StudyInstanceUID)
            lut[f"ptid/{str(first.get('PatientID', '')).strip()}"] = plan.pseudonym
            lut[f"study/{study_uid}"] = plan.study_index
            lut[f"tref/{study_uid}"] = plan.tref
            if plan.days_since_first is not None:
                lut[f"days/{study_uid}"] = plan.days_since_first
            lut[f"role/{s.uid}"] = s.role
            if s.series_tags:
                lut[f"stags/{s.uid}"] = ";".join(f"{k}={v}" for k, v in s.series_tags.items())
            for k, (path, t, _, sop) in enumerate(sorted(s.files, key=lambda f: f[0]), 1):
                ds = hdr[path]
                rel = os.path.join(engine.study_dir_of(plan), s.out_folder,
                                   engine.output_name(ds.get("InstanceNumber"), k))
                staged.append((path, rel))
                sec = engine.file_seconds(plan, t)
                if sec is not None:
                    lut[f"sec/{sop}"] = f"{sec:.3f}"
                pos = s.positions.get(path)
                if pos is not None:
                    lut[f"pos/{sop}"] = pos
                    lut[f"npos/{sop}"] = plan.n_positions
                if clock is not None:
                    date = engine.file_date(ds)
                    for kw in sorted(policy.relative_tm):
                        if ds.get(kw) not in (None, ""):
                            v = clock(date, ds.get(kw))
                            if v is not None:
                                lut[f"{TM_KEY}{kw}/{sop}"] = v
        out.append((plan, staged, lut))
    return out


# ----------------------------------------------------------------------------- CTP (DicomAnonymizerTool)
def run_dat(java, dat_home, stage, ctp_out, script, lut, filt, threads, n_expected, log_path):
    """Run DicomAnonymizerTool on the staging tree. DAT always exits 0 and can stay alive after the last
    file, so progress is read from its log and the process is stopped once every file is accounted for."""
    cmd = [java, "-jar", os.path.join(dat_home, "DAT.jar"), "-in", stage, "-out", ctp_out, "-f", filt,
           "-da", script, "-lut", lut, "-n", str(threads), "-v"]   # -v: also logs filtered files
    counts = {"ok": 0, "failed": 0}
    with open(log_path, "w") as log:
        proc = subprocess.Popen(cmd, cwd=dat_home, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        def reader():
            for line in proc.stdout:
                log.write(line)
                if "Anonymized file:" in line:
                    counts["ok"] += 1
                elif "Aborting the processing" in line or "Skipping non-matching" in line:
                    counts["failed"] += 1

        th = threading.Thread(target=reader, daemon=True)
        th.start()
        done_at = None
        while proc.poll() is None:
            time.sleep(0.5)
            if counts["ok"] + counts["failed"] >= n_expected:
                done_at = done_at or time.time()
                if time.time() - done_at > 15:
                    proc.terminate()
                    break
        proc.wait()
        th.join(timeout=5)
    return counts


# ----------------------------------------------------------------------------- post-pass
def scrub_private(ds, policy) -> None:
    """Apply the protocol scrubbers to whitelisted blobs at any depth; drop creators of empty blocks."""
    kept_blocks = set()
    for elem in list(ds):
        t = elem.tag
        if t.is_private and t.element >= 0x0100:
            method = policy.private_scrub.get((t.group, engine.private_creator_of(ds, t).lower(), t.element & 0xFF))
            if method:
                raw = elem.value if isinstance(elem.value, (bytes, bytearray)) else \
                    str(elem.value).encode("latin-1", "ignore")
                new = SCRUBBERS[method](bytes(raw))
                if new is None:
                    del ds[t]
                    continue
                elem.value = new
            kept_blocks.add((t.group, t.element >> 8))
        if elem.VR == "SQ":
            for item in elem.value:
                scrub_private(item, policy)
    for elem in list(ds):
        t = elem.tag
        if t.is_private and 0x0010 <= t.element <= 0x00FF and (t.group, t.element) not in kept_blocks:
            del ds[t]


def normalize_output_block(ds, policy) -> None:
    for el, vr in OUTPUT_BLOCK_VR.items():
        t = Tag(policy.output_private_group, 0x1000 | el)
        if t in ds and ds[t].VR != vr:
            v = ds[t].value
            v = v.decode("latin-1") if isinstance(v, (bytes, bytearray)) else str(v)
            ds[t] = DataElement(t, vr, v.strip().rstrip("\x00"))


def _post_job(args):
    plan, staged, config, ctp_out, dst = args
    policy = engine.load_policy(config)
    problems, nfiles = [], 0
    for _, rel in staged:
        p = os.path.join(ctp_out, rel)
        if not os.path.exists(p):
            problems.append(f"{rel}: not written by CTP (skipped or quarantined, see dat.log)")
            continue
        ds = pydicom.dcmread(p, force=True)
        if "PixelData" not in ds:   # every kept SOP class is an image: never accept a silent pixel loss
            problems.append(f"{rel}: pixel data missing in the CTP output")
            continue
        scrub_private(ds, policy)
        normalize_output_block(ds, policy)
        engine.inflate(ds)
        out = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        ds.save_as(out, enforce_file_format=False)
        os.remove(p)
        nfiles += 1
        problems += [f"{rel}: {x}" for x in engine.verify_file(out, plan.forbidden, plan.pseudonym, policy)]
    rows = [engine.series_row(plan, s, [engine.file_seconds(plan, f[1]) for f in s.files]) for s in plan.series]
    prow = engine.patient_row(plan, engine.age_group(plan.age, policy.age_bins), nfiles, len(problems))
    return prow, rows, engine.excluded_rows(plan), problems


def stage_file(src, p):
    """Symlink the original into the staging tree. Deflated files are staged as an inflated copy instead:
    DicomAnonymizerTool drops the pixel data of deflated input."""
    try:
        ts = str(pydicom.filereader.read_file_meta_info(src).get("TransferSyntaxUID", ""))
    except Exception:  # noqa: BLE001
        ts = ""
    if ts != DeflatedExplicitVRLittleEndian:
        os.symlink(os.path.abspath(src), p)
        return
    ds = pydicom.dcmread(src, force=True)
    engine.inflate(ds)
    ds.save_as(p, enforce_file_format=False)
    cli.chmod_private(p)


# ----------------------------------------------------------------------------- commands
def _inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def run(a):
    if _inside(a.work, a.dst) or _inside(a.dst, a.work) or _inside(a.work, a.src) or _inside(a.dst, a.src):
        sys.exit("--work, --dst and --src must be separate folders (WORK holds secrets)")
    cli.check_secret_paths(a)
    dat_jar = os.path.join(a.dat_home, "DAT.jar")
    if not os.path.exists(dat_jar):
        sys.exit(f"DicomAnonymizerTool not found: {dat_jar} (set --dat-home or DAT_HOME)")
    key_hex = cli.load_uid_key(a.uid_key)
    by_patient = cli.group_by_patient(a.src, a.workers)
    pseudo_map = cli.prepare_pseudonyms(a, by_patient)
    registry = cli.load_registry(a.study_registry)
    os.makedirs(a.work, mode=0o700, exist_ok=True)
    stage, ctp_out = os.path.join(a.work, "stage"), os.path.join(a.work, "ctp_out")
    for d in (stage, ctp_out):
        if os.path.exists(d):
            shutil.rmtree(d)
        os.makedirs(d)

    # 1. pre-pass (headers only)
    policy = engine.load_policy(a.config)
    lut, studies, failed = age_table(policy), [], 0
    jobs = [(paths, a.config, pseudo_map, a.use_existing_patient_id, key_hex, registry)
            for paths in by_patient.values()]
    with ProcessPoolExecutor(a.workers) as ex:
        for fut in as_completed([ex.submit(_plan_job, j) for j in jobs]):
            try:
                results = fut.result()
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"PLAN ERROR\n{traceback.format_exc()}", flush=True)
                continue
            for plan, staged, entries in results:
                for k, v in entries.items():
                    if k in lut and str(lut[k]) != str(v):
                        print(f"warning: conflicting lookup values for {k.split('/')[0]} (duplicate UID in input)")
                    lut[k] = v
                studies.append((plan, staged))
                registry.setdefault(plan.pseudonym, {})[plan.study_key] = (plan.study_index, plan.study_date)
    n_files = 0
    for plan, staged in studies:
        for src, rel in staged:
            p = os.path.join(stage, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            stage_file(src, p)
            n_files += 1
    script, lut_path = os.path.join(a.work, "DicomAnonymizer.script"), os.path.join(a.work, "lookup-table.properties")
    filt = os.path.join(a.work, "mr-filter.script")
    write_secret(script, build_script(a.config, uid_salt(key_hex), scrub_later=True))
    write_secret(filt, filter_script(a.config))
    write_lookup(lut_path, lut)
    print(f"pre-pass: {len(studies)} studies, {n_files} files staged", flush=True)

    # 2. CTP DicomAnonymizer
    t0 = time.time()
    counts = run_dat(a.java, a.dat_home, stage, ctp_out, script, lut_path, filt, a.threads, n_files,
                     os.path.join(a.work, "dat.log"))
    print(f"CTP DicomAnonymizer: {counts['ok']} OK, {counts['failed']} not written ({time.time() - t0:.0f} s)",
          flush=True)

    # 3. post-pass: scrub protocol blobs, verify, reports
    meta_dir = os.path.join(a.dst, "_deid_meta")
    os.makedirs(meta_dir, exist_ok=True)
    patients, timing, excluded = [], [], []
    with ProcessPoolExecutor(a.workers) as ex:
        futs = {ex.submit(_post_job, (plan, staged, a.config, ctp_out, a.dst)): plan for plan, staged in studies}
        for k, fut in enumerate(as_completed(futs), 1):
            try:
                prow, rows, excl, problems = fut.result()
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"[{k}/{len(futs)}] POST ERROR\n{traceback.format_exc()}", flush=True)
                continue
            patients.append(prow)
            timing += rows
            excluded += excl
            failed += bool(problems)
            status = "OK" if not problems else f"VERIFY FAILED ({len(problems)})"
            print(f"[{k}/{len(futs)}] {prow['pseudonym']} study {prow['study']}: {prow['n_files']} files, "
                  f"ref={prow['timing_reference']}, positions={prow['n_temporal_positions']} {status}", flush=True)
            for p in problems[:5]:
                print(f"    {p}", flush=True)
    engine.write_csv(os.path.join(meta_dir, "patients.csv"), sorted(patients, key=lambda r: (r["pseudonym"], r["study"])))
    engine.write_csv(os.path.join(meta_dir, "series_timing.csv"),
                     sorted(timing, key=lambda r: (r["pseudonym"], r["study"], r["series_folder"])))
    engine.write_csv(os.path.join(meta_dir, "excluded.csv"), excluded)
    if a.study_registry:
        cli.save_registry(a.study_registry, registry)
    if failed:
        print(f"done: {len(patients)} studies, failures: {failed}; WORK kept for inspection: {a.work} "
              "(holds secrets and raw CTP output: delete it when done)", flush=True)
        return 1
    if not a.keep_work:
        for name in ("stage", "ctp_out", "DicomAnonymizer.script", "lookup-table.properties", "mr-filter.script",
                     "dat.log"):
            p = os.path.join(a.work, name)
            if os.path.isdir(p):
                shutil.rmtree(p)
            elif os.path.exists(p):
                os.remove(p)
    print(f"done: {len(patients)} studies, failures: 0", flush=True)
    return 0


def script_cmd(a):
    """Script + lookup table for a CTP server pipeline (no pre-pass: see module docstring)."""
    os.makedirs(a.out, mode=0o700, exist_ok=True)
    key_hex = cli.load_uid_key(a.uid_key)
    policy = engine.load_policy(a.config)
    lut = age_table(policy)
    if a.pseudonym_map:
        a.use_existing_patient_id = False
        lut.update({f"ptid/{k}": v for k, v in cli.load_pseudonyms(a).items()})
    write_secret(os.path.join(a.out, "DicomAnonymizer.script"), build_script(a.config, uid_salt(key_hex), scrub_later=False))
    write_lookup(os.path.join(a.out, "lookup-table.properties"), lut)
    write_secret(os.path.join(a.out, "mr-filter.script"), filter_script(a.config))
    print(f"wrote {a.out}/DicomAnonymizer.script, lookup-table.properties (both secret: UID salt, original IDs) "
          "and mr-filter.script")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dicom-cloak-ctp", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    cfg_default = os.environ.get("DICOM_CLOAK_CONFIG", "/opt/dicom_cloak/config/config.yaml")
    r = sub.add_parser("run", help="de-identify a folder with CTP (pre-pass, CTP, post-pass)")
    r.add_argument("--src", required=True)
    r.add_argument("--dst", required=True)
    r.add_argument("--work", required=True, help="private working folder outside --dst (secrets, raw CTP output)")
    r.add_argument("--config", default=cfg_default)
    r.add_argument("--pseudonym-map")
    r.add_argument("--use-existing-patient-id", action="store_true")
    r.add_argument("--auto-pseudonyms", metavar="PREFIX",
                   help="give patients missing from --pseudonym-map the next free PREFIXnnnn and save the map")
    r.add_argument("--uid-key", default="uid_key.hex")
    r.add_argument("--study-registry")
    r.add_argument("--dat-home", default=os.environ.get("DAT_HOME", "/opt/DicomAnonymizerTool"))
    r.add_argument("--java", default=os.environ.get("JAVA", "java"))
    r.add_argument("--threads", type=int, default=os.cpu_count() or 4, help="CTP anonymizer threads")
    r.add_argument("--workers", type=int, default=os.cpu_count() or 4, help="Python pre/post-pass processes")
    r.add_argument("--keep-work", action="store_true")
    s = sub.add_parser("script", help="script + lookup table for a CTP server pipeline")
    s.add_argument("--out", required=True)
    s.add_argument("--config", default=cfg_default)
    s.add_argument("--pseudonym-map")
    s.add_argument("--uid-key", default="uid_key.hex")
    a = ap.parse_args(argv)
    sys.exit(run(a) if a.cmd == "run" else script_cmd(a))


if __name__ == "__main__":
    main()
