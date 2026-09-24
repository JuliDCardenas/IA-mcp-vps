FROM debian:bookworm-slim

ARG AGY_INSTALL_URL=https://antigravity.google/cli/install.sh

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        jq \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 agy \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash agy \
    && install -d -o agy -g agy /workspace /var/lib/coding-jobs

USER agy
ENV HOME=/home/agy
ENV PATH=/home/agy/.local/bin:${PATH}

RUN curl -fsSL "${AGY_INSTALL_URL}" -o /tmp/install-agy.sh \
    && bash /tmp/install-agy.sh --skip-path --skip-aliases \
    && rm -f /tmp/install-agy.sh \
    && agy --version

WORKDIR /workspace

CMD ["bash", "-lc", "trap : TERM INT; sleep infinity & wait"]
