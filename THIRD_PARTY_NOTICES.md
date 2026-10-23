# Third-party components

The DICOM Cloak source code is licensed under the MIT License (`LICENSE`). This repository does not
contain third-party code. The components below are downloaded when the package is installed or the
Docker image is built, and remain under their own licenses. If you redistribute a built Docker image,
these licenses apply to the components inside it.

| Component | Used for | License |
|---|---|---|
| pydicom | reading and writing DICOM | MIT |
| NumPy | pixel data handling | BSD 3-Clause |
| PyYAML | policy files | MIT |
| nibabel | conversion comparison, tests | MIT |
| dcm2niix (PyPI wheel) | conversion comparison, tests | BSD-style, see https://github.com/rordenlab/dcm2niix/blob/master/license.txt |
| pytest | tests | MIT |
| Python, `python:3.11-slim` base image | runtime | PSF License; Debian packages under their own licenses |
| RSNA CTP and DicomAnonymizerTool (CTPTools25, `ctp` image only) | CTP engine | RSNA Public License, http://mirc.rsna.org/rsnapubliclicense.pdf |
| Eclipse Temurin 25 JRE (`ctp` image only) | Java runtime for CTP | GPL v2 with Classpath Exception |

RSNA CTP is Copyright (c) the Radiological Society of North America. DICOM Cloak generates
configuration for CTP's DicomAnonymizer and calls it as a separate program; it does not modify or
include CTP source code.
