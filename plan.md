# Implementation Plan

1. **Create `src/dari_mcp_vps/tools/discovery.py`**:
   - Implement the 4 tools described: `discover_containers`, `discover_compose_projects`, `discover_http_targets`, and `suggest_allowlist_updates`.
   - Use `_json("GET", "/containers/json?all=1")` for `discover_containers` and filter out sensitive info. Only return explicitly selected safe fields (name/id, image, status, pertinent Compose labels, published ports).
   - Use `config.get('allowed_paths', {})` for `discover_compose_projects` to scan *only within* authorized paths looking for `docker-compose.yml` or `docker-compose.yaml` files. Do not traverse outside allowed paths or process symlinks leading outside. Parse YAML safely without environment interpolation.
   - For `discover_http_targets`, derive probable HTTP services from the discovered containers (checking ports, specifically 80, 443, 8080, etc.). Explain certainty. DO NOT make any network calls.
   - For `suggest_allowlist_updates`, take discovered candidates and generate valid YAML snippets for updating the config, deduplicating against currently allowed items.
   - Tools must be read-only: `annotations={"readOnlyHint": True}`.

2. **Register the new tools in `src/dari_mcp_vps/server.py`**:
   - Import and call `register_discovery_tools(mcp, CONFIG)` in `server.py`.

3. **Write Tests (`tests/test_discovery.py`)**:
   - Test offline and strictly bounded using mocked Docker responses and temporary filesystems.
   - Assert `readOnlyHint` is True for all four tools.
   - Verify constraints: bounds, escaping YAML, truncated info, non-HTTP port handling, symlink bounds.

4. **Run existing contract regression tests and newly added tests.**
   - Run `PYTHONPATH=src python3 -m pytest tests/test_discovery.py tests/test_tool_contracts.py` (or whatever the test files are).

5. **Complete pre-commit steps to ensure proper testing, verification, review, and reflection are done.**
