# Modelo de amenazas

## Riesgos

1. Lectura de secretos.
2. Cambios destructivos.
3. Prompt injection desde archivos.
4. Comandos fuera de allowlist.

## Mitigaciones

- Allowlist de rutas/contenedores.
- Denylist de rutas sensibles.
- Límite de tamaño.
- Sin shell libre.
- Sin sudo.
