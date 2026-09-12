# Ejecución con Docker

## Contexto del VPS actual

Los stacks viven bajo `/home/ubuntu` en el host:

```txt
/home/ubuntu/BasesDeDatos
/home/ubuntu/Chatwoot
/home/ubuntu/EvolutionApiPdP
/home/ubuntu/homepage
/home/ubuntu/n8n
/home/ubuntu/mosquitto
/home/ubuntu/prometheus
/home/ubuntu/traccar
/home/ubuntu/codeserver
/home/ubuntu/evolution
/home/ubuntu/ollama
```

El compose monta `/home/ubuntu` como `/mnt/stacks` dentro del contenedor.

## 1. Actualizar repo

```bash
cd ~/IA-mcp-vps
git pull
```

## 2. Crear config real

```bash
cp config.docker.example.yaml config.yaml
nano config.yaml
```

## 3. Ajustar UID/GID si hace falta

El compose corre el contenedor como UID/GID `1000:1000`, que suele ser el usuario `ubuntu`.

Verifica en el host:

```bash
id
```

Si no eres `1000:1000`, crea `.env`:

```bash
cat > .env <<'EOF'
MCP_UID=1000
MCP_GID=1000
EOF
```

cambiando los números por los de `id`.

## 4. Ajustar nombres reales de contenedores

Antes de levantar, revisa nombres reales:

```bash
docker ps --format '{{.Names}}'
```

Luego edita `allowed_containers` en `config.yaml` para que coincida exactamente.

## 5. Ajustar rutas de compose si hace falta

Busca archivos compose reales:

```bash
find /home/ubuntu -maxdepth 3 \( -name 'docker-compose.yml' -o -name 'compose.yml' -o -name 'docker-compose.yaml' -o -name 'compose.yaml' \) -print
```

Si un stack usa `compose.yml` en vez de `docker-compose.yml`, ajusta `allowed_compose_projects`.

## 6. Levantar

```bash
docker compose up -d --build
```

Si vienes del error `No module named 'mcp.server.fastmcp'`, reconstruye sin caché para instalar `mcp<2`:

```bash
docker compose down
docker compose build --no-cache
docker compose up -d
docker logs -f ia-mcp-vps
```

## 7. Ver estado

```bash
docker ps --filter name=ia-mcp-vps
docker logs --tail 100 ia-mcp-vps
```

## 8. Probar acceso a archivo del home

```bash
docker exec -it ia-mcp-vps ls -l /mnt/stacks/tracker-noche-2026-08-20.log
docker exec -it ia-mcp-vps python -c "from pathlib import Path; print(Path('/mnt/stacks/tracker-noche-2026-08-20.log').exists())"
```

## 9. Parar

```bash
docker compose down
```

## Nota sobre el restart loop

El servidor actual usa transporte MCP por `stdio`. Si Docker no mantiene stdin abierto, el proceso puede cerrar inmediatamente y el contenedor queda reiniciando. Por eso el compose incluye:

```yaml
stdin_open: true
tty: true
```

Para conexión remota real con Notion AI falta una fase posterior: transporte HTTP/SSE/Streamable HTTP + autenticación.

## Nota de seguridad

Se monta `/home/ubuntu`, no `/`. Además `home_stacks` queda read-only a nivel lógico en `config.yaml`; los scopes específicos son los escribibles. El contenedor sí tiene el volumen de `/home/ubuntu` montado con permisos de host, así que los guardrails del código son críticos.

También se monta:

```yaml
/var/run/docker.sock:/var/run/docker.sock
```

Esto da capacidad fuerte sobre Docker del host. Por eso no debe existir herramienta de shell libre y los reinicios deben pasar por allowlist.
