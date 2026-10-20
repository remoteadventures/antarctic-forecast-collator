"""Local server for Antarctic Forecast Collator.

Serves app/ as static files and proxies METAR/TAF from aviationweather.gov
(which does not allow direct browser requests).
Usage: python serve.py [PORT=8765]
"""
import json, re, sys, time, urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP = Path(__file__).parent / "app"
AWC = "https://aviationweather.gov/api/data"
CACHE, TTL = {}, 300
ICAO_LIST = re.compile(r"^[A-Z0-9]{4}(,[A-Z0-9]{4})*$")
LOOKBACK_H = 168  # many Antarctic stations report intermittently: look back a week for the latest report
SERIES_H = 30     # hours of METARs returned for plotting


def awc(kind, ids, hours):
    key = (kind, ids, hours)
    if key in CACHE and time.time() - CACHE[key][0] < TTL:
        return CACHE[key][1]
    url = f"{AWC}/{kind}?ids={ids}&format=json" + (f"&hours={hours}" if hours else "")
    req = urllib.request.Request(url, headers={"User-Agent": "antarctic-forecast-collator/0.1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read()
    data = json.loads(body) if body.strip() else []
    CACHE[key] = (time.time(), data)
    return data


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/obs":
            q = parse_qs(u.query)
            out = {"metar": [], "latest": {}, "taf": []}
            try:
                ids = q.get("metar", [""])[0].upper()
                if ICAO_LIST.match(ids):
                    reports = awc("metar", ids, LOOKBACK_H)
                    cutoff = time.time() - SERIES_H * 3600
                    out["metar"] = [r for r in reports if r["obsTime"] >= cutoff]
                    for code in ids.split(","):
                        mine = [r for r in reports if r["icaoId"] == code]
                        out["latest"][code] = max(mine, key=lambda r: r["obsTime"]) if mine else None
                ids = q.get("taf", [""])[0].upper()
                if ICAO_LIST.match(ids):
                    out["taf"] = awc("taf", ids, None)
                self._json(200, out)
            except Exception as e:  # upstream failure: report, don't crash the page
                self._json(502, {"error": str(e)})
            return
        super().do_GET()

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if self.path.endswith((".json", ".html", "/")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"serving http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(APP))).serve_forever()
