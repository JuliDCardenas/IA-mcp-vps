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

RUN useradd -m -u 10001 mcpuser \
    && mkdir -p /app/audit /app/backups \
    && chown -R mcpuser:mcpuser /app

USER mcpuser

ENV IA_MCP_VPS_CONFIG=/config/config.yaml

CMD ["python", "-m", "dari_mcp_vps.server"]
