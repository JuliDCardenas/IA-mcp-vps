# Agy worker

## Propósito

Ejecutar Antigravity CLI dentro de un contenedor aislado para trabajos asíncronos de auditoría e implementación. Agy analiza contexto acotado y devuelve resultados estructurados; no controla GitHub, Docker ni producción.

## Límite de seguridad

El worker tiene:

- usuario no root `10001:10001`;
- root filesystem read-only;
- todas las capabilities eliminadas;
- `no-new-privileges`;
- sin Docker socket ni binds del host;
- sin puertos publicados;
- `/workspace` como volumen read-only;
- `/home/agy` para perfil OAuth;
- `/var/lib/coding-jobs` para estado, clones temporales y resultados;
- límites de procesos, memoria y CPU.

La red saliente continúa habilitada para Agy y para obtener snapshots públicos. Debe restringirse cuando se conozcan todos los endpoints requeridos.

## Construcción y arranque

```bash
git pull --ff-only
docker compose -f docker-compose.agy-worker.yml build agy-worker
docker compose -f docker-compose.agy-worker.yml up -d --force-recreate agy-worker
docker compose -f docker-compose.agy-worker.yml ps
```

La construcción debe finalizar con `agy --version` exitoso.

## Autenticación inicial

```bash
docker compose -f docker-compose.agy-worker.yml exec agy-worker agy
```

Abrir localmente la URL de autorización y completar OAuth. No copiar códigos, tokens o perfil cacheado a Git, Notion, logs o chat. El perfil persiste en el volumen `agy_home`.

## Smoke test headless

```bash
docker compose -f docker-compose.agy-worker.yml exec agy-worker \
  agy -p 'Return only a short confirmation that headless mode works. Do not use tools.' \
  --output-format json \
  --json-schema '{"type":"object","properties":{"ok":{"type":"boolean"},"message":{"type":"string"}},"required":["ok","message"],"additionalProperties":false}' \
  --sandbox \
  --print-timeout 2m
```

Nunca usar `--dangerously-skip-permissions`.

## Modelo de ejecución

### Auditoría

El script fijo empaqueta un contexto textual limitado desde `/workspace/IA-mcp-vps`. Agy no necesita invocar `read_file` en headless.

### Implementación

El script fijo clona el `main` actual en el directorio del trabajo, entrega contexto a Agy, valida su salida estructurada y aplica los reemplazos únicamente dentro del clon. El resultado conserva el SHA base, manifiesto, hashes y artefactos para revisión.

## Verificación

```bash
docker inspect agy-worker --format '{{range .Mounts}}{{println .Type .Destination .RW}}{{end}}'
docker inspect agy-worker --format '{{json .HostConfig.CapDrop}}'
docker inspect agy-worker --format '{{json .HostConfig.SecurityOpt}}'
docker inspect agy-worker --format '{{.HostConfig.ReadonlyRootfs}}'
```

Todos los mounts deben ser volúmenes. `/workspace` debe ser read-only. No debe existir `/var/run/docker.sock`.

## Detención

```bash
docker compose -f docker-compose.agy-worker.yml down
```

Esto preserva los volúmenes. No añadir `-v` salvo destrucción intencional del perfil, workspace y resultados.

## Fuera de alcance del worker

- credenciales GitHub o SSH;
- commits, push, PR o merge;
- escritura directa en `main`;
- acceso al Docker socket;
- comandos de despliegue;
- shell MCP genérico;
- aprobación automática de permisos.
