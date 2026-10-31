# Build: the final stage copies collector.py out of the test stage, so the image can only be built when the
# unit tests (including the Helm chart assertions when helm is present) pass.
FROM python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 AS test
WORKDIR /app
COPY collector.py /app/collector.py
COPY tests /app/tests
RUN python -m unittest discover -s tests -v

FROM python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962
ARG VERSION=dev
ARG REVISION=unknown
# SPDX identifier of the repository license.
ARG LICENSE=Apache-2.0
LABEL org.opencontainers.image.title="toduen-collector" \
      org.opencontainers.image.description="Pushes selected Prometheus metrics from your cluster to Todün. Outbound only, listens only on 9090 for its own health/metrics." \
      org.opencontainers.image.source="https://github.com/Coflnet/toduen-collector" \
      org.opencontainers.image.url="https://dash.coflnet.com" \
      org.opencontainers.image.vendor="Coflnet" \
      org.opencontainers.image.licenses="${LICENSE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
# The collector is stdlib only: drop pip, whose vendored libraries (urllib3, msgpack, ...) are
# never used at runtime but carry CVEs that fail the release scan.
RUN python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.*/ensurepip /root/.cache
WORKDIR /app
COPY --from=test /app/collector.py /app/collector.py
# No bytecode writes: the chart runs with a read-only root filesystem and as UID/GID 65532.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
EXPOSE 9090
ENTRYPOINT ["python", "/app/collector.py"]
