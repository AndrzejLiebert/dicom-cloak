# DICOM Cloak (MAGIC Lab): breast MRI DICOM de-identification image.
#   target "python" (default, last stage): dicom-cloak, dicom-cloak-check, dicom-cloak-compare, dicom-cloak-report
#   target "ctp":                          the same plus RSNA CTP (Java 25) for sites that require CTP
#
# Build from the project root (the cloak.sh / cloak.bat launchers do this automatically):
#   docker build -f docker/Dockerfile --target python -t dicom-cloak:1.0 .
#   docker build -f docker/Dockerfile --target ctp    -t dicom-cloak:1.0-ctp .
# Multi-architecture (Intel/AMD and Apple Silicon / ARM servers):
#   docker buildx build --platform linux/amd64,linux/arm64 -f docker/Dockerfile -t <registry>/dicom-cloak:1.0 --push .
# Self-test: docker run --rm --network none --entrypoint python dicom-cloak:1.0 -m pytest -q -p no:cacheprovider tests

FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DICOM_CLOAK_CONFIG=/opt/dicom_cloak/config/config.yaml
WORKDIR /opt/dicom_cloak
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src
RUN pip install --no-cache-dir --root-user-action=ignore ".[test]" && rm -rf build src/*.egg-info
COPY config ./config
COPY tests ./tests
# runs without network and without root; data is mounted at /in (read-only), /out and /secrets
RUN useradd --uid 10001 --create-home cloak
USER cloak

# Java 25 runtime for CTP (CTPTools25 builds target Java 25); Temurin images exist for amd64 and arm64
FROM eclipse-temurin:25-jre AS jre

FROM base AS ctp
USER root
COPY --from=jre /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk PATH=/opt/java/openjdk/bin:$PATH \
    DAT_HOME=/opt/DicomAnonymizerTool JAVA=/opt/java/openjdk/bin/java
# RSNA DicomAnonymizerTool + CTP, pinned commit + SHA-256 (docker/install_ctp.py); needs network at build time
COPY docker/install_ctp.py ctp/config.xml /tmp/ctp_install/
RUN python /tmp/ctp_install/install_ctp.py /opt --config /tmp/ctp_install/config.xml && rm -rf /tmp/ctp_install \
    && mkdir -p /opt/CTP/scripts /opt/CTP/output && chown -R cloak /opt/CTP
COPY ctp /opt/dicom_cloak/ctp
USER cloak
# batch:  dicom-cloak-ctp run --src /in --dst /out --work /tmp/work ...   (see docs/CTP.md)
# server: generate /opt/CTP/scripts with `dicom-cloak-ctp script`, then run from /opt/CTP:
#         java -jar libraries/CTP.jar
ENTRYPOINT ["dicom-cloak-ctp"]
CMD ["--help"]

FROM base AS python
ENTRYPOINT ["dicom-cloak"]
CMD ["--help"]
