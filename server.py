#!/usr/bin/env python3
"""
Jack's OS — pure-Python static server for Render.

Serves the single self-contained index.html plus a tiny read-only news proxy
(`/api/news?topic=...`) that fetches Australia-localized Google News RSS
server-side (browsers can't fetch RSS cross-origin). Stdlib only — no pip deps.

Render injects the port to bind via the PORT env var; we bind 0.0.0.0:$PORT.
"""

import os
import sys
import json
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else "8000"))
ROOT = os.path.dirname(os.path.abspath(__file__))

# What "relevant to Jack" means per widget — AU-localized search queries.
NEWS_TOPICS = {
    "property":   "Australian property market house prices interest rates real estate",
    "shares":     "ASX shares gold price silver price oil price superannuation Australia",
    "business":   "Australian business economy small business inflation",
    "government": "Australian federal government policy tax budget Centrelink",
}
_NEWS_CACHE = {}          # topic -> (fetched_at, items)
_NEWS_TTL = 900           # 15 min


def fetch_news(topic):
    q = NEWS_TOPICS.get(topic)
    if not q:
        return []
    hit = _NEWS_CACHE.get(topic)
    if hit and (time.time() - hit[0]) < _NEWS_TTL:
        return hit[1]
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "en-AU", "gl": "AU", "ceid": "AU:en"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (JacksOS news)"})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = r.read()
    root = ET.fromstring(data)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        date = (it.findtext("pubDate") or "").strip()
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        # Google titles are "Headline - Source"; strip the trailing source.
        if " - " in title:
            head, tail = title.rsplit(" - ", 1)
            title = head
            if not source:
                source = tail
        if title and link:
            items.append({"title": title, "link": link, "source": source, "date": date})

    def _ts(s):
        try:
            return parsedate_to_datetime(s).timestamp()
        except Exception:
            return 0.0

    items.sort(key=lambda x: _ts(x["date"]), reverse=True)   # freshest first
    items = items[:8]
    _NEWS_CACHE[topic] = (time.time(), items)
    return items


class Handler(SimpleHTTPRequestHandler):
    """Static handler + news proxy, with sane headers and an index.html fallback."""

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        super().end_headers()

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/news"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            topic = (qs.get("topic") or ["property"])[0]
            try:
                return self._send_json({"topic": topic, "items": fetch_news(topic)})
            except Exception as e:  # never 500 the widget — return empty + reason
                return self._send_json({"topic": topic, "items": [], "error": str(e)})
        # Single-page app: unknown paths fall back to index.html.
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.path = "/index.html"
        return super().do_GET()

    def log_message(self, fmt, *args):
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
