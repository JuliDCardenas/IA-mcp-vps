# Despliegue preliminar

## Opción recomendada: Docker

```bash
git clone git@github.com:JuliDCardenas/IA-mcp-vps.git
cd IA-mcp-vps
cp config.docker.example.yaml config.yaml
nano config.yaml
docker compose up -d --build
docker logs -f ia-mcp-vps
```

Ver más en [`docker.md`](docker.md).

## Opción sin Docker

```bash
git clone git@github.com:JuliDCardenas/IA-mcp-vps.git
cd IA-mcp-vps
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp config.example.yaml config.yaml
python3 -m dari_mcp_vps.server
```

## Pendiente antes de exponer remotamente

1. Completar autenticación para transporte remoto.
2. Probar herramientas read-only localmente.
3. Crear usuario Linux dedicado o endurecer el contenedor.
4. Definir rutas reales del stack GPS.
5. Validar permisos Docker sin shell libre.
6. Configurar transporte remoto MCP compatible con Notion AI.
