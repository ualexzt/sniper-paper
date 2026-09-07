"""Small read-only dashboard HTTP server bound to loopback by default."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

from .dashboard import render_dashboard_html
from .dashboard_adapter import journal_dashboard
from .storage import Journal


def dashboard_server(
    journal: Journal,
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    allow_nonloopback: bool = False,
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "::1", "localhost"} and not allow_nonloopback:
        raise ValueError("non-loopback dashboard binding requires explicit allow_nonloopback")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/" or self.path.startswith("/?"):
                payload = render_dashboard_html(provider=lambda: journal_dashboard(journal)).encode()
                self._send(HTTPStatus.OK, "text/html; charset=utf-8", payload)
            elif self.path == "/api/snapshot":
                payload = json.dumps(journal_dashboard(journal), separators=(",", ":")).encode()
                self._send(HTTPStatus.OK, "application/json", payload)
            elif self.path == "/healthz":
                state = journal_dashboard(journal)
                service_ok = all(item["status"] == "Healthy" for item in state["service_health"])
                books_ok = any(
                    item["label"] == "Market books" and item["status"] == "Ready" for item in state["data_health"]
                )
                status = HTTPStatus.OK if service_ok and books_ok else HTTPStatus.SERVICE_UNAVAILABLE
                self._send(status, "text/plain", b"ok\n" if status == HTTPStatus.OK else b"not ready\n")
            else:
                self._send(HTTPStatus.NOT_FOUND, "text/plain", b"not found\n")

        def do_POST(self) -> None:
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, "text/plain", b"read only\n")

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, status: HTTPStatus, content_type: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
            )
            self.end_headers()
            self.wfile.write(payload)

    return ThreadingHTTPServer((host, port), Handler)


def start_dashboard(
    journal: Journal,
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    allow_nonloopback: bool = False,
) -> tuple[ThreadingHTTPServer, Thread]:
    server = dashboard_server(journal, host, port, allow_nonloopback=allow_nonloopback)
    thread = Thread(target=server.serve_forever, name="dashboard", daemon=True)
    thread.start()
    return server, thread
