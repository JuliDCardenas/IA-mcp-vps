# Modelo de amenazas

## Activos protegidos

- VPS y socket Docker;
- credenciales de GitHub, OAuth y Bearer tokens;
- repositorios y rama `main`;
- datos y configuración de producción;
- contenido de los trabajos y sus resultados.

## Riesgos principales

1. Lectura o exfiltración de secretos.
2. Cambios destructivos o escritura directa en `main`.
3. Prompt injection desde archivos del repositorio.
4. Ejecución de comandos arbitrarios.
5. Escape por rutas relativas, symlinks o archivos especiales.
6. Cambios sobre una revisión base obsoleta.
7. Resultados o logs sin límites.
8. Despliegue de cambios no aprobados.
9. Agotamiento de CPU, memoria, almacenamiento o tokens.

## Mitigaciones

### Contenedor Agy

- usuario no root `10001:10001`;
- filesystem raíz read-only;
- capabilities Linux eliminadas;
- `no-new-privileges`;
- sin Docker socket;
- sin bind mounts del host;
- `/workspace` montado read-only;
- límites de procesos, memoria y CPU;
- credenciales de GitHub ausentes.

### Contexto y agente

- contexto construido por scripts fijos;
- exclusión de `.env`, secretos, credenciales y configuración local;
- archivos tratados como datos no confiables;
- salida JSON validada por esquema;
- sin `--dangerously-skip-permissions`;
- Agy no recibe shell ni comandos de promoción.

### Aplicación de cambios

- clon independiente por trabajo;
- clon de la rama `main` actual para implementaciones;
- captura obligatoria del SHA base;
- rutas relativas allowlisted;
- validación con `realpath`;
- rechazo de symlinks;
- máximo 20 archivos, 64 KiB por archivo y 512 KiB total;
- escaneo de patrones de secretos;
- validación de diff y sintaxis;
- hashes SHA-256 por artefacto.

### Promoción

- Agy no hace commit ni push;
- GitHub es la fuente de verdad;
- Notion IA usa únicamente el GitHub MCP;
- rama y PR antes de `main`;
- revisión de criterios y checks;
- aprobación explícita del usuario;
- merge con `expectedHeadSha`;
- despliegue posterior al merge aprobado.

## Riesgos residuales

- El contexto enviado al modelo puede contener información interna no detectada por patrones simples.
- El worker conserva salida a red para autenticación y operación de Agy.
- Los patrones de secretos no sustituyen GitHub Secret Scanning ni revisión humana.
- Las pruebas actuales son sintácticas y deben ampliarse por repositorio.
- Los repositorios privados requieren un broker de lectura separado; nunca se debe montar una credencial privada dentro de Agy.

## Reglas de respuesta

- Fallar cerrado ante ruta, tamaño, secreto, symlink o esquema inválido.
- No promover si el SHA base ya no es válido.
- No declarar éxito de despliegue sin evidencia operacional.
