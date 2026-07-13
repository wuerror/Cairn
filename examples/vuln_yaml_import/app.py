"""Intentionally vulnerable demo target for Cairn P3 e2e.

Modes:
1. Marker mode (default): if body contains CAIRN_POC_OK → respond with success marker.
2. Unsafe YAML mode: if body looks like yaml with !!python, echo success marker
   without actually deserializing (safe for labs; still exercises harness + oracle).

Run:
  python examples/vuln_yaml_import/app.py
  # listens on http://127.0.0.1:18080
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer


HOST = "127.0.0.1"
PORT = 18080
SUCCESS = b"CAIRN_POC_OK token=demo\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/health"):
            self._reply(200, b"ok vuln_yaml_import\n")
            return
        self._reply(404, b"not found\n")

    def do_POST(self):  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/import":
            self._reply(404, b"not found\n")
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        lowered = body.lower()
        # dual-mode oracle: marker probe OR yaml gadget marker
        if "cairn_poc_ok" in lowered or "!!python" in body or "yaml.load" in lowered:
            self._reply(200, SUCCESS)
            return
        self._reply(200, b"imported ok\n")

    def log_message(self, format, *args):  # noqa: A003
        return

    def _reply(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = HTTPServer((HOST, PORT), Handler)
    print(f"vuln_yaml_import listening on http://{HOST}:{PORT}")
    print("POST /api/import  body containing CAIRN_POC_OK or !!python → success")
    server.serve_forever()


if __name__ == "__main__":
    main()
