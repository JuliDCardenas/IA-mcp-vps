# Caddy para mcp.julidcardenas.site

## DNS

Crear registro A en Hostinger:

```txt
mcp.julidcardenas.site -> IP publica del VPS
```

## Reverse proxy recomendado

Como Caddy corre en host y el contenedor publica solo en localhost:

```caddyfile
mcp.julidcardenas.site {
    @missingAuth not header Authorization "Bearer TU_TOKEN_LARGO"
    respond @missingAuth "Unauthorized" 401

    reverse_proxy 127.0.0.1:8787
}
```

Luego recargar Caddy:

```bash
sudo caddy validate --config /ruta/al/Caddyfile
sudo systemctl reload caddy
```

Si tu Caddyfile real está en `/home/ubuntu/infra-backup/caddy/Caddyfile`, primero confirma si ese es el activo o solo un backup. El Caddy de host normalmente usa:

```txt
/etc/caddy/Caddyfile
```

Verifica con:

```bash
systemctl status caddy
sudo caddy environ | grep CADDYFILE
```

## Prueba rápida

Sin token debe responder 401:

```bash
curl -i https://mcp.julidcardenas.site
```

Con token debe llegar al MCP:

```bash
curl -i -H 'Authorization: Bearer TU_TOKEN_LARGO' https://mcp.julidcardenas.site
```

Nota: el endpoint exacto puede devolver 404/405 si se consulta con curl simple, pero ya no debería ser 401 si el token pasó.
