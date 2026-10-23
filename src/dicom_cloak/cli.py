"""dicom-cloak: de-identify a breast MRI DICOM tree into a clean copy.

  dicom-cloak run --src IN --dst OUT --config config.yaml (--pseudonym-map map.csv | --use-existing-patient-id)
                 [--auto-pseudonyms PREFIX] [--uid-key key.hex] [--study-registry registry.csv] [--workers N]
                 [--dry-run]

The pseudonym map (CSV: original_patient_id,pseudonym) is never copied to OUT. It is read only, unless
--auto-pseudonyms is given: then patients missing from the map get the next free PREFIXnnnn (e.g.
SUBJ_0001) and the map file is created or extended (mode 600). Existing entries never change.
Multiple visits per patient are numbered chronologically (study_01 = first visit) with
days_since_first_study. Visits exported in separate runs: pass the same --study-registry (secret like the
map: it holds original visit dates) so numbering and day offsets continue instead of restarting.
OUT gets <pseudonym>/study_NN/<NNNN_series>/IM_*.dcm and _deid_meta/{patients,series_timing,excluded}.csv.
Exit code 1 if any study fails or any written file fails verification.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import secrets
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

import pydicom

from . import engine


def _study_key(path):
    try:
        ds = pydicom.dcmread(path, stop_before_pixels=True, specific_tags=["PatientID", "StudyInstanceUID"])
        return path, (str(ds.get("PatientID", "")), str(ds.get("StudyInstanceUID", "")))
    except Exception:  # noqa: BLE001
        return path, None


def _process(args):
    paths, config, pseudo_map, use_existing, key_hex, dst, dry, registry = args
    policy = engine.load_policy(config)
    uidmap = engine.UIDMapper(bytes.fromhex(key_hex))
    headers = [(p, pydicom.dcmread(p, stop_before_pixels=True)) for p in paths]

    def pseudonym_of(pid):
        return pid if use_existing else pseudo_map.get(pid)

    out, reg_rows = [], []
    for plan in engine.plan_studies(headers, pseudonym_of, policy, registry=registry, uidmap=uidmap):
        out.append(engine.write_study(plan, dst, uidmap, policy, dry))
        reg_rows.append((plan.pseudonym, plan.study_key, plan.study_index, plan.study_date))
    return out, reg_rows


def load_registry(path):
    reg = {}
    if path and os.path.exists(path):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                reg.setdefault(r["pseudonym"], {})[r["study_key"]] = (int(r["study_index"]), r["study_date"])
    return reg


def save_registry(path, reg):
    rows = sorted((p, k, i, d) for p, studies in reg.items() for k, (i, d) in studies.items())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pseudonym", "study_key", "study_index", "study_date"])
        w.writerows(rows)
    chmod_private(path)


def load_uid_key(path):
    """Read the UID secret (hex); create it (mode 600) if missing or empty."""
    if not os.path.exists(path) or not open(path).read().strip():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, secrets.token_hex(32).encode())
        os.close(fd)
    chmod_private(path)
    key_hex = open(path).read().strip()
    if len(bytes.fromhex(key_hex)) < 16:
        sys.exit("UID key too short (need >= 16 bytes hex)")
    return key_hex


def chmod_private(path, mode=0o600):
    """Best effort: some bind mounts (Docker Desktop on Windows) do not support Unix permissions."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def load_pseudonyms(a):
    if a.pseudonym_map and (os.path.exists(a.pseudonym_map) or not getattr(a, "auto_pseudonyms", None)):
        with open(a.pseudonym_map, newline="", encoding="utf-8-sig") as f:   # BOM: Excel "CSV UTF-8"
            header = f.readline()
            f.seek(0)
            delim = ";" if ";" in header and "," not in header else ","    # Excel with a ; list separator
            rows = list(csv.DictReader(f, delimiter=delim))
        if rows and not {"original_patient_id", "pseudonym"} <= set(rows[0]):
            sys.exit("pseudonym map needs the header line: original_patient_id,pseudonym")
        pmap = {r["original_patient_id"].strip(): r["pseudonym"].strip() for r in rows
                if (r["original_patient_id"] or "").strip()}
        dup = len(pmap.values()) - len(set(pmap.values()))
        if dup:
            sys.exit(f"pseudonym map: {dup} pseudonym(s) given to more than one patient")
        return pmap
    if getattr(a, "auto_pseudonyms", None):
        return {}
    if not a.use_existing_patient_id:
        sys.exit("give --pseudonym-map or --use-existing-patient-id")
    return {}


def assign_pseudonyms(pseudo_map, patient_ids, path, prefix):
    """Give every PatientID without a pseudonym the next free <prefix>NNNN and save the map (mode 600).
    Existing entries are never changed. Returns the number of new pseudonyms."""
    rx = re.compile(re.escape(prefix) + r"(\d+)$")
    nxt = max((int(m.group(1)) for v in pseudo_map.values() if (m := rx.match(v))), default=0)
    new = sorted(pid for pid in patient_ids if pid not in pseudo_map)
    for pid in new:
        nxt += 1
        pseudo_map[pid] = f"{prefix}{nxt:04d}"
    if path and (new or not os.path.exists(path)):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["original_patient_id", "pseudonym"])
            w.writerows(pseudo_map.items())
        chmod_private(path)
    return len(new)


def group_by_patient(src, workers):
    """{PatientID: [paths]} over all DICOM files under src (all studies of a patient together, so visit
    numbering is stable), plus counts for the log line."""
    files = [os.path.join(r, f) for r, _, fs in os.walk(src) for f in fs]
    groups, non_dicom = {}, []
    with ProcessPoolExecutor(workers) as ex:
        for path, key in ex.map(_study_key, files, chunksize=256):
            if key is None:
                non_dicom.append(path)
            else:
                groups.setdefault(key, []).append(path)
    print(f"files: {len(files)}  DICOM studies: {len(groups)}  non-DICOM files skipped: {len(non_dicom)}",
          flush=True)
    by_patient = {}
    for (pid, _), paths in groups.items():
        by_patient.setdefault(pid, []).extend(paths)
    return by_patient


def check_secret_paths(a):
    """Secret files (map, key, registry) must not end up inside the output or the input folder."""
    dst = os.path.realpath(a.dst)
    for name in ("pseudonym_map", "uid_key", "study_registry"):
        p = getattr(a, name, None)
        if p and os.path.realpath(p).startswith(dst + os.sep):
            sys.exit(f"--{name.replace('_', '-')} must live outside --dst (it is secret)")
    src = os.path.realpath(a.src)
    if dst == src or dst.startswith(src + os.sep):
        sys.exit("--dst must not be inside --src")


def prepare_pseudonyms(a, by_patient):
    pseudo_map = load_pseudonyms(a)
    if a.auto_pseudonyms:
        if not a.pseudonym_map:
            sys.exit("--auto-pseudonyms needs --pseudonym-map (the file the new pseudonyms are saved to)")
        save_to = None if getattr(a, "dry_run", False) else a.pseudonym_map
        n = assign_pseudonyms(pseudo_map, by_patient, save_to, a.auto_pseudonyms)
        print(f"pseudonyms: {n} new, {len(pseudo_map) - n} already in the map", flush=True)
    return pseudo_map


def run(a):
    check_secret_paths(a)
    key_hex = load_uid_key(a.uid_key)
    by_patient = group_by_patient(a.src, a.workers)
    pseudo_map = prepare_pseudonyms(a, by_patient)
    meta_dir = os.path.join(a.dst, "_deid_meta")
    os.makedirs(meta_dir, exist_ok=True)
    patients, timing, excluded, failed = [], [], [], 0
    registry = load_registry(a.study_registry)
    jobs = [(paths, a.config, pseudo_map, a.use_existing_patient_id, key_hex, a.dst, a.dry_run, registry)
            for paths in by_patient.values()]
    with ProcessPoolExecutor(a.workers) as ex:
        futs = [ex.submit(_process, j) for j in jobs]
        for k, fut in enumerate(as_completed(futs), 1):
            try:
                results, reg_rows = fut.result()
                for pseudo, key, idx, date in reg_rows:
                    registry.setdefault(pseudo, {})[key] = (idx, date)
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"[{k}/{len(jobs)}] ERROR\n{traceback.format_exc()}", flush=True)
                continue
            for prow, rows, excl, problems in results:
                patients.append(prow)
                timing += rows
                excluded += excl
                status = "OK" if not problems else f"VERIFY FAILED ({len(problems)})"
                failed += bool(problems)
                print(f"[{k}/{len(jobs)}] {prow['pseudonym']} study {prow['study']}: {prow['n_files']} files, "
                      f"ref={prow['timing_reference']}, positions={prow['n_temporal_positions']} {status}",
                      flush=True)
                for p in problems[:5]:
                    print(f"    {p}", flush=True)
    engine.write_csv(os.path.join(meta_dir, "patients.csv"), sorted(patients, key=lambda r: (r["pseudonym"], r["study"])))
    engine.write_csv(os.path.join(meta_dir, "series_timing.csv"),
                     sorted(timing, key=lambda r: (r["pseudonym"], r["study"], r["series_folder"])))
    engine.write_csv(os.path.join(meta_dir, "excluded.csv"), excluded)
    if a.study_registry and not a.dry_run:
        save_registry(a.study_registry, registry)
    print(f"done: {len(patients)} studies, failures: {failed}", flush=True)
    return 1 if failed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dicom-cloak", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--src", required=True)
    r.add_argument("--dst", required=True)
    r.add_argument("--config", default=os.environ.get("DICOM_CLOAK_CONFIG", "/opt/dicom_cloak/config/config.yaml"))
    r.add_argument("--pseudonym-map")
    r.add_argument("--use-existing-patient-id", action="store_true",
                   help="PatientID is already a pseudonym (must match pseudonym_regex)")
    r.add_argument("--auto-pseudonyms", metavar="PREFIX",
                   help="give patients missing from --pseudonym-map the next free PREFIXnnnn and save the map")
    r.add_argument("--uid-key", default="uid_key.hex", help="HMAC secret for UID remapping (created, mode 600)")
    r.add_argument("--study-registry", help="CSV carrying visit numbering/dates across runs (secret, outside --dst)")
    r.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    r.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        sys.exit(run(a))


if __name__ == "__main__":
    main()
