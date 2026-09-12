FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    ripgrep \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir -e .

RUN mkdir -p /app/audit /app/backups && chmod -R 775 /app

ENV IA_MCP_VPS_CONFIG=/config/config.yaml
ENV IA_MCP_VPS_TRANSPORT=http
ENV IA_MCP_VPS_HOST=0.0.0.0
ENV IA_MCP_VPS_PORT=8787

CMD ["python", "-m", "dari_mcp_vps.server"]
