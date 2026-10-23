"""Allowlist scrubbers for Siemens protocol blobs (MrPhoenixProtocol / ASCCONV).

Siemens stores the full measurement protocol as text inside private elements:
  - XA/XB (Vida, Terra.X, ...): (0021,xx19) 'SIEMENS MR SDS 01' - XProtocol header + ### ASCCONV ### block
    (in Enhanced MR objects nested in SharedFunctionalGroupsSequence > (0021,xxFE))
  - VB/VD/VE: (0029,xx20) 'SIEMENS CSA HEADER' - binary CSA2 ('SV10') container, element MrPhoenixProtocol
The blobs carry identifiers (device serial number, exam date and original UIDs via tReferenceImageN, free
text). dcm2niix reads phase-encode polarity, readout timing, acceleration, diffusion scheme, multiband and
coil information from the ASCCONV block, so the block is kept but FILTERED:
  - only the ASCCONV section survives (XProtocol header and tail are dropped)
  - a line survives only if its key belongs to a technical group (ASCCONV_KEEP_GROUPS) or is an explicitly
    allowed top-level key, AND its value is numeric - except for the few keys in STRING_KEYS
Everything else (tReferenceImage*, free text, unknown groups) is dropped. The engine's byte search for
original identifiers runs on the result like on any other kept value.
Key list derived from the keys dcm2niix reads (console/nii_dicom_batch.cpp, siemensCsaAscii) plus the
technical parameter groups around them.
"""
from __future__ import annotations

import re
import struct

ASCCONV_KEEP_GROUPS = {
    "sKSpace", "sPat", "sSliceArray", "sGroupArray", "sFastImaging", "sDiffusion", "sRXSPEC", "sTXSPEC",
    "sGRADSPEC", "sPrepPulses", "sSliceAcceleration", "sAsl", "sIR", "sDixonData", "sRSatArray", "sSpecPara",
    "sAngio", "sBladePara", "sRawFilter", "sPreScanNormalizeFilter", "sAdjData", "sNavigatorPara",
    "sPhysioImaging", "sCommonIterRecon", "sChannelMatrix", "sWipMemBlock", "sWiPMemBlock",
    "sCoilSelectMeas", "sCoilElementID", "sProtConsistencyInfo", "sParametricMapping",
}
ASCCONV_KEEP_TOP = {
    "alTR", "alTE", "alTI", "alTD", "adFlipAngleDegree", "acFlowComp", "lContrasts", "lInvContrasts",
    "lCombinedEchoes", "lAverages", "dAveragesDouble", "lRepetitions", "lDelayTimeInTR", "ucReadOutMode",
    "ucCoilCombineMode", "ucReconstructionMode", "lTotalScanTimeSec", "lScanTimeSec", "ucOneSeriesForAllMeas",
    "ucEnableNoiseAdjust", "ulVersion", "lScanRegionPosTra", "ucScanRegionPosValid", "lPtabAbsStartPosZ",
    "bPtabAbsStartPosZValid", "ucAAMode", "ucDisableChangeStoreImages",
}
# string-valued keys allowed (all other quoted values are dropped)
STRING_KEYS = {
    "tProtocolName", "tSequenceFileName", "sProtConsistencyInfo.tBaselineString",
    "sProtConsistencyInfo.tMeasuredBaselineString", "sCoilSelectMeas.sCoilStringForConversion",
}
STRING_KEY_PATTERNS = [re.compile(r"^sCoilElementID\.tCoilID$"),
                       re.compile(r"^sCoilSelectMeas\.aRxCoilSelectData\[\d+\]\.asList\[\d+\]\.sCoilElementID\.tCoilID$"),
                       re.compile(r"^sCoilSelectMeas\.aRxCoilSelectData\[\d+\]\.asList\[\d+\]\.sCoilElementID\.tElement$")]
NUMERIC = re.compile(r"^[-+]?(0x[0-9A-Fa-f]+|\d+(\.\d*)?([eE][-+]?\d+)?|\.\d+([eE][-+]?\d+)?)$")
BEGIN, END = "### ASCCONV BEGIN", "### ASCCONV END"


def _group(key: str) -> str:
    return re.split(r"[.\[]", key, maxsplit=1)[0]


def filter_ascconv(text: str) -> str | None:
    """Return the filtered '### ASCCONV BEGIN ... END ###' block, or None if there is none."""
    i0 = text.find(BEGIN)
    if i0 < 0:
        return None
    i1 = text.find(END, i0)
    if i1 < 0:
        return None
    end_line = text[i1:].split("\n", 1)[0]
    lines = text[i0:i1].split("\n")
    out = [lines[0].rstrip("\r")]                    # BEGIN line (object/version/converter tags)
    for line in lines[1:]:
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.split("#", 1)[0].strip()
        group = _group(key)
        if group not in ASCCONV_KEEP_GROUPS and group not in ASCCONV_KEEP_TOP and key not in STRING_KEYS:
            continue
        if value.startswith('"'):
            if not (key in STRING_KEYS or any(p.match(key) for p in STRING_KEY_PATTERNS)):
                continue
            is_baseline = key.startswith("sProtConsistencyInfo.t")   # software build string (version detection)
            if (re.search(r"\d{8}", value) and not is_baseline) or len(value) > 66:   # dates / long text
                continue
        elif not NUMERIC.match(value):
            continue
        out.append(f"{key} = {value}")
    out.append(end_line.rstrip("\r"))
    return "\n".join(out) + "\n"


def siemens_ascconv(blob: bytes) -> bytes | None:
    """XA/XB protocol element: plain text (XProtocol + ASCCONV). Returns the filtered ASCCONV only."""
    text = bytes(blob).decode("latin-1")
    f = filter_ascconv(text)
    if f is None:
        return None
    # Keep a (content-free) XProtocol opener: dcm2niix skips the FIRST BYTE of a text protocol blob
    # (phoenixOffsetCSASeriesHeader returns EXIT_FAILURE=1, used as offset), so a blob starting directly
    # with '### ASCCONV BEGIN' would not be recognised and the whole protocol would be ignored.
    b = ("<XProtocol>\n" + f).encode("latin-1")
    return b + b"\x00" if len(b) % 2 else b


# ----------------------------------------------------------------------------- CSA2 (SV10) container
def _read_csa2(blob: bytes):
    if blob[:4] != b"SV10":
        return None
    n_tags = struct.unpack_from("<I", blob, 8)[0]
    pos, tags = 16, []
    for _ in range(n_tags):
        name, vm, vr, syngodt, n_items, last = struct.unpack_from("<64si4s3i", blob, pos)
        pos += 84
        items = []
        for _ in range(n_items):
            x0, x1, x2, x3 = struct.unpack_from("<4i", blob, pos)
            pos += 16
            items.append(blob[pos:pos + x1])
            pos += x1 + ((4 - x1 % 4) % 4)
        tags.append([name.split(b"\x00", 1)[0].decode("latin-1"), vm, vr, syngodt, last, items])
    return tags


def _write_csa2(tags) -> bytes:
    out = bytearray(b"SV10\x04\x03\x02\x01" + struct.pack("<2I", len(tags), 77))
    for name, vm, vr, syngodt, last, items in tags:
        out += struct.pack("<64si4s3i", name.encode("latin-1"), vm, vr, syngodt, len(items), last)
        for it in items:
            n = len(it)
            out += struct.pack("<4i", n, n, 77, n) + it + b"\x00" * ((4 - n % 4) % 4)
    return bytes(out)


CSA_SERIES_KEEP = {"MrPhoenixProtocol", "MrProtocolVersion", "MrProtocol"}


def siemens_csa_series(blob: bytes) -> bytes | None:
    """CSA series header: keep only the protocol elements, with their ASCCONV filtered."""
    tags = _read_csa2(bytes(blob))
    if tags is None:
        return None
    kept = []
    for t in tags:
        if t[0] not in CSA_SERIES_KEEP:
            continue
        if t[0] in ("MrPhoenixProtocol", "MrProtocol"):
            new_items = []
            for it in t[5]:
                f = filter_ascconv(it.split(b"\x00", 1)[0].decode("latin-1"))
                if f is not None:
                    new_items.append(f.encode("latin-1") + b"\x00")
            if not new_items:
                continue
            t = t[:5] + [new_items]
        kept.append(t)
    return _write_csa2(kept) if kept else None


SCRUBBERS = {"siemens_ascconv": siemens_ascconv, "siemens_csa_series": siemens_csa_series}
