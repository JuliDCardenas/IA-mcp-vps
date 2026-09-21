from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Any

from dari_mcp_vps.security import SecurityError


def _target(config: dict[str, Any], name: str) -> dict[str, Any]:
    targets = config.get("allowed_http_targets", {})
    if name not in targets:
        raise SecurityError(f"HTTP target not allowed: {name}")
    value = targets[name]
    if isinstance(value, str):
        return {"url": value, "method": "GET"}
    return value


def register_http_tools(mcp: Any, app_config: Any) -> None:
    @mcp.tool()
    def http_probe(target: str, timeout_s: float = 5.0) -> dict[str, Any]:
        """Probe an allowlisted HTTP target and return status, latency and truncated response."""
        cfg = _target(app_config.raw, target)
        url = cfg["url"]
        method = cfg.get("method", "GET").upper()
        headers = cfg.get("headers", {})
        max_bytes = int(cfg.get("max_response_bytes", 1000))
        req = urllib.request.Request(url, method=method, headers=headers)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = resp.read(max_bytes)
                latency_ms = round((time.monotonic() - started) * 1000, 2)
                return {
                    "ok": 200 <= resp.status < 400,
                    "target": target,
                    "url": url,
                    "status": resp.status,
                    "reason": resp.reason,
                    "latency_ms": latency_ms,
                    "headers": {k.lower(): v for k, v in resp.headers.items() if k.lower() in {"content-type", "server", "location"}},
                    "body_preview": body.decode("utf-8", errors="replace"),
                }
        except urllib.error.HTTPError as exc:
            body = exc.read(max_bytes)
            latency_ms = round((time.monotonic() - started) * 1000, 2)
            return {
                "ok": False,
                "target": target,
                "url": url,
                "status": exc.code,
                "reason": exc.reason,
                "latency_ms": latency_ms,
                "body_preview": body.decode("utf-8", errors="replace"),
            }
        except Exception as exc:
            latency_ms = round((time.monotonic() - started) * 1000, 2)
            return {"ok": False, "target": target, "url": url, "error": str(exc), "latency_ms": latency_ms}
