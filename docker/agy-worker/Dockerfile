FROM debian:bookworm-slim

ARG AGY_INSTALL_URL=https://antigravity.google/cli/install.sh

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        coreutils \
        curl \
        git \
        jq \
        python3 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 agy \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash agy \
    && install -d -o agy -g agy \
        /home/agy/.local/bin \
        /workspace \
        /var/lib/coding-jobs

COPY --chmod=0555 docker/agy-worker/bootstrap/run-bootstrap-audit.sh /opt/agy-bootstrap/run-bootstrap-audit.sh
COPY --chmod=0555 docker/agy-worker/bootstrap/configure-readonly-permissions.sh /opt/agy-bootstrap/configure-readonly-permissions.sh
COPY --chmod=0444 docker/agy-worker/bootstrap/prompt.txt /opt/agy-bootstrap/prompt.txt
COPY --chmod=0444 docker/agy-worker/bootstrap/result-schema.json /opt/agy-bootstrap/result-schema.json
COPY --chmod=0555 docker/agy-worker/job/run-job.sh /opt/agy-job/run-job.sh
COPY --chmod=0555 docker/agy-worker/job/run-implementation-job.sh /opt/agy-job/run-implementation-job.sh
COPY --chmod=0555 docker/agy-worker/job/read-artifact.sh /opt/agy-job/read-artifact.sh
COPY --chmod=0444 docker/agy-worker/job/result-schema.json /opt/agy-job/result-schema.json
COPY --chmod=0444 docker/agy-worker/job/implementation-result-schema.json /opt/agy-job/implementation-result-schema.json

USER agy
ENV HOME=/home/agy
ENV PATH=/home/agy/.local/bin:${PATH}
ENV PYTHONPYCACHEPREFIX=/tmp/agy-pycache

RUN curl -fsSL "${AGY_INSTALL_URL}" -o /tmp/install-agy.sh \
    && bash /tmp/install-agy.sh --dir /home/agy/.local/bin \
    && rm -f /tmp/install-agy.sh \
    && agy --version

WORKDIR /workspace

CMD ["bash", "-lc", "trap : TERM INT; sleep infinity & wait"]
