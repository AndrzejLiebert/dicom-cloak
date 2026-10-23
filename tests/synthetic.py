"""Synthetic breast-MRI DICOM studies with planted identifiers (no real data needed).

make_siemens(dir): native + 2 post-contrast series (one volume per series) + Dixon F/W TWIST pair +
                   a TTP map (derived, must not become a phase) + DWI with CSA/0019 private b-values
make_philips(dir): one DCE series with 3 dynamics (TemporalPositionIdentifier) + private b-value/scale
make_ge(dir):      DWI with GEMS private b-value + a GEMS_IDEN_01 private date (must be removed)
Every study carries nested leaks (RequestAttributesSequence, OtherPatientIDsSequence,
OriginalAttributesSequence/ModifyingSystem, vendor-root UIDs with timestamps).
"""
import os

import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

MR = "1.2.840.10008.5.1.4.1.1.4"
NATIONAL_ID = "44051401458"     # syntactically valid PESEL (checksum ok), fictitious
HOSPITAL_ID = "7654321"
ACCESSION = "998877665"
PHYSICIAN = "Smith-Jones^Anna"
SERIAL = "123456"
STATION = "MRC12345"
DATE = "20250314"
IOP = [1.0, 0.0, 0.0, 0.0, 0.9961947, -0.0871557]  # 5 degree tilt


def _uid(root, n):
    return f"{root}.{DATE}101500{n:06d}"


def base(patient_id, study_uid, series_uid, for_uid, desc, sn, inst, acq_dt, manufacturer):
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = MR
    sop = generate_uid()
    ds.file_meta.MediaStorageSOPInstanceUID = sop
    ds.file_meta.SourceApplicationEntityTitle = STATION
    ds.SpecificCharacterSet = "ISO_IR 192"
    ds.SOPClassUID, ds.SOPInstanceUID = MR, sop
    ds.Modality, ds.Manufacturer = "MR", manufacturer
    ds.PatientName, ds.PatientID, ds.PatientSex, ds.PatientAge = "Doe^Jane", patient_id, "F", "047Y"
    ds.PatientBirthDate, ds.PatientWeight = "19780514", "68"
    ds.OtherPatientIDs = NATIONAL_ID
    o = Dataset()
    o.PatientID, o.IssuerOfPatientID = HOSPITAL_ID, "HOSP"
    ds.OtherPatientIDsSequence = Sequence([o])
    ds.InstitutionName, ds.InstitutionAddress = "General Hospital", "Main St 1, City"
    ds.StationName, ds.DeviceSerialNumber = STATION, SERIAL
    ds.AccessionNumber, ds.StudyID = ACCESSION, ACCESSION
    ds.ReferringPhysicianName = PHYSICIAN
    ra = Dataset()
    ra.AccessionNumber, ra.RequestingPhysician = ACCESSION, PHYSICIAN
    ra.ReasonForTheRequestedProcedure = "C50 - breast tumour"
    ra.StudyInstanceUID = _uid("1.3.12.2.1107.5.2.99", 1)
    ds.RequestAttributesSequence = Sequence([ra])
    oa = Dataset()
    oa.ModifyingSystem = f"{STATION} / {SERIAL}"
    oa.AttributeModificationDateTime = DATE + "120000"
    ds.OriginalAttributesSequence = Sequence([oa])
    # not in any removal list: only the allowlist (or the byte check) catches these
    ds.DerivationDescription = f"reformatted for {PHYSICIAN.split('^')[0]}"
    ds.AdmittingDiagnosesDescription = "C50 - breast tumour"
    ds.StudyDate = ds.SeriesDate = ds.AcquisitionDate = DATE
    ds.StudyTime = "101500"
    ds.AcquisitionDateTime = acq_dt
    ds.AcquisitionTime = acq_dt[8:]
    ds.StudyInstanceUID, ds.SeriesInstanceUID, ds.FrameOfReferenceUID = study_uid, series_uid, for_uid
    ds.SeriesDescription, ds.ProtocolName, ds.SeriesNumber, ds.InstanceNumber = desc, desc, sn, inst
    ds.ImageType = ["ORIGINAL", "PRIMARY", "M", "NORM"]
    # deliberately non-default geometry (oblique, anisotropic) so that losing any geometry attribute
    # changes the dcm2niix affine
    ds.ImageOrientationPatient = IOP
    ds.ImagePositionPatient = [-100.0, -100.0 + 1.5 * inst * IOP[5] * -1, 1.5 * inst * IOP[4]]
    ds.PixelSpacing, ds.SliceThickness = [0.7, 0.8], 1.5
    ds.Rows = ds.Columns = 8
    ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
    rng = np.random.default_rng(inst + sn * 100)
    ds.PixelData = rng.integers(0, 4000, (8, 8), dtype=np.uint16).tobytes()
    return ds


def _save(ds, d, name):
    os.makedirs(d, exist_ok=True)
    ds.save_as(os.path.join(d, name), enforce_file_format=True)


def _t(sec):  # DT at 10:15:00 + sec on DATE
    h, m, s = 10, 15 + int(sec) // 60, int(sec) % 60
    return f"{DATE}{h:02d}{m:02d}{s:02d}.{int(round((sec % 1) * 1e6)):06d}"


def make_siemens(d, patient_id="ORIG-S-1"):
    study, forr = _uid("1.3.12.2.1107.5.2.99", 2), _uid("1.3.12.2.1107.5.2.99", 3)
    series = [("t1_tra_native", 3, 0.0), ("t1_tra_dyn_post", 20, 200.0), ("t1_tra_dyn_post", 21, 300.0),
              ("t1_twist_dixon_tra_dyn_TTC=20.0s_F", 10, 100.0), ("t1_twist_dixon_tra_dyn_TTC=20.0s_W", 11, 100.0),
              ("t1_twist_dixon_tra_dyn_TTC=20.0s_W_TTP", 30, 400.0)]
    for desc, sn, sec in series:
        suid = _uid("1.3.12.2.1107.5.2.99", 1000 + sn)
        for i in (1, 2, 3):
            ds = base(patient_id, study, suid, forr, desc, sn, i, _t(sec), "SIEMENS")
            if desc.endswith("TTP"):
                ds.ImageType = ["DERIVED", "PRIMARY", "TTP"]
            _save(ds, os.path.join(d, f"{desc}_{sn}"), f"Doe_Jane_{sn}_{i}.dcm")
    suid = _uid("1.3.12.2.1107.5.2.99", 1040)
    for i, b in enumerate((50, 800, 1000), 1):
        ds = base(patient_id, study, suid, forr, "diff_tra_TRACEW", 40, i, _t(600.0 + i), "SIEMENS")
        ds.SequenceName = f"*re_b{b}t"
        blk = ds.private_block(0x0019, "SIEMENS MR HEADER", create=True)
        blk.add_new(0x0C, "IS", str(b))
        blk.add_new(0x27, "FD", [1.0, 0.0, 0.0, 1.0, 0.0, 1.0])
        csa = ds.private_block(0x0029, "SIEMENS CSA HEADER", create=True)
        csa.add_new(0x10, "OB", b"SV10\x04\x03\x02\x01" + b"\x00" * 16)
        xa = ds.private_block(0x0021, "SIEMENS MR SDS 01", create=True)   # XA-style protocol blob with leaks
        prot = ("<XProtocol> <ParamString.\"PatientName\"> { \"Doe^Jane\" }\n"
                "### ASCCONV BEGIN object=MrProtDataImpl ###\n"
                f"tReferenceImage0 = \"\"1.3.12.2.1107.5.2.43.{SERIAL}.{DATE}101500\"\"\n"
                "sPat.lAccelFactPE = 2\nsKSpace.lBaseResolution = 128\n### ASCCONV END ###\n")
        xa.add_new(0x19, "OB", prot.encode("latin-1") + (b"" if len(prot) % 2 == 0 else b" "))
        junk = ds.private_block(0x0051, "SIEMENS MR HEADER", create=True)
        junk.add_new(0x0A, "LO", "TA 00.22")         # not whitelisted -> removed
        _save(ds, os.path.join(d, "diff_40"), f"dwi_{i}.dcm")
    return study


def make_philips(d, patient_id="ORIG-P-1"):
    study, forr = _uid("1.3.46.670589.11.99", 2), _uid("1.3.46.670589.11.99", 3)
    suid = _uid("1.3.46.670589.11.99", 401)
    k = 0
    for dyn, sec in ((1, 0.0), (2, 70.0), (3, 140.0)):
        for sl in (1, 2):
            k += 1
            ds = base(patient_id, study, suid, forr, "DCE_THRIVE dynamic", 401, k, _t(0.0), "Philips")
            # Philips classic DCE: AcquisitionDateTime/Time repeat the SERIES start for every dynamic;
            # the per-dynamic time is only in FrameAcquisitionDateTime / ContentTime
            ds.FrameAcquisitionDateTime = _t(sec + 0.5 * sl)
            ds.ContentDate, ds.ContentTime = DATE, _t(sec + 0.5 * sl)[8:]
            ds.TemporalPositionIdentifier, ds.NumberOfTemporalPositions = dyn, 3
            blk = ds.private_block(0x2005, "Philips MR Imaging DD 001", create=True)
            blk.add_new(0x0E, "FL", 1.25)
            blk.add_new(0x0D, "FL", 0.0)
            ph = ds.private_block(0x2001, "Philips Imaging DD 001", create=True)
            ph.add_new(0x08, "IS", str(dyn))
            _save(ds, os.path.join(d, "dce"), f"IM_{k}.dcm")
    return study


def make_ge(d, patient_id="ORIG-G-1"):
    study, forr = _uid("1.2.840.113619.2.99", 2), _uid("1.2.840.113619.2.99", 3)
    suid = _uid("1.2.840.113619.2.99", 501)
    for i, b in enumerate((0, 800), 1):
        ds = base(patient_id, study, suid, forr, "DWI ax", 5, i, _t(900 + i), "GE MEDICAL SYSTEMS")
        p = ds.private_block(0x0043, "GEMS_PARM_01", create=True)
        p.add_new(0x39, "IS", [str(1000000000 + b), "0", "0", "0"])
        iden = ds.private_block(0x0009, "GEMS_IDEN_01", create=True)
        iden.add_new(0x27, "SL", 1741946100)                # image actual date (epoch) -> removed
        _save(ds, os.path.join(d, "dwi"), f"{i}.dcm")
    # a non-image object and an OS metadata file, both must be excluded
    sr = base(patient_id, study, _uid("1.2.840.113619.2.99", 599), forr, "report", 999, 1, _t(0), "GE")
    sr.SOPClassUID = sr.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.88.22"
    _save(sr, os.path.join(d, "sr"), "sr.dcm")
    open(os.path.join(d, ".DS_Store"), "wb").write(b"\x00\x00\x00\x01Bud1")
    return study


EMR = "1.2.840.10008.5.1.4.1.1.4.1"


def make_enhanced(d, patient_id="ORIG-E-1", nframes=4):
    """Enhanced MR (multi-frame) with functional groups, as written by newer scanner software.
    Per-frame FrameComments carries a national ID (not allowlisted -> must go)."""
    study, forr = _uid("1.3.12.2.1107.5.2.99", 7), _uid("1.3.12.2.1107.5.2.99", 8)
    ds = base(patient_id, study, _uid("1.3.12.2.1107.5.2.99", 701), forr, "t1_tra_enhanced_native", 70, 1,
              _t(0.0), "SIEMENS")
    ds.SOPClassUID = ds.file_meta.MediaStorageSOPClassUID = EMR
    for k in ("ImagePositionPatient", "ImageOrientationPatient", "PixelSpacing", "SliceThickness"):
        delattr(ds, k)
    ds.NumberOfFrames = nframes
    ds.ImageType = ["ORIGINAL", "PRIMARY", "M", "NONE"]
    sh = Dataset()
    pm = Dataset(); pm.PixelSpacing = [0.7, 0.8]; pm.SliceThickness = 2.0; pm.SpacingBetweenSlices = 2.0
    po = Dataset(); po.ImageOrientationPatient = IOP
    ft = Dataset(); ft.FrameType = ["ORIGINAL", "PRIMARY", "M", "NONE"]
    tr = Dataset(); tr.RepetitionTime = 4.5; tr.FlipAngle = 10
    ec = Dataset(); ec.EffectiveEchoTime = 2.1
    sh.PixelMeasuresSequence = Sequence([pm]); sh.PlaneOrientationSequence = Sequence([po])
    sh.MRImageFrameTypeSequence = Sequence([ft]); sh.MRTimingAndRelatedParametersSequence = Sequence([tr])
    sh.MREchoSequence = Sequence([ec])
    ds.SharedFunctionalGroupsSequence = Sequence([sh])
    frames = []
    for i in range(nframes):
        f = Dataset()
        pp = Dataset(); pp.ImagePositionPatient = [-100.0, -100.0 + 2.0 * i * 0.0871557, 2.0 * i * 0.9961947]
        fc = Dataset()
        fc.FrameAcquisitionDateTime = fc.FrameReferenceDateTime = _t(0.0)
        fc.StackID, fc.InStackPositionNumber, fc.DimensionIndexValues = "1", i + 1, [1, i + 1]
        fc.FrameComments = f"ID {NATIONAL_ID}"
        pv = Dataset(); pv.RescaleIntercept, pv.RescaleSlope, pv.RescaleType = -5, 1.5, "US"
        f.PlanePositionSequence = Sequence([pp]); f.FrameContentSequence = Sequence([fc])
        f.PixelValueTransformationSequence = Sequence([pv])
        frames.append(f)
    ds.PerFrameFunctionalGroupsSequence = Sequence(frames)
    di = []
    for ptr, grp in ((0x00209056, 0x00209111), (0x00209057, 0x00209111)):
        x = Dataset(); x.DimensionIndexPointer, x.FunctionalGroupPointer = ptr, grp
        x.DimensionOrganizationUID = _uid("1.3.12.2.1107.5.2.99", 9)
        di.append(x)
    ds.DimensionIndexSequence = Sequence(di)
    rng = np.random.default_rng(70)
    ds.PixelData = rng.integers(0, 4000, (nframes, 8, 8), dtype=np.uint16).tobytes()
    _save(ds, os.path.join(d, "enhanced"), "enh.dcm")
    return study


VISITS = [("T0", "20250110", "9"), ("T1", "20250301", "5"), ("T2", "20250620", "1")]  # UID order != date order


def make_multivisit(d, patient_id="ORIG-M-1", visits=VISITS):
    """One patient, several visits in the SAME year, study UIDs sorting opposite to chronology."""
    for tp, date, uid_digit in visits:
        study = f"1.3.12.2.1107.5.2.99.{uid_digit}{date}"
        forr = study + ".3"
        for desc, sn, sec in (("t1_tra_native", 3, 0.0), ("t1_tra_dyn_post", 20, 90.0)):
            suid = f"{study}.{sn}"
            for i in (1, 2):
                acq = f"{date}101500.000000"
                ds = base(patient_id, study, suid, forr, desc, sn, i, acq, "SIEMENS")
                ds.StudyDate = ds.SeriesDate = ds.AcquisitionDate = date
                ds.AcquisitionDateTime = f"{date}1015{int(sec) // 60 % 60:02d}"[:12] + f"{int(sec) % 60:02d}.000000"
                ds.AcquisitionTime = ds.AcquisitionDateTime[8:]
                ds.ClinicalTrialTimePointID = tp
                ds.ClinicalTrialTimePointDescription = f"visit {tp} for Doe"   # free text: must be dropped
                _save(ds, os.path.join(d, tp, f"{desc}_{sn}"), f"{i}.dcm")
