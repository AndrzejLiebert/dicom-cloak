"""De-identification engine for breast MRI DICOM (Siemens / Philips / GE).

Pipeline per study (patient + StudyInstanceUID):
  1. plan   (headers only) select series, drop rejected instances, classify series roles,
            build one joint DCE timing scheme, collect the study's original identifiers
  2. write  clean every kept file (pixel data untouched) and write it under neutral names
  3. verify re-read every written header: rules + byte search for the original identifiers

Policy files: config/standard_keep.yaml, config/standard_remove.yaml, config/vendor_private.yaml, config/config.yaml.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

import pydicom
import yaml

from .scrub import SCRUBBERS
from pydicom.dataset import Dataset
from pydicom.multival import MultiValue
from pydicom.sequence import Sequence
from pydicom.tag import Tag
from pydicom.uid import DeflatedExplicitVRLittleEndian, ExplicitVRLittleEndian

STANDARD_UID_ROOT = "1.2.840.10008."
REJECTION_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.88.59"  # Key Object Selection (rejection notes)
KEEP_UI_KEYWORDS = {
    "SOPClassUID", "ReferencedSOPClassUID", "MediaStorageSOPClassUID", "TransferSyntaxUID",
    "ReferencedTransferSyntaxUID", "ImplementationClassUID", "CodingSchemeUID", "ContextUID",
    "MappingResourceUID", "ContextGroupExtensionCreatorUID", "TemplateExtensionCreatorUID",
    "PrivateInformationCreatorUID", "RelatedGeneralSOPClassUID", "OriginalSpecializedSOPClassUID",
    "SOPClassesInStudy",
}
# Source values of these keywords are searched for (as bytes) in every written header.
IDENTIFYING_KEYWORDS = {
    "OtherPatientIDs", "PatientID", "IssuerOfPatientID", "AccessionNumber", "StudyID", "AdmissionID",
    "PlacerOrderNumberImagingServiceRequest", "FillerOrderNumberImagingServiceRequest", "RequestedProcedureID",
    "ScheduledProcedureStepID", "PerformedProcedureStepID", "DeviceSerialNumber", "StationName",
    "InstitutionName", "InstitutionAddress", "InstitutionalDepartmentName", "CurrentPatientLocation",
    "ReasonForStudy", "ReasonForTheRequestedProcedure", "RequestingService", "ModifyingSystem",
}
REWRITTEN_KEYWORDS = {"PatientIdentityRemoved", "DeidentificationMethod", "DeidentificationMethodCodeSequence",
                      "LongitudinalTemporalInformationModified"}
# Written by the engine after cleaning (allowed in the output even if a site trims the allowlist).
ENGINE_WRITTEN = REWRITTEN_KEYWORDS | {"PatientName", "PatientID", "TemporalPositionTimeOffset",
                                       "TemporalPositionIdentifier", "NumberOfTemporalPositions"}
# multi-valued LO (each value <= 64 characters)
DEID_METHOD = ["DICOM Cloak allowlist profile (Python engine)", "identifiers removed, private tags except vendor whitelist",
               "UIDs remapped (2.25 keyed hash)", "dates reduced to year, times relative to the study",
               "age grouped"]
DEID_CODES = [("113100", "Basic Application Confidentiality Profile"),
              ("113105", "Clean Descriptors Option"),
              ("113107", "Retain Longitudinal Temporal Information Modified Dates Option"),
              ("113108", "Retain Patient Characteristics Option"),
              ("113111", "Retain Safe Private Option")]
# Elements of the engine's own private block
PRIV_AGE_GROUP, PRIV_SECONDS, PRIV_ROLE, PRIV_TREF, PRIV_SERIES_TAG = 0x01, 0x02, 0x03, 0x05, 0x06
PRIV_STUDY_INDEX, PRIV_DAYS = 0x07, 0x08   # visit number, days since the patient's first visit
PRIV_JOINT_POSITION, PRIV_N_JOINT = 0x09, 0x0A   # joint DCE temporal position / count (vendor TPI untouched)


# ----------------------------------------------------------------------------- configuration
@dataclass
class Policy:
    remove_keywords: set
    private_keep: dict          # (group, creator_lower) -> {element_byte: name}
    output_private_group: int
    output_private_creator: str
    keep_sop_classes: set
    exclude_series_regex: list
    role_rules: list            # [(role, compiled regex)] applied to SeriesDescription/ProtocolName
    age_bins: list
    cluster_gap_s: float
    pseudonym_regex: re.Pattern
    extra_series_tag_regex: list = field(default_factory=list)  # [(name, regex)] e.g. a TTC=<s> value in the name
    derived_tokens: set = field(default_factory=set)             # ImageType tokens of computed maps
    derived_name_regex: re.Pattern | None = None
    private_scrub: dict = field(default_factory=dict)            # (group, creator_lower, elem) -> scrub method
    mode: str = "allowlist"                                      # "allowlist" (default) or "blocklist"
    relative_clock: str | None = "120000"                        # fake clock for kept TM attributes, or None
    relative_tm: set = field(default_factory=lambda: {"AcquisitionTime", "ContentTime"})
    allow_keywords: set = field(default_factory=set)
    allow_sequences: set = field(default_factory=set)

    def allowed(self, elem) -> bool:
        """Standard (non-private) element may stay? In blocklist mode everything not removed may stay."""
        if self.mode != "allowlist":
            return True
        kw = elem.keyword
        return kw in self.allow_sequences if elem.VR == "SQ" else kw in self.allow_keywords


def load_policy(config_path: str) -> Policy:
    base = os.path.dirname(os.path.abspath(config_path))
    cfg = yaml.safe_load(open(config_path))

    def rel(p):
        return p if os.path.isabs(p) else os.path.join(base, p)

    std = yaml.safe_load(open(rel(cfg["standard_remove"])))
    remove = set()
    for items in std["remove"].values():
        remove.update(items)
    vend = yaml.safe_load(open(rel(cfg["vendor_private"])))
    keep, scrub = {}, {}
    for vendor in cfg.get("vendors", ["siemens", "philips", "ge"]):
        for e in vend.get(vendor, {}).get("keep", []):
            keep.setdefault((int(e["group"]), e["creator"].strip().lower()), {})[int(e["element"])] = e["name"]
            if e.get("scrub"):
                if e["scrub"] not in SCRUBBERS:
                    raise ValueError(f"unknown scrub method {e['scrub']!r}")
                scrub[(int(e["group"]), e["creator"].strip().lower(), int(e["element"]))] = e["scrub"]
    mode = cfg.get("mode", "allowlist")
    allow_kw, allow_sq = set(), set()
    if mode == "allowlist":
        allow = yaml.safe_load(open(rel(cfg["standard_keep"])))
        for items in allow["keep"].values():
            allow_kw.update(items)
        allow_sq = set(allow["keep_sequences"])
        conflict = (allow_kw | allow_sq) & remove - REWRITTEN_KEYWORDS
        if conflict:  # the removal list always wins; refuse an inconsistent policy
            raise ValueError(f"attributes both allowed and removed: {sorted(conflict)}")
    elif mode != "blocklist":
        raise ValueError(f"unknown mode {mode!r}")
    rc = cfg.get("relative_clock", "120000")
    return Policy(
        private_scrub=scrub,
        mode=mode,
        relative_clock=None if rc in (None, "", False) else str(rc),
        relative_tm=set(cfg.get("relative_time_attributes", ["AcquisitionTime", "ContentTime"])),
        allow_keywords=allow_kw,
        allow_sequences=allow_sq,
        remove_keywords=remove,
        private_keep=keep,
        output_private_group=int(cfg.get("output_private_group", 0x0011)),
        output_private_creator=cfg.get("output_private_creator", "DICOM CLOAK 1.0"),
        keep_sop_classes=set(std["sop_classes"]["keep"]),
        exclude_series_regex=[re.compile(r, re.I) for r in cfg.get("exclude_series_regex", [])],
        role_rules=[(r["role"], re.compile(r["regex"], re.I)) for r in cfg["series_roles"]],
        age_bins=cfg.get("age_bins", [18, 30, 40, 50, 60, 70, 80]),
        cluster_gap_s=float(cfg.get("timing_cluster_gap_s", 2.0)),
        pseudonym_regex=re.compile(cfg.get("pseudonym_regex", r"^[A-Za-z0-9_-]{1,64}$")),
        extra_series_tag_regex=[(t["name"], re.compile(t["regex"])) for t in cfg.get("series_name_tags", [])],
        derived_tokens={t.upper() for t in cfg.get("derived_imagetype_tokens", [])},
        derived_name_regex=re.compile(cfg["derived_name_regex"], re.I) if cfg.get("derived_name_regex") else None,
    )


# ----------------------------------------------------------------------------- helpers
def age_group(age: str, bins: list) -> str:
    """'056Y' -> '51-60' for bins [18,30,40,...]: '<18', '18-30', '31-40', ..., '81+'."""
    s = str(age or "").strip()
    if len(s) < 4 or not s[:3].isdigit():
        return "unknown"
    years = int(s[:3]) if s[3].upper() == "Y" else 0
    if years < bins[0]:
        return f"<{bins[0]}"
    lo = bins[0]
    for hi in bins[1:]:
        if years <= hi:
            return f"{lo}-{hi}"
        lo = hi + 1
    return f"{lo}+"


def dt_seconds(value) -> float | None:
    s = re.split(r"[+-]", str(value).strip())[0]
    if len(s) < 14 or not s[:14].isdigit():
        return None
    base = datetime.strptime(s[:14], "%Y%m%d%H%M%S").timestamp()
    frac = s[14:]
    return base + (float(frac) if frac.startswith(".") and len(frac) > 1 else 0.0)


def acq_seconds(ds) -> float | None:
    """AcquisitionDateTime; else (Acquisition|Series|Study|Content)Date + (Acquisition|Content)Time;
    for Enhanced MR without top-level time: first frame's FrameAcquisitionDateTime."""
    if ds.get("AcquisitionDateTime"):
        t = dt_seconds(ds.AcquisitionDateTime)
        if t is not None:
            return t
    date = next((str(ds.get(k)) for k in ("AcquisitionDate", "SeriesDate", "StudyDate", "ContentDate")
                 if ds.get(k)), "")
    tm = next((str(ds.get(k)) for k in ("AcquisitionTime", "ContentTime") if ds.get(k)), "")
    if len(date) == 8 and len(tm) >= 6:
        return dt_seconds(date + tm)
    pf = ds.get("PerFrameFunctionalGroupsSequence")
    if pf:
        fc = pf[0].get("FrameContentSequence")
        if fc and fc[0].get("FrameAcquisitionDateTime"):
            t = dt_seconds(fc[0].FrameAcquisitionDateTime)
            if t is not None:
                return t
    # already de-identified input (dates = year only): relative seconds written by a previous run
    if ds.get("TemporalPositionTimeOffset") not in (None, ""):
        return float(ds.TemporalPositionTimeOffset)
    return None


TIME_SOURCES = ("AcquisitionDateTime", "FrameAcquisitionDateTime", "AcquisitionTime", "ContentTime",
                "TemporalPositionTimeOffset")


def time_sources(ds) -> dict:
    """All candidate acquisition times (seconds) of one header, by source name, in preference order.
    Vendors differ: e.g. Philips classic DCE may repeat the series start in AcquisitionTime for every
    dynamic while FrameAcquisitionDateTime / ContentTime carry the per-dynamic time."""
    out = {}
    if ds.get("AcquisitionDateTime"):
        t = dt_seconds(ds.AcquisitionDateTime)
        if t is not None:
            out["AcquisitionDateTime"] = t
    fdt = ds.get("FrameAcquisitionDateTime")
    if not fdt:
        pf = ds.get("PerFrameFunctionalGroupsSequence")
        fc = pf[0].get("FrameContentSequence") if pf else None
        fdt = fc[0].get("FrameAcquisitionDateTime") if fc else None
    if fdt and dt_seconds(fdt) is not None:
        out["FrameAcquisitionDateTime"] = dt_seconds(fdt)
    date = next((str(ds.get(k)) for k in ("AcquisitionDate", "SeriesDate", "StudyDate", "ContentDate")
                 if ds.get(k)), "")
    for src, cdate in (("AcquisitionTime", date), ("ContentTime", str(ds.get("ContentDate") or date))):
        tm = str(ds.get(src) or "")
        if len(cdate) == 8 and len(tm) >= 6 and dt_seconds(cdate + tm) is not None:
            out[src] = dt_seconds(cdate + tm)
    if ds.get("TemporalPositionTimeOffset") not in (None, ""):
        out["TemporalPositionTimeOffset"] = float(ds.TemporalPositionTimeOffset)
    return out


def choose_time_source(files_times: list, tpis: list):
    """Pick the source for one series: the first one present in every file that, when the series holds
    several temporal positions, gives a distinct start time per position. Returns (source or None)."""
    groups = {}
    for tp, times in zip(tpis, files_times):
        groups.setdefault(tp, []).append(times)
    for src in TIME_SOURCES:
        if not all(src in t for t in files_times):
            continue
        if len(groups) <= 1:
            return src
        starts = [min(t[src] for t in g) for g in groups.values()]
        if len({round(s, 1) for s in starts}) == len(groups):
            return src
    return None


def file_date(ds) -> str:
    for k in ("AcquisitionDate", "ContentDate", "SeriesDate", "StudyDate"):
        v = str(ds.get(k, "") or "")
        if len(v) == 8 and v.isdigit():
            return v
    v = str(ds.get("AcquisitionDateTime", "") or "")[:8]
    return v if len(v) == 8 and v.isdigit() else ""


class RelativeClock:
    """Maps an original time of day to base + (absolute time - study anchor), formatted as TM.
    Keeps every interval (slice timing, phase spacing) exact while hiding the real time of day."""

    def __init__(self, base_tm: str, anchor_abs: float):
        b = base_tm.ljust(6, "0")
        self.base = int(b[:2]) * 3600 + int(b[2:4]) * 60 + float(b[4:] or 0)
        self.anchor = anchor_abs

    def __call__(self, date: str, tm) -> str | None:
        t = dt_seconds(date + str(tm).strip()) if date else None
        if t is None:
            return None
        v = self.base + (t - self.anchor)
        if not 0 <= v < 86400:
            return None
        h, rem = divmod(v, 3600)
        m, sec = divmod(rem, 60)
        return f"{int(h):02d}{int(m):02d}{sec:09.6f}"


def acq_year(ds) -> str:
    for k in ("AcquisitionDateTime", "AcquisitionDate", "SeriesDate", "StudyDate", "ContentDate"):
        v = str(ds.get(k, "")).strip()
        if len(v) >= 4 and v[:4].isdigit():
            return v[:4]
    return ""


def classify(ds, policy: Policy) -> str:
    text = f"{ds.get('SeriesDescription', '')} {ds.get('ProtocolName', '')}"
    itype = "\\".join(str(x) for x in ds.get("ImageType", []))
    tokens = {t.upper() for t in itype.split("\\")}
    desc = str(ds.get("SeriesDescription", ""))
    if tokens & policy.derived_tokens or (policy.derived_name_regex and policy.derived_name_regex.search(desc)):
        return "derived_map"  # computed maps (TTP, wash-in, temporal MIP, ADC, ...) are not DCE phases
    if "MIP" in itype.upper():
        return "mip"
    if re.search(r"\bSUB\b|\\SUB(\\|$)", itype, re.I):
        return "subtraction"
    for role, rx in policy.role_rules:
        if rx.search(text):
            return role
    return "other"


class UIDMapper:
    def __init__(self, key: bytes):
        self.key = key

    def __call__(self, uid) -> str:
        uid = str(uid).strip()
        if not uid or uid.startswith(STANDARD_UID_ROOT):
            return uid
        h = hmac.new(self.key, uid.encode(), hashlib.sha256).digest()[:16]
        return "2.25." + str(int.from_bytes(h, "big"))


def _values(elem):
    return list(elem.value) if isinstance(elem.value, (list, MultiValue)) else [elem.value]


def private_creator_of(ds, tag: Tag) -> str:
    if tag.element < 0x0100:
        return ""
    ct = Tag(tag.group, tag.element >> 8)
    return str(ds[ct].value).strip() if ct in ds else ""


def is_whitelisted_private(ds, elem, policy: Policy) -> bool:
    tag = elem.tag
    if tag.element < 0x0100:  # creator element: kept only if its block holds a kept element (see clean)
        return False
    creator = private_creator_of(ds, tag).lower()
    return (tag.element & 0xFF) in policy.private_keep.get((tag.group, creator), {})


# ----------------------------------------------------------------------------- cleaning
def clean_dataset(ds: Dataset, uidmap: UIDMapper, policy: Policy, top: bool = True, clock=None,
                  date: str = "") -> None:
    """clock/date: RelativeClock of the study and the file's original date (for kept TM attributes)."""
    kept_blocks = set()
    for elem in list(ds):
        tag, kw, vr = elem.tag, elem.keyword, elem.VR
        if tag.is_private:
            if is_whitelisted_private(ds, elem, policy):
                method = policy.private_scrub.get((tag.group, private_creator_of(ds, tag).lower(), tag.element & 0xFF))
                if method:
                    raw = elem.value if isinstance(elem.value, (bytes, bytearray)) else str(elem.value).encode("latin-1", "ignore")
                    new = SCRUBBERS[method](bytes(raw))
                    if new is None:
                        del ds[tag]
                        continue
                    elem.value = new
                kept_blocks.add((tag.group, tag.element >> 8))
                if vr == "SQ":  # kept private sequence: apply all generic rules inside it
                    for item in elem.value:
                        clean_dataset(item, uidmap, policy, top=False, clock=clock, date=date)
                continue
            if tag.element >= 0x0100 or tag.element < 0x0010:
                del ds[tag]
            continue
        if (0x6000 <= tag.group <= 0x60FF) or (0x5000 <= tag.group <= 0x50FF) or kw in policy.remove_keywords \
                or not policy.allowed(elem):
            del ds[tag]
            continue
        if vr == "SQ":
            for item in elem.value:
                clean_dataset(item, uidmap, policy, top=False, clock=clock, date=date)
            continue
        if vr == "TM" and clock is not None and kw in policy.relative_tm:
            new = clock(date, elem.value)
            if new is None:
                del ds[tag]
            else:
                elem.value = new
            continue
        if vr == "PN":
            if not (top and kw == "PatientName"):
                del ds[tag]
            continue
        if vr in ("DA", "TM", "AE"):
            del ds[tag]
            continue
        if vr == "DT":
            v = str(elem.value).strip()
            elem.value = v[:4] if len(v) >= 4 else ""
            continue
        if vr == "UI" and kw not in KEEP_UI_KEYWORDS:
            vals = [uidmap(u) for u in _values(elem)]
            elem.value = vals if len(vals) != 1 else vals[0]
    # private creator elements: keep only those whose block still has whitelisted content
    for elem in list(ds):
        t = elem.tag
        if t.is_private and 0x0010 <= t.element <= 0x00FF and (t.group, t.element) not in kept_blocks:
            del ds[t]


def add_private(ds, policy: Policy, age_grp, seconds, role, tref, series_tags, study_index=None, days=None,
                joint_position=None, n_joint=None):
    block = ds.private_block(policy.output_private_group, policy.output_private_creator, create=True)
    if joint_position is not None:
        block.add_new(PRIV_JOINT_POSITION, "IS", str(joint_position))
        block.add_new(PRIV_N_JOINT, "IS", str(n_joint))
    if study_index is not None:
        block.add_new(PRIV_STUDY_INDEX, "IS", str(study_index))
    if days is not None:
        block.add_new(PRIV_DAYS, "IS", str(days))
    block.add_new(PRIV_AGE_GROUP, "LO", age_grp)
    block.add_new(PRIV_ROLE, "LO", role)
    block.add_new(PRIV_TREF, "LO", tref)
    if seconds is not None:
        block.add_new(PRIV_SECONDS, "DS", f"{seconds:.3f}")
    if series_tags:
        block.add_new(PRIV_SERIES_TAG, "LO", ";".join(f"{k}={v}" for k, v in series_tags.items()))


# ----------------------------------------------------------------------------- verification
def new_uids(ds) -> set:
    out = set()

    def walk(d):
        for e in d:
            if e.VR == "SQ":
                for it in e.value:
                    walk(it)
            elif e.VR == "UI":
                out.update(str(u) for u in _values(e) if str(u).startswith("2.25."))

    walk(ds)
    if hasattr(ds, "file_meta"):
        walk(ds.file_meta)
    return out


def verify_file(path: str, forbidden: dict, pseudonym: str, policy: Policy) -> list:
    """Rules + byte search of the header (not pixel data) for original identifier values."""
    problems = []
    with open(path, "rb") as fh:
        ds = pydicom.dcmread(fh, stop_before_pixels=True, force=True)
        end = fh.tell()
        fh.seek(0)
        raw = fh.read(end)
    for uid in new_uids(ds):  # our random 2.25 digits can contain short originals by chance
        raw = raw.replace(uid.encode(), b"#" * len(uid))
    for b, kw in forbidden.items():
        if b in raw:
            problems.append(f"original value of {kw} in header bytes (len={len(b)})")

    def walk(d, top):
        for e in d:
            if e.tag.is_private:
                if top and e.tag.group == policy.output_private_group and e.tag.element >= 0x0010 and \
                        private_creator_of(d, e.tag) in ("", policy.output_private_creator):
                    continue
                if e.tag.element >= 0x0100 and not is_whitelisted_private(d, e, policy):
                    problems.append(f"non-whitelisted private tag {e.tag}")
                if e.VR != "SQ":
                    continue
            if top and e.keyword in REWRITTEN_KEYWORDS:
                continue
            if not e.tag.is_private and not policy.allowed(e) and not (top and e.keyword in ENGINE_WRITTEN):
                problems.append(f"not allowlisted {e.keyword or e.tag}")
            if e.VR == "SQ":
                for it in e.value:
                    walk(it, False)
            elif e.VR == "PN" and not (top and e.keyword == "PatientName"):
                problems.append(f"PN {e.keyword}")
            elif e.VR in ("DA", "AE") or (e.VR == "TM" and not (policy.relative_clock and e.keyword in policy.relative_tm)):
                problems.append(f"{e.VR} {e.keyword}")
            elif e.VR == "DT" and len(str(e.value).strip()) > 4:
                problems.append(f"DT {e.keyword}")
            elif e.VR == "UI" and e.keyword not in KEEP_UI_KEYWORDS:
                for u in _values(e):
                    if u and not (str(u).startswith("2.25.") or str(u).startswith(STANDARD_UID_ROOT)):
                        problems.append(f"UID not remapped {e.keyword}")
            elif e.keyword in policy.remove_keywords:
                problems.append(f"removed keyword present {e.keyword}")

    walk(ds, True)
    if str(ds.get("PatientName", "")) != pseudonym or str(ds.get("PatientID", "")) != pseudonym:
        problems.append("PatientName/PatientID not the pseudonym")
    if ds.get("PatientIdentityRemoved") != "YES":
        problems.append("PatientIdentityRemoved not YES")
    return problems


def collect_forbidden(ds, out: dict, keep_values: set):
    """Original identifying values of one header -> out[bytes] = keyword (recursive, incl. file meta AE)."""
    def walk(d):
        for e in d:
            if e.VR == "SQ":
                for it in e.value:
                    walk(it)
                continue
            kw = e.keyword or str(e.tag)
            ident = (e.VR in ("PN", "DA", "DT", "TM", "AE") or kw in IDENTIFYING_KEYWORDS
                     or (e.VR == "UI" and kw not in KEEP_UI_KEYWORDS))
            if not ident:
                continue
            for v in _values(e):
                s = str(v).strip()
                if re.fullmatch(r"[0.:\- ]*", s) or len(set(s)) == 1:
                    continue  # placeholders such as 000000 / 00000000 carry no identity (and match everywhere)
                if s and s not in keep_values and not s.startswith(STANDARD_UID_ROOT) and (
                        len(s) >= 6 or (e.VR == "PN" and len(s) >= 4)):
                    out[s.encode("utf-8", "ignore")] = kw
    walk(ds)
    if hasattr(ds, "file_meta"):
        walk(ds.file_meta)


# ----------------------------------------------------------------------------- planning
@dataclass
class SeriesPlan:
    uid: str
    desc: str
    role: str
    files: list                 # [(path, header-time or None, original TPI or None, sop_uid)]
    series_number: object
    out_folder: str
    series_tags: dict
    positions: dict = field(default_factory=dict)   # path -> joint temporal position
    times: dict = field(default_factory=dict)       # path -> {source: seconds}
    time_source: str = ""                           # chosen source, or "" (order from TPI only)


@dataclass
class StudyPlan:
    pseudonym: str
    study_index: int
    series: list
    excluded: list
    forbidden: dict
    t0: float | None
    tref: str
    n_positions: int
    age: str
    sex: str
    year: str
    days_since_first: int | None = None   # whole days since the patient's first study (relative only)
    timepoint: str = ""                   # ClinicalTrialTimePointID (e.g. T0..T3), if present
    study_key: str = ""                   # remapped StudyInstanceUID (identifies the visit in the registry)
    study_date: str = ""                  # original YYYYMMDD: ONLY for the secret registry, never written
    clock_anchor: float | None = None     # absolute time of the study's first acquisition (relative clock)


def study_date(items):
    """Earliest original date (YYYYMMDD) and absolute time (s) of a study's headers, for visit ordering."""
    best_t, best_d = None, None
    for _, ds in items:
        for src, t in time_sources(ds).items():
            if src != "TemporalPositionTimeOffset" and (best_t is None or t < best_t):
                best_t = t
        for k in ("AcquisitionDateTime", "AcquisitionDate", "SeriesDate", "ContentDate", "StudyDate"):
            v = str(ds.get(k, "") or "")[:8]
            if len(v) == 8 and v.isdigit() and (best_d is None or v < best_d):
                best_d = v
    if best_t is None and best_d:
        best_t = datetime.strptime(best_d, "%Y%m%d").timestamp()
    return best_d, best_t


def _days_between(d0: str, d1: str) -> int | None:
    if not (d0 and d1):
        return None
    return (datetime.strptime(d1, "%Y%m%d") - datetime.strptime(d0, "%Y%m%d")).days


def _safe(s: str, n=64) -> str:
    return re.sub(r"[^A-Za-z0-9._=+-]+", "_", s).strip("_")[:n] or "series"


def scan_tree(src: str):
    """Yield (path, header) for every DICOM file under src; non-DICOM files are reported."""
    for root, _, files in os.walk(src):
        for f in sorted(files):
            p = os.path.join(root, f)
            try:
                ds = pydicom.dcmread(p, stop_before_pixels=True)
            except Exception:  # noqa: BLE001 - non-DICOM (viewer databases, thumbnails, .DS_Store, ...)
                yield p, None
                continue
            yield p, ds


def plan_studies(headers, pseudonym_of, policy: Policy, registry: dict | None = None, uidmap=None):
    """headers: list of (path, ds). pseudonym_of(PatientID) -> pseudonym or None.

    Multiple visits of one patient: studies are numbered chronologically (original acquisition date/time,
    read before dates are removed) and get days_since_first. `registry` ({pseudonym: {study_key:
    (index, YYYYMMDD)}}, kept outside the output) carries numbering and the first-visit date across runs:
    known visits keep their number, new visits get the next free one."""
    studies = {}
    rejected = set()
    for p, ds in headers:
        if str(ds.get("SOPClassUID", "")) == REJECTION_SOP_CLASS:
            full = pydicom.dcmread(p, force=True)
            for ev in full.get("CurrentRequestedProcedureEvidenceSequence", []):
                for rs in ev.get("ReferencedSeriesSequence", []):
                    for r in rs.get("ReferencedSOPSequence", []):
                        rejected.add(str(r.ReferencedSOPInstanceUID))
    for p, ds in headers:
        key = (str(ds.get("PatientID", "")), str(ds.get("StudyInstanceUID", "")))
        studies.setdefault(key, []).append((p, ds))
    plans = []
    registry = registry if registry is not None else {}
    visits = {}   # pseudonym -> [(abs_time, study_key, date, (pid, study_uid))]
    anchors = {}  # (pid, study_uid) -> absolute time of the study's first acquisition
    for (pid, study_uid), items in studies.items():
        pseudo = pseudonym_of(pid)
        if pseudo is None:
            raise ValueError("no pseudonym for a PatientID (mapping file incomplete)")
        if not policy.pseudonym_regex.match(pseudo):
            raise ValueError(f"pseudonym does not match pseudonym_regex: {pseudo!r}")
        d, t = study_date(items)
        key = uidmap(study_uid) if uidmap else hashlib.sha256(study_uid.encode()).hexdigest()[:32]
        visits.setdefault(pseudo, []).append((t if t is not None else float("inf"), key, d or "", (pid, study_uid)))
        anchors[(pid, study_uid)] = t
    visit_info = {}
    for pseudo, vs in visits.items():
        reg = registry.setdefault(pseudo, {})
        nxt = max((i for i, _ in reg.values()), default=0)
        for t, key, d, sk in sorted(vs):
            if key not in reg:
                nxt += 1
                reg[key] = (nxt, d)
        # day 0 = the visit registered first (index 1). Never re-anchor: offsets already released must not
        # change. A visit that predates it but arrives in a later run gets negative days and the next number.
        anchor = min(reg.values(), key=lambda v: v[0])[1] if reg else ""
        for t, key, d, sk in vs:
            visit_info[sk] = (pseudo, reg[key][0], _days_between(anchor, d), key, d)
    for (pid, study_uid), items in sorted(studies.items(), key=lambda kv: visit_info[kv[0]][:2]):
        pseudo, idx, days, study_key, sdate = visit_info[(pid, study_uid)]
        forbidden, excluded, series = {}, [], {}
        meta = {"age": "", "sex": "", "year": "",
                "timepoint": next((str(ds.get("ClinicalTrialTimePointID")) for _, ds in items
                                   if ds.get("ClinicalTrialTimePointID")), "")}
        for p, ds in items:
            collect_forbidden(ds, forbidden, keep_values={pseudo, policy.output_private_creator})
            sop_class = str(ds.get("SOPClassUID", ""))
            desc = str(ds.get("SeriesDescription", "") or ds.get("ProtocolName", ""))
            if sop_class not in policy.keep_sop_classes:
                excluded.append((desc, f"SOP class {getattr(ds.get('SOPClassUID'), 'name', sop_class)}"))
                continue
            if any(rx.search(desc) for rx in policy.exclude_series_regex):
                excluded.append((desc, "series excluded by exclude_series_regex"))
                continue
            if str(ds.get("SOPInstanceUID", "")) in rejected:
                excluded.append((desc, "instance rejected by a rejection note"))
                continue
            suid = str(ds.get("SeriesInstanceUID", ""))
            if suid not in series:
                tags = {}
                for name, rx in policy.extra_series_tag_regex:
                    m = rx.search(desc)
                    if m:
                        tags[name] = m.group(1)
                series[suid] = SeriesPlan(suid, desc, classify(ds, policy), [], ds.get("SeriesNumber"),
                                          "", tags)
            tpi = ds.get("TemporalPositionIdentifier")
            series[suid].files.append((p, None, int(tpi) if tpi not in (None, "") else None,
                                       str(ds.get("SOPInstanceUID", ""))))
            series[suid].times[p] = time_sources(ds)
            meta["age"] = meta["age"] or str(ds.get("PatientAge", ""))
            meta["sex"] = meta["sex"] or str(ds.get("PatientSex", ""))
            meta["year"] = meta["year"] or acq_year(ds)
        # dedupe excluded rows per series description
        excluded = sorted({(d, r): sum(1 for x in excluded if x == (d, r)) for d, r in excluded}.items())
        for s in series.values():
            src = choose_time_source([s.times[f[0]] for f in s.files], [f[2] for f in s.files])
            s.time_source = src or ""
            s.files = [(p, s.times[p].get(src) if src else None, tpi, sop) for p, _, tpi, sop in s.files]
        t0, tref, n_pos = build_timing(list(series.values()), policy)
        used = set()
        for s in sorted(series.values(), key=lambda s: (int(s.series_number or 0), s.desc)):
            name = _safe(f"{int(s.series_number or 0):04d}_{s.desc}")
            while name in used:
                name += "_"
            used.add(name)
            s.out_folder = name
        plans.append(StudyPlan(pseudo, idx, sorted(series.values(), key=lambda s: s.out_folder),
                               [(d, r, n) for (d, r), n in excluded], forbidden, t0, tref, n_pos,
                               meta["age"], meta["sex"], meta["year"], days, meta["timepoint"], study_key, sdate,
                               anchors.get((pid, study_uid))))
    return plans


def build_timing(series: list, policy: Policy):
    """One joint scheme over reference + dynamic series.

    Volumes are clusters of per-file times: by original TemporalPositionIdentifier when the series has
    several (Philips/GE style, dynamics inside one series), otherwise by time gaps > cluster_gap_s
    (Siemens style, one volume per series). Returns (t0, reference_name, n_positions) and fills
    SeriesPlan.positions for reference/dynamic/subtraction files.
    """
    # series whose temporal positions cannot be timed (no source separates them): keep the vendor's
    # TemporalPositionIdentifier order, series-local, with no seconds (never invent 0 s offsets)
    tpi_only_max = 0
    for s in series:
        tps = {f[2] for f in s.files if f[2] is not None}
        if s.role in ("reference", "dynamic") and not s.time_source and len(tps) > 1:
            for p, _, tp, _ in s.files:
                s.positions[p] = tp
            tpi_only_max = max(tpi_only_max, max(tps))
    refs = [t for s in series if s.role == "reference" for _, t, _, _ in s.files if t is not None]
    dyn = [t for s in series if s.role == "dynamic" for _, t, _, _ in s.files if t is not None]
    if refs:
        t0, tref = min(refs), "reference_series"
    elif dyn:
        t0, tref = min(dyn), "first_dynamic_volume"
    else:
        return None, ("temporal_position_identifier_only" if tpi_only_max else "none"), tpi_only_max
    volumes = []  # (start_time, [paths])
    for s in series:
        if s.role not in ("reference", "dynamic"):
            continue
        files = [f for f in s.files if f[1] is not None]
        tpis = {f[2] for f in files if f[2] is not None}
        if len(tpis) > 1:
            groups = {}
            for f in files:
                groups.setdefault(f[2], []).append(f)
            clusters = list(groups.values())
        else:
            clusters, cur = [], []
            for f in sorted(files, key=lambda f: f[1]):
                if cur and f[1] - cur[-1][1] > policy.cluster_gap_s:
                    clusters.append(cur)
                    cur = []
                cur.append(f)
            if cur:
                clusters.append(cur)
        for c in clusters:
            volumes.append((min(f[1] for f in c), [f[0] for f in c]))
    starts = []
    for t, _ in sorted(volumes):  # volumes starting within cluster_gap_s (e.g. Dixon F/W) share a position
        if not starts or t - starts[-1] > policy.cluster_gap_s:
            starts.append(t)

    def pos_of(t):
        hits = [i for i, st in enumerate(starts) if abs(st - t) <= policy.cluster_gap_s]
        return hits[0] + 1 if hits else None

    owner = {f[0]: s for s in series for f in s.files}
    for t, paths in volumes:
        for p in paths:
            owner[p].positions[p] = pos_of(t)
    for s in series:  # subtraction images take the position of the volume acquired at their time
        if s.role == "subtraction":
            for p, t, _, _ in s.files:
                if t is not None:
                    s.positions[p] = pos_of(t)
    return t0, tref, max(len(starts), tpi_only_max)


# ----------------------------------------------------------------------------- writing
def study_dir_of(plan: StudyPlan) -> str:
    """Output folder of a study, relative to the destination root."""
    return os.path.join(plan.pseudonym, f"study_{plan.study_index:02d}")


def file_seconds(plan: StudyPlan, t) -> float | None:
    return None if (t is None or plan.t0 is None) else round(t - plan.t0, 3)


def output_name(instance_number, k: int) -> str:
    """Neutral file name (never derived from the source file name)."""
    return f"IM_{int(instance_number):05d}_{k:05d}.dcm" if instance_number not in (None, "") else f"IM_{k:05d}.dcm"


def series_row(plan: StudyPlan, s: SeriesPlan, offsets: list) -> dict:
    valid = [o for o in offsets if o is not None]
    positions = sorted({p for p in s.positions.values() if p is not None})
    return {"pseudonym": plan.pseudonym, "study": plan.study_index, "series_folder": s.out_folder,
            "role": s.role, "temporal_positions": " ".join(map(str, positions)),
            "n_temporal_positions": plan.n_positions if positions else "",
            "seconds_after_reference_start": f"{min(valid):.1f}" if valid else "",
            "seconds_after_reference_end": f"{max(valid):.1f}" if valid else "",
            "series_name_tags": ";".join(f"{k}={v}" for k, v in s.series_tags.items()),
            "time_source": s.time_source or ("TemporalPositionIdentifier only (series-local order, no seconds)"
                                             if positions else ""),
            "timing_reference": plan.tref, "n_files": len(s.files)}


def patient_row(plan: StudyPlan, agrp: str, nfiles: int, nproblems: int) -> dict:
    return {"pseudonym": plan.pseudonym, "study": plan.study_index,
            "days_since_first_study": "" if plan.days_since_first is None else plan.days_since_first,
            "timepoint_id": plan.timepoint, "sex": plan.sex, "age_group": agrp,
            "acquisition_year": plan.year, "timing_reference": plan.tref,
            "n_temporal_positions": plan.n_positions, "n_series": len(plan.series), "n_files": nfiles,
            "n_problems": nproblems}


def excluded_rows(plan: StudyPlan) -> list:
    return [{"pseudonym": plan.pseudonym, "study": plan.study_index, "series_description": d, "reason": r,
             "n_files": n} for d, r, n in plan.excluded]


def inflate(ds) -> None:
    """Deflated files are written inflated: the byte search of verify_file must see the real header bytes
    (and dcm2niix cannot read deflated files). The pixel data of a deflated file is native."""
    if hasattr(ds, "file_meta") and ds.file_meta.get("TransferSyntaxUID") == DeflatedExplicitVRLittleEndian:
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian


def write_study(plan: StudyPlan, dst: str, uidmap: UIDMapper, policy: Policy, dry_run=False):
    agrp = age_group(plan.age, policy.age_bins)
    study_dir = os.path.join(dst, study_dir_of(plan))
    rows, problems, nfiles = [], [], 0
    for s in plan.series:
        out_dir = os.path.join(study_dir, s.out_folder)
        if not dry_run:
            os.makedirs(out_dir, exist_ok=True)
        offsets = []
        for k, (path, t, _, _) in enumerate(sorted(s.files, key=lambda f: f[0]), 1):
            ds = pydicom.dcmread(path, force=True)
            sec = file_seconds(plan, t)
            offsets.append(sec)
            fdate = file_date(ds)
            clock = (RelativeClock(policy.relative_clock, plan.clock_anchor)
                     if policy.relative_clock and plan.clock_anchor is not None else None)
            clean_dataset(ds, uidmap, policy, clock=clock, date=fdate)
            ds.PatientName = plan.pseudonym
            ds.PatientID = plan.pseudonym
            ds.PatientIdentityRemoved = "YES"
            ds.DeidentificationMethod = DEID_METHOD
            seq = Sequence()
            for code, meaning in DEID_CODES:
                it = Dataset()
                it.CodeValue, it.CodingSchemeDesignator, it.CodeMeaning = code, "DCM", meaning
                seq.append(it)
            ds.DeidentificationMethodCodeSequence = seq
            ds.LongitudinalTemporalInformationModified = "MODIFIED"
            if sec is not None:
                ds.TemporalPositionTimeOffset = float(sec)
            # The vendor's TemporalPositionIdentifier / NumberOfTemporalPositions are left untouched:
            # converters (dcm2niix) use them to assemble 4D volumes. The joint scheme goes to the private block.
            pos = s.positions.get(path)
            add_private(ds, policy, agrp, sec, s.role, plan.tref, s.series_tags, plan.study_index,
                        plan.days_since_first, pos, plan.n_positions if pos is not None else None)
            if hasattr(ds, "file_meta"):
                ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
                inflate(ds)
                for key in list(ds.file_meta.keys()):
                    e = ds.file_meta[key]
                    if e.tag.is_private or e.VR == "AE" or e.tag.element >= 0x0100:
                        del ds.file_meta[key]
            nfiles += 1
            if dry_run:
                continue
            out = os.path.join(out_dir, output_name(ds.get("InstanceNumber"), k))
            ds.save_as(out, enforce_file_format=False)
            for pr in verify_file(out, plan.forbidden, plan.pseudonym, policy):
                problems.append(f"{os.path.relpath(out, dst)}: {pr}")
        rows.append(series_row(plan, s, offsets))
    return patient_row(plan, agrp, nfiles, len(problems)), rows, excluded_rows(plan), problems


def write_csv(path: str, rows: list):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
