# Ejecución con Docker

## 1. Actualizar repo

```bash
git pull
```

## 2. Crear config real

```bash
cp config.docker.example.yaml config.yaml
nano config.yaml
```

Ajusta especialmente:

- `allowed_paths.gps_stack.root`
- nombres reales de contenedores en `allowed_containers`
- ruta real del `docker-compose.yml` del stack GPS

Si usas el `docker-compose.yml` incluido, recuerda que dentro del contenedor el stack GPS se ve como:

```txt
/mnt/gps-stack
```

porque el host monta:

```txt
/opt/gps-tracker:/mnt/gps-stack
```

Si tu stack GPS vive en otra ruta del host, cambia la ruta izquierda del volumen.

## 3. Levantar

```bash
docker compose up -d --build
```

## 4. Ver logs

```bash
docker logs -f ia-mcp-vps
```

## 5. Parar

```bash
docker compose down
```

## Nota de seguridad sobre Docker socket

El compose monta:

```yaml
/var/run/docker.sock:/var/run/docker.sock
```

Esto permite que el MCP consulte y reinicie contenedores del host. Es útil, pero sensible. Por eso las herramientas deben mantener allowlist estricta y no debe existir shell libre.

## Troubleshooting

### `config.yaml` no existe

Crear desde el ejemplo:

```bash
cp config.docker.example.yaml config.yaml
```

### La ruta `/opt/gps-tracker` no existe

Edita `docker-compose.yml` y cambia:

```yaml
- /opt/gps-tracker:/mnt/gps-stack
```

por la ruta real del stack GPS en el VPS.

### Permiso denegado con Docker

El contenedor usa `docker.io` y monta `docker.sock`. Si hay errores de permisos, primero prueba si el socket está montado:

```bash
docker exec -it ia-mcp-vps ls -l /var/run/docker.sock
```

En fase posterior ajustaremos usuario/grupo para Docker de forma más limpia si hace falta.
