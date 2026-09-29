import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOL_DIR not in sys.path:
    sys.path.insert(0, TOOL_DIR)

FIXTURES = os.path.join(TOOL_DIR, "tests", "fixtures")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _serve(self, method):
        srv = self.server
        srv.log.append((method, self.path, self.headers.get("User-Agent", ""), time.monotonic()))
        if method == "HEAD" and srv.no_head:
            status, headers, body = 405, {}, b""
        else:
            status, headers, body = srv.routes.get(self.path, (404, {"Content-Type": "text/html"}, b"<h1>404</h1>"))
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        self._serve("GET")

    def do_HEAD(self):
        self._serve("HEAD")


@pytest.fixture
def serve():
    """serve(routes, no_head=False) -> server with .base ('http://127.0.0.1:PORT') and .log."""
    servers = []

    def start(routes, no_head=False):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)  # port 0: a random free port
        srv.routes = routes
        srv.no_head = no_head
        srv.log = []
        srv.base = f"http://127.0.0.1:{srv.server_address[1]}"
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def html(title, links=()):
    body = "".join(f'<a href="{h}">{h}</a>' for h in links)
    return (200, {"Content-Type": "text/html; charset=utf-8"},
            f"<html><head><title>{title}</title></head><body>{body}</body></html>")


def redirect(location, status=301):
    return (status, {"Location": location}, b"")


@pytest.fixture
def pages():
    """Helpers to build routes: pages.html(title, links), pages.redirect(location, status)."""
    class P:
        pass
    p = P()
    p.html = html
    p.redirect = redirect
    return p


@pytest.fixture
def fixtures_dir():
    return FIXTURES
