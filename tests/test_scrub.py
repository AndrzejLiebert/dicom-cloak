"""Siemens protocol scrubber: identifiers out, technical ASCCONV keys in, CSA2 container stays valid."""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from dicom_cloak import scrub  # noqa: E402

SERIAL, DATE = "175921", "20250314"
ASCCONV = f"""### ASCCONV BEGIN object=MrProtDataImpl@MrProtocolData version=51130001 converter=%MEASCONST%/ConverterList/Prot_Converter.txt ###
ulVersion\t = 0x14b44b6
tSequenceFileName\t = ""%SiemensSeq%\\ep2d_diff""
tProtocolName\t = ""ep2d_diff_tra""
tReferenceImage0\t = ""1.3.12.2.1107.5.2.43.{SERIAL}.{DATE}101500123456""
sProtConsistencyInfo.tBaselineString\t = ""N4_VE11C_LATEST_20160120""
sKSpace.lBaseResolution\t = 128
sKSpace.ucPhasePartialFourier\t = 0x10
sPat.lAccelFactPE\t = 2
sDiffusion.dsScheme\t = 1
sFastImaging.lEchoSpacing\t = 560
sSliceArray.asSlice[0].dInPlaneRot\t = 3.14159
sSliceArray.ucMode\t = 0x4
alTR[0]\t = 8000000
sWipMemBlock.tFree\t = ""operator note: Jane Doe""
sCoilSelectMeas.sCoilStringForConversion\t = ""Breast_18""
sStudyComment\t = ""free text {DATE}""
lPatientWeight\t = 68
### ASCCONV END ###
"""
XPROTOCOL = '<XProtocol> \n{\n  <Name> "PhoenixMetaProtocol" \n  <ParamString."PatientName"> { "Doe^Jane" }\n}\n'


def _check(text):
    assert SERIAL not in text and DATE + "1015" not in text and "Doe" not in text and "Jane" not in text
    assert "tReferenceImage0" not in text and "sStudyComment" not in text and "lPatientWeight" not in text
    assert "sWipMemBlock.tFree" not in text
    for k in ("sKSpace.lBaseResolution", "sPat.lAccelFactPE", "sDiffusion.dsScheme", "sFastImaging.lEchoSpacing",
              "sSliceArray.ucMode", "alTR[0]", "tProtocolName", "tSequenceFileName",
              "sProtConsistencyInfo.tBaselineString", "sCoilSelectMeas.sCoilStringForConversion"):
        assert k in text, k
    body = text[text.index("### ASCCONV BEGIN"):]
    assert body.rstrip("\x00\n").endswith("### ASCCONV END ###")
    assert text.count("<XProtocol>") <= 1 and "PhoenixMetaProtocol" not in text


def test_xa_blob():
    out = scrub.siemens_ascconv((XPROTOCOL + ASCCONV + "\n</XProtocol>\n").encode("latin-1"))
    assert len(out) % 2 == 0
    assert out[1:].find(b"### ASCCONV BEGIN") > 0      # still found after dcm2niix skips the first byte
    _check(out.decode("latin-1"))


def _csa2(tags):
    return scrub._write_csa2([[n, 1, b"UN\x00\x00", 1, 77, [v]] for n, v in tags])


def test_csa_series_header_rebuilt_and_valid():
    nib = __import__("pytest").importorskip("nibabel")
    from nibabel.nicom import csareader
    blob = _csa2([("UsedPatientWeight", b"68\x00"), ("MrPhoenixProtocol", (XPROTOCOL + ASCCONV).encode("latin-1") + b"\x00"),
                  ("MrProtocolVersion", b"51130001\x00"), ("SeriesWorkflowStatus", b"Doe^Jane\x00")])
    out = scrub.siemens_csa_series(blob)
    parsed = csareader.read(out)                       # independent parser accepts the rebuilt container
    assert set(parsed["tags"]) == {"MrPhoenixProtocol", "MrProtocolVersion"}
    _check(parsed["tags"]["MrPhoenixProtocol"]["items"][0])
    assert b"Doe" not in out and b"68\x00" not in out.split(b"MrPhoenixProtocol")[0]


def test_no_ascconv_means_removed():
    assert scrub.siemens_ascconv(b"<XProtocol> nothing technical </XProtocol>") is None
    assert scrub.siemens_csa_series(b"not a csa container") is None
