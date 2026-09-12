# Despliegue preliminar

```bash
git clone git@github.com:JuliDCardenas/IA-mcp-vps.git
cd IA-mcp-vps
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp config.example.yaml config.yaml
python -m dari_mcp_vps.server
```

Pendiente: auth remota, usuario Linux dedicado, systemd/Docker y conexión con Notion AI.
