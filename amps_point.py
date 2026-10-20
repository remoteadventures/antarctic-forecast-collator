"""Extract an AMPS (Antarctic WRF) point forecast from the real-time GRIB feed.

Downloads only the needed GRIB records via HTTP byte ranges using the .idx files.
Usage: python amps_point.py [RUN=YYYYMMDDHH|latest] [DOMAIN=d5] [LAT LON]
"""
import csv, http.client, math, re, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import eccodes

BASE = "https://www2.mmm.ucar.edu/rt/amps/wrf_grib"
UA = {"User-Agent": "Mozilla/5.0 (amps-point research script)"}
NZFX = (-77.956, 166.766)  # Phoenix Airfield

# (output name, idx variable, idx level text)
FIELDS = [
    ("u10", "UGRD", "10 m above gnd"),
    ("v10", "VGRD", "10 m above gnd"),
    ("t2", "TMP", "2 m above gnd"),
    ("rh2", "RH", "2 m above gnd"),
    ("snmr2", "SNMR", "2 m above gnd"),
    ("cldbase", "HGT", "cld base"),
    ("mslp", "PRMSL", "MSL"),
    ("ustar", "FRICV", "atmos col"),
    ("u150", "UGRD", "150 m above MSL"),
    ("v150", "VGRD", "150 m above MSL"),
]


def get(url, rng=None, attempts=4):
    """GET (optionally a byte range), retrying timeouts and connection errors: UCAR's server is sometimes slow."""
    h = dict(UA)
    if rng:
        h["Range"] = f"bytes={rng[0]}-{rng[1]}"
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r:
                return r.read()
        except (OSError, http.client.HTTPException) as e:  # URLError, timeouts and resets are OSErrors
            if attempt == attempts - 1 or (isinstance(e, urllib.error.HTTPError) and e.code < 500):
                raise
            time.sleep(5 * (attempt + 1))


def listing(url):
    return re.findall(r"href=['\"]?([^'\" >]+)", get(url).decode())


def latest_run():
    return sorted(d for d in listing(BASE + "/") if re.fullmatch(r"\d{10}", d))[-1]


def parse_idx(text):
    recs = [l.split(":") for l in text.strip().splitlines()]
    out = []
    for i, r in enumerate(recs):
        start = int(r[1])
        end = int(recs[i + 1][1]) - 1 if i + 1 < len(recs) else None
        out.append({"var": r[3], "level": r[11], "desc": r[12], "start": start, "end": end})
    return out


def decode(msg_bytes):
    gid = eccodes.codes_new_from_message(msg_bytes)
    try:
        return eccodes.codes_get_values(gid)
    finally:
        eccodes.codes_release(gid)


def fetch_record(grb_url, rec):
    end = rec["end"] if rec["end"] is not None else rec["start"] + 50_000_000
    return decode(get(grb_url, (rec["start"], end)))


def rnd(x, nd, scale=1.0):
    return None if x is None else round(x * scale, nd)


def main():
    run = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "latest" else latest_run()
    dom = sys.argv[2] if len(sys.argv) > 2 else "d5"
    lat, lon = (float(sys.argv[3]), float(sys.argv[4])) if len(sys.argv) > 4 else NZFX
    files = [f for f in listing(f"{BASE}/{run}/") if f.startswith(f"{run}_WRF_{dom}_f") and f.endswith(".grb")]
    hours = sorted(int(re.search(r"_f(\d+)\.grb", f).group(1)) for f in files)
    print(f"run {run} domain {dom}: {len(hours)} forecast hours", file=sys.stderr)

    # locate nearest grid point from the NLAT/ELON fields of the first file
    stem0 = f"{BASE}/{run}/{run}_WRF_{dom}_f{hours[0]:03d}"
    recs0 = parse_idx(get(stem0 + ".idx").decode())
    glat = fetch_record(stem0 + ".grb", next(r for r in recs0 if r["var"] == "NLAT"))
    glon = fetch_record(stem0 + ".grb", next(r for r in recs0 if r["var"] == "ELON"))
    dlon = (glon - lon + 180) % 360 - 180
    d = (glat - lat) ** 2 + (dlon * math.cos(math.radians(lat))) ** 2
    idx_point = int(np.argmin(d))
    km = math.sqrt(d[idx_point]) * 111.2
    # bearing of the grid +x axis at the point, for rotating grid-relative winds to true
    j = idx_point + 1  # neighbour along x (x varies fastest)
    dy = glat[j] - glat[idx_point]
    dx = ((glon[j] - glon[idx_point] + 180) % 360 - 180) * math.cos(math.radians(lat))
    theta = math.atan2(dx, dy)
    print(f"nearest point {glat[idx_point]:.4f},{glon[idx_point]:.4f} ({km:.2f} km away)", file=sys.stderr)

    def fetch_hour(fh):
        stem = f"{BASE}/{run}/{run}_WRF_{dom}_f{fh:03d}"
        recs = parse_idx(get(stem + ".idx").decode())
        row = {"run": run, "fh": fh}
        for name, var, lev in FIELDS:
            rec = next((r for r in recs if r["var"] == var and r["level"] == lev), None)
            row[name] = float(fetch_record(stem + ".grb", rec)[idx_point]) if rec else None
        # 1-h precip accumulation (record described as "N-1-N hr acc")
        rec = next((r for r in recs if r["var"] == "APCP" and r["desc"] == f"{fh-1}-{fh}hr acc"), None)
        row["pcp1h_mm"] = float(fetch_record(stem + ".grb", rec)[idx_point]) if rec else None
        for a, b in (("u10", "v10"), ("u150", "v150")):
            if row[a] is not None:  # grid-relative -> earth-relative (east, north)
                ug, vg = row[a], row[b]
                row[a] = ug * math.sin(theta) - vg * math.cos(theta)
                row[b] = ug * math.cos(theta) + vg * math.sin(theta)
        return row

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(fetch_hour, hours))

    w = csv.writer(sys.stdout)
    w.writerow(["run", "fh", "wdir", "wspd_kt", "w150_kt", "t2_c", "rh2", "snmr2_gkg", "cldbase_m",
                "mslp_hpa", "ustar", "pcp1h_mm"])
    for r in rows:
        if None in (r["u10"], r["v10"], r["t2"]):
            print(f"f{r['fh']:03d}: surface fields missing, skipped", file=sys.stderr)
            continue
        spd = math.hypot(r["u10"], r["v10"]) * 1.94384
        wdir = (math.degrees(math.atan2(-r["u10"], -r["v10"])) + 360) % 360  # degrees true, from
        w150 = math.hypot(r["u150"], r["v150"]) * 1.94384 if r["u150"] is not None else None
        w.writerow([r["run"], r["fh"], round(wdir), round(spd, 1), w150 and round(w150, 1),
                    round(r["t2"] - 273.15, 1), rnd(r["rh2"], 0), rnd(r["snmr2"], 3, 1000),
                    rnd(r["cldbase"], 0), rnd(r["mslp"], 1, 0.01), rnd(r["ustar"], 2),
                    r["pcp1h_mm"] if r["pcp1h_mm"] is None else round(r["pcp1h_mm"], 2)])


if __name__ == "__main__":
    main()
