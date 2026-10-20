"""Build app/data/amps_history.json: what each recent AMPS run forecast for every site.

Reads the archived exports (archive/amps/**/amps_<run>.json.gz) plus the current app/data/amps.json,
keeps runs from the last HISTORY_H hours before the newest run, and for each run keeps the valid times
from the newest run's start to KEEP_AHEAD_H after it (enough for the 48 h run grid until the next run).
Stored column-wise to keep the file small. Run after amps_export.py / harvest.py.
"""
import datetime as dt, gzip, json, pathlib

HERE = pathlib.Path(__file__).parent
HISTORY_H = 96
KEEP_AHEAD_H = 72  # newest run + 72 h: covers "now to +48 h" until the next run arrives
FIELDS = {  # output key -> export field
    "ws": "wind_speed_10m", "wd": "wind_direction_10m", "t": "temperature_2m", "cb": "cloud_base",
    "vis": "visibility_snow", "p": "precipitation", "mslp": "pressure_msl",
}


def load_runs():
    runs = {}
    for f in sorted((HERE / "archive/amps").glob("*/*/amps_*.json.gz")):
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
        runs[d["run"]] = d
    cur = HERE / "app/data/amps.json"
    if cur.exists():
        d = json.loads(cur.read_text())
        runs[d["run"]] = d
    return runs


def main():
    runs = load_runs()
    if not runs:
        raise SystemExit("no AMPS runs found")
    newest = max(dt.datetime.fromisoformat(r) for r in runs)
    lo, hi = newest - dt.timedelta(hours=HISTORY_H), newest + dt.timedelta(hours=KEEP_AHEAD_H)
    keep = sorted(r for r in runs if dt.datetime.fromisoformat(r) >= lo)
    out = {"model": "AMPS", "generated": dt.datetime.now(dt.timezone.utc).isoformat(), "runs": keep, "sites": {}}
    for r in keep:
        for site, L in runs[r]["locations"].items():
            rows = [h for h in L["hourly"] if newest <= dt.datetime.fromisoformat(h["time"] + "+00:00") <= hi]
            if not rows:
                continue
            col = {"time": [h["time"] for h in rows], "dom": [h.get("domain", L["domain"]) for h in rows]}
            for k, src in FIELDS.items():
                col[k] = [h.get(src) if not isinstance(h.get(src), float) else round(h[src], 2) for h in rows]
            out["sites"].setdefault(site, {})[r] = col
    dest = HERE / "app/data/amps_history.json"
    dest.write_text(json.dumps(out, separators=(",", ":")))
    print(f"wrote {dest}: {len(keep)} runs ({keep[0][:13]} to {keep[-1][:13]}), {dest.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
