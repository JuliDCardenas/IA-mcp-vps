# Repositorios privados en Agy worker

## Modelo adoptado

Cada repositorio privado utiliza una deploy key SSH exclusiva y de solo lectura. La clave se monta directamente en `agy-worker` como secreto Docker, nunca se copia al repositorio ni se incluye en prompts, resultados o logs.

La promoción de cambios continúa realizándose exclusivamente mediante GitHub MCP. Agy no recibe permisos de escritura, `push`, PR, merge o despliegue.

## Repositorio inicial

- Alias MCP: `repositorio_bd_emision`
- GitHub: `JuliDCardenas/repositorio-bd-emision`
- Rama permitida: `main`
- Clave dentro del contenedor: `/run/secrets/repo_key_repositorio_bd_emision`

## Archivo local

La clave privada debe permanecer fuera del checkout:

```text
/home/ubuntu/.config/ia-mcp-vps/keys/repositorio_bd_emision
```

El archivo debe pertenecer al UID/GID usado por el worker y no ser legible por otros:

```bash
sudo chown 10001:10001 /home/ubuntu/.config/ia-mcp-vps/keys/repositorio_bd_emision
sudo chmod 0400 /home/ubuntu/.config/ia-mcp-vps/keys/repositorio_bd_emision
```

Configurar en `~/IA-mcp-vps/.env`:

```dotenv
REPO_KEY_REPOSITORIO_BD_EMISION_FILE=/home/ubuntu/.config/ia-mcp-vps/keys/repositorio_bd_emision
```

`.env` ya está excluido de Git.

## GitHub

Registrar la clave pública en `Settings → Deploy keys` y mantener desmarcado `Allow write access`.

## SSH

El worker usa exclusivamente:

- `BatchMode=yes`;
- `IdentitiesOnly=yes`;
- `StrictHostKeyChecking=yes`;
- `UserKnownHostsFile=/opt/agy-job/github-known-hosts.txt`;
- clave Ed25519 oficial de GitHub fijada en la imagen.

No se permite `ssh-keyscan` durante la ejecución ni `StrictHostKeyChecking=no`.

## Verificación

Después del despliegue:

```bash
docker compose -f docker-compose.agy-worker.yml config
docker compose -f docker-compose.agy-worker.yml up -d --force-recreate agy-worker
docker inspect agy-worker --format '{{json .Config.User}}'
docker inspect agy-worker --format '{{range .Mounts}}{{println .Destination .RW}}{{end}}'
```

La clave debe aparecer en `/run/secrets/repo_key_repositorio_bd_emision` como read-only. Nunca imprimir su contenido.

Ejecutar primero un trabajo `audit` o `implement` mínimo y verificar que el `base_commit` coincide con GitHub antes de promover cambios.

## Rotación y revocación

1. Crear una clave nueva fuera del repositorio.
2. Agregar la nueva pública como deploy key read-only.
3. Actualizar la ruta en `.env` y recrear el worker.
4. Validar un clon privado.
5. Eliminar la deploy key anterior en GitHub.
6. Borrar de forma segura la clave privada anterior.

## Riesgo aceptado

Agy y los procesos confiables del worker comparten el mismo UID, por lo que la clave está técnicamente disponible dentro del contenedor. El alcance se limita mediante una clave exclusiva, read-only y por repositorio. El código del repositorio también se envía al proveedor de Antigravity para análisis; debe utilizar únicamente datos autorizados, ficticios o anonimizados.
