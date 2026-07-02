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
import urllib.error
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


# ── Claude proxy — the key lives here (env var), never in the browser ──
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
# join+split scrubs ALL whitespace — keys pasted into Render with line breaks still work
ENV_API_KEY = "".join(os.environ.get("ANTHROPIC_API_KEY", "").split())

# Other service keys (set in Render → Environment; the dashboard auto-detects them)
ENV_KEYS_STATUS = {
    "claude": lambda: bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
    "basiq": lambda: bool(os.environ.get("BASIQ_API_KEY", "").strip()),
    "twilio": lambda: bool(
        os.environ.get("TWILIO_SID", "").strip() and os.environ.get("TWILIO_TOKEN", "").strip()
    ),
}

# ── Airbnb iCal proxy (browsers can't fetch .ics cross-origin) ──
_ICAL_CACHE = {}
_ICAL_TTL = 1800  # 30 min
_ICAL_ALLOWED = ("airbnb.com", "airbnb.com.au", "muscache.com", "vrbo.com", "booking.com")


def fetch_ical(url):
    host = urllib.parse.urlparse(url).hostname or ""
    if not any(host == d or host.endswith("." + d) for d in _ICAL_ALLOWED):
        return {"error": "host_not_allowed", "events": []}
    hit = _ICAL_CACHE.get(url)
    if hit and (time.time() - hit[0]) < _ICAL_TTL:
        return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (JacksOS calendar)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode("utf-8", "ignore")
    # unfold continuation lines, then walk VEVENTs
    lines = raw.replace("\r\n ", "").replace("\r", "").split("\n")
    events, cur = [], None
    for ln in lines:
        if ln.startswith("BEGIN:VEVENT"):
            cur = {}
        elif ln.startswith("END:VEVENT") and cur is not None:
            if cur.get("start") and cur.get("end"):
                events.append(cur)
            cur = None
        elif cur is not None:
            if ln.startswith("DTSTART"):
                cur["start"] = _ical_date(ln)
            elif ln.startswith("DTEND"):
                cur["end"] = _ical_date(ln)
            elif ln.startswith("SUMMARY:"):
                cur["summary"] = ln[8:].strip()
    result = {"events": events}
    _ICAL_CACHE[url] = (time.time(), result)
    return result


def _ical_date(line):
    val = line.split(":", 1)[-1].strip()[:8]  # YYYYMMDD
    if len(val) == 8 and val.isdigit():
        return f"{val[0:4]}-{val[4:6]}-{val[6:8]}"
    return None


def call_claude(payload):
    # Prefer the server-side env-var key (secure). Fall back to a key the client
    # passes (Jack's on-device key) so AI works before the env var is set.
    key = "".join((payload.get("apiKey") or ENV_API_KEY or "").split())
    if not key:
        return {"error": "not_configured", "text": ""}
    body = {
        "model": payload.get("model") or "claude-opus-4-8",
        "max_tokens": min(int(payload.get("max_tokens") or 1024), 4096),
        "messages": payload.get("messages") or [],
    }
    if payload.get("system"):
        body["system"] = payload["system"]
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": "api_error", "text": "", "detail": e.read().decode("utf-8", "ignore")[:400]}
    except Exception as e:
        msg = str(e).replace(key, "***")  # never echo key material in errors
        return {"error": msg[:200], "text": ""}
    parts = resp.get("content") or []
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return {"text": text, "model": resp.get("model", "")}


class Handler(SimpleHTTPRequestHandler):
    """Static handler + news + Claude proxy, with sane headers and an index.html fallback."""

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

    def do_POST(self):
        if self.path == "/api/ai":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {}
            return self._send_json(call_claude(payload))
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/ai/status":
            return self._send_json({"configured": bool(ENV_API_KEY)})
        if self.path == "/api/status":
            return self._send_json({k: fn() for k, fn in ENV_KEYS_STATUS.items()})
        if self.path.startswith("/api/ical"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            url = (qs.get("url") or [""])[0]
            try:
                return self._send_json(fetch_ical(url))
            except Exception as e:
                return self._send_json({"error": str(e), "events": []})
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
