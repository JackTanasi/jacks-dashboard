#!/usr/bin/env python3
"""
Jack's OS — pure-Python static server for Render.

Serves the single self-contained index.html (Firebase / Gmail / Claude all run
client-side, so there is nothing to do server-side except hand over the file).

Render injects the port to bind via the PORT env var; we bind 0.0.0.0:$PORT.
Stdlib only — no pip dependencies.
"""

import os
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# Render injects $PORT. Fall back to an optional CLI arg, then 8000 (local dev).
PORT = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else "8000"))
ROOT = os.path.dirname(os.path.abspath(__file__))


class Handler(SimpleHTTPRequestHandler):
    """Static handler with sane headers and an index.html fallback."""

    def end_headers(self):
        # Always revalidate the app shell so a redeploy is picked up instantly;
        # everything else (fonts/libs) comes from third-party CDNs anyway.
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        super().end_headers()

    def do_GET(self):
        # Single-page app: any unknown path falls back to index.html so deep
        # links / refreshes don't 404.
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.path = "/index.html"
        return super().do_GET()

    def log_message(self, fmt, *args):
        # Compact one-line logs in the Render log stream.
        print("%s - %s" % (self.address_string(), fmt % args))


def main():
    handler = partial(Handler, directory=ROOT)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), handler)
    print(f"Jack's OS serving {ROOT} on 0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
