#!/usr/bin/env python3
"""Answer every HTTP request on port 80 with a redirect to the Humdrum web UI.

The app's web server (the App Lab WebUI brick) listens on its own port inside a
container; phones only probe port 80, so this bridges the two without touching
Docker's networking. Standard library only.
"""

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET = os.environ.get("PORTAL_TARGET", "http://10.42.0.1:7000/")
PORT = int(os.environ.get("PORTAL_PORT", "80"))

BODY = f"""<!doctype html><meta name=viewport content="width=device-width">
<title>Humdrum</title>
<body style="font:16px system-ui;padding:24px;background:#e9e8e4">
<p><a href="{TARGET}" style="color:#ff4d00">Open Humdrum &rarr;</a></p>
<script>location.replace({TARGET!r})</script>
""".encode()


class Redirect(BaseHTTPRequestHandler):
    def _send(self, body: bool) -> None:
        self.send_response(302)
        self.send_header("Location", TARGET)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        if body:
            self.wfile.write(BODY)

    def do_GET(self):
        self._send(True)

    do_POST = do_GET

    def do_HEAD(self):
        self._send(False)

    def log_message(self, fmt, *args):
        pass  # phones probe constantly; keep the journal quiet


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Redirect).serve_forever()
