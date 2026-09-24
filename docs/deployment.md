# Despliegue

## Servidor MCP

```bash
cd ~/IA-mcp-vps
git pull --ff-only
docker compose build ia-mcp-vps
docker compose up -d --force-recreate ia-mcp-vps
docker logs --tail 100 ia-mcp-vps
```

El puerto debe permanecer publicado únicamente en `127.0.0.1:8787`; Caddy proporciona HTTPS y autenticación exterior.

## Worker Agy

```bash
cd ~/IA-mcp-vps
git pull --ff-only
docker compose -f docker-compose.agy-worker.yml build agy-worker
docker compose -f docker-compose.agy-worker.yml up -d --force-recreate agy-worker
docker compose -f docker-compose.agy-worker.yml ps
```

No ejecutar `down -v`: eliminaría perfil OAuth, workspace y resultados persistidos.

## Verificación del aislamiento

```bash
docker inspect agy-worker --format '{{.Config.User}}'
docker inspect agy-worker --format '{{.HostConfig.ReadonlyRootfs}}'
docker inspect agy-worker --format '{{json .HostConfig.CapDrop}}'
docker inspect agy-worker --format '{{json .HostConfig.SecurityOpt}}'
docker inspect agy-worker --format '{{range .Mounts}}{{println .Type .Destination .RW}}{{end}}'
```

Esperado:

- usuario `10001:10001`;
- root filesystem read-only;
- `CapDrop` contiene `ALL`;
- `no-new-privileges`;
- solo volúmenes nombrados;
- `/workspace` read-only;
- sin puertos publicados;
- sin Docker socket.

## Actualización de herramientas MCP

Cuando cambie el esquema o la lista de herramientas:

1. reconstruir y recrear `ia-mcp-vps`;
2. consultar `listTools`;
3. si Notion conserva un catálogo antiguo, refrescar o recrear la conexión MCP;
4. no asumir que una herramienta está disponible hasta verla en el catálogo.

## Validación previa al merge

1. Imágenes construyen sin errores.
2. Worker y MCP aparecen saludables.
3. `coding_job_create` retorna inmediatamente.
4. El trabajo llega a `NOTION_REVIEW` o `FAILED` explícito.
5. El SHA base coincide con el repositorio esperado.
6. Manifiesto, hashes y artefactos coinciden.
7. No hay secretos ni rutas fuera del clon.
8. Se crea una rama y PR de prueba mediante GitHub MCP.
9. Se verifica el `expectedHeadSha` antes del merge.

## Despliegue de una aplicación modificada por Agy

El worker no despliega aplicaciones. Después de aprobar y fusionar el PR:

1. obtener el merge SHA desde GitHub;
2. actualizar el checkout de producción con avance rápido a la revisión aprobada;
3. construir/recrear únicamente los servicios afectados;
4. ejecutar probes y revisar logs;
5. revertir mediante Git si la verificación operacional falla.

Los comandos concretos deben estar allowlisted por proyecto. Nunca aceptar comandos libres generados por Agy.
