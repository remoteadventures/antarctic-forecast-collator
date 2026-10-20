"""Export AMPS point forecasts for every location in app/locations.json to app/data/amps.json.

Each location is served from the finest AMPS domain that contains it (d5/d6 ~0.85 km,
d3 ~2.7 km, then the 3-hourly d2/d1). One download of each GRIB record serves all points.
Usage: python amps_export.py [RUN=YYYYMMDDHH|latest] [--if-new] [--out=PATH]
  latest    newest run whose d5 domain is fully uploaded (UCAR uploads files over several hours)
  --if-new  exit without downloading if app/data/amps.json already holds that run
  --out     write somewhere other than app/data/amps.json (.gz is compressed); used for backfilling the archive
"""
import datetime as dt, gzip, json, math, pathlib, re, sys
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import eccodes
from amps_point import BASE, FIELDS, get, listing, parse_idx, fetch_record

HERE = pathlib.Path(__file__).parent
DOMAINS = ["d5", "d6", "d3", "d2", "d1"]  # finest first
LONG_DOMAINS = {"d2", "d1"}  # 3-hourly to 120 h; the fine nests (d3/d5/d6) stop at 39 h
MIN_D5_HOURS = 37
P_LEVELS = [975, 950, 925, 900, 875, 850, 800, 700, 600, 500]  # AMPS pressure levels (hPa) used for the 2000 ft wind
AGL_2000FT = 609.6


def levels_for(psfc_hpa):
    """Pressure levels bracketing ~2000 ft above a surface at psfc (plus one either side for safety)."""
    target = psfc_hpa * math.exp(-AGL_2000FT / 7500)  # scale height ~7.5 km in cold air
    below = [l for l in P_LEVELS if l >= target]       # higher pressure = lower down
    above = [l for l in P_LEVELS if l < target]
    return set(below[-2:] + above[:2])


def wind_at_height(levels, hgt_sfc, w10, theta):
    """Interpolate grid-relative (u, v) to 2000 ft AGL between the bracketing levels (10 m wind as the bottom).
    levels: list of (height MSL m, u, v). Returns (speed kt, direction deg) or (None, None)."""
    pts = [(10.0, *w10)] + sorted((z - hgt_sfc, u, v) for z, u, v in levels if z - hgt_sfc > 10)
    pts.sort()
    for (z0, u0, v0), (z1, u1, v1) in zip(pts, pts[1:]):
        if z0 <= AGL_2000FT <= z1:
            f = (AGL_2000FT - z0) / (z1 - z0)
            ug, vg = u0 + (u1 - u0) * f, v0 + (v1 - v0) * f
            ue, ve = ug * math.sin(theta) - vg * math.cos(theta), ug * math.cos(theta) + vg * math.sin(theta)
            return round(math.hypot(ue, ve) * 1.94384, 1), round((math.degrees(math.atan2(-ue, -ve)) + 360) % 360)
    return None, None  # a complete d5 run has f000-f039; fewer means still uploading


def latest_complete_run():
    runs = sorted((d for d in listing(BASE + "/") if re.fullmatch(r"\d{10}", d)), reverse=True)
    for run in runs[:3]:
        n = sum(1 for f in listing(f"{BASE}/{run}/") if re.fullmatch(rf"{run}_WRF_d5_f\d+\.grb", f))
        if n >= MIN_D5_HOURS:
            return run
        print(f"{run}: only {n} d5 hours uploaded so far, skipping", file=sys.stderr)
    raise SystemExit("no complete AMPS run found")


def gc_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km (works with numpy arrays and at the pole)."""
    p1, p2, dl = np.radians(lat1), np.radians(lat2), np.radians(lon2 - lon1)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 6371.0 * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def bearing(lat1, lon1, lat2, lon2):
    p1, p2, dl = map(math.radians, (lat1, lat2, lon2 - lon1))
    return math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))


POLAR_CAP = 89.5  # within this of the pole, directions are given relative to grid north (Greenwich)


def grid_lookup(glat, glon, lat, lon, nx):
    """Nearest grid index, its distance (km), and the +x axis bearing (rad) at that point.

    Bearing is from true north, or from grid north inside the polar cap. Uses points 3 cells
    either side along x, since the packed NLAT/ELON fields are too coarse for adjacent cells.
    """
    d = gc_km(lat, lon, glat, glon)
    i = int(np.argmin(d))
    col = i % nx
    a, b = i - min(3, col), i + min(3, nx - 1 - col)
    if abs(lat) > POLAR_CAP:  # azimuthal plane: +Y points along the Greenwich meridian
        r = 90 - np.abs(glat[[a, b]])
        X, Y = r * np.sin(np.radians(glon[[a, b]])), r * np.cos(np.radians(glon[[a, b]]))
        theta = math.atan2(X[1] - X[0], Y[1] - Y[0])
    else:
        theta = bearing(glat[a], glon[a], glat[b], glon[b])
    return i, float(d[i]), theta


def vis_from_snow(snmr, t_k, p_pa=97_000):
    """Visibility (m) from falling-snow mixing ratio, Stoelinga & Warner (1999). Ignores blowing snow."""
    rho = p_pa / (287.0 * t_k)
    conc = max(snmr, 0) * rho * 1000  # g/m3
    if conc < 1e-4:
        return 20_000  # no falling snow: clear (blowing snow and fog aren't modelled here)
    beta = 10.36 * conc ** 0.7776  # extinction, 1/km
    return min(-math.log(0.02) / beta * 1000, 20_000)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    run = args[0] if args and args[0] != "latest" else latest_complete_run()
    out_arg = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--out=")), None)
    dest = pathlib.Path(out_arg) if out_arg else HERE / "app/data/amps.json"
    if "--if-new" in sys.argv and dest.exists():
        have = json.loads(dest.read_text()).get("run", "")
        if have.startswith(dt.datetime.strptime(run, "%Y%m%d%H").strftime("%Y-%m-%dT%H")):
            print(f"AMPS run {run} already exported; nothing to do", file=sys.stderr)
            return
    locs = json.loads((HERE / "app/locations.json").read_text())
    files = listing(f"{BASE}/{run}/")
    run_time = dt.datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
    out = {"model": "AMPS (Polar WRF)", "run": run_time.isoformat(), "generated": dt.datetime.now(dt.timezone.utc).isoformat(),
           "locations": {}}
    primary, extension = {}, {}  # location id -> (domain, grid_km, dist, step, dir_ref, series)

    for dom in DOMAINS:
        # fine nests serve sites with no domain yet; long-range domains also extend sites beyond the fine nests
        pending = [l for l in locs if l["id"] not in primary or (dom in LONG_DOMAINS and l["id"] not in extension)]
        if not pending:
            continue
        hours = sorted(int(m.group(1)) for f in files if (m := re.fullmatch(rf"{run}_WRF_{dom}_f(\d+)\.grb", f)))
        if not hours:
            continue
        stem0 = f"{BASE}/{run}/{run}_WRF_{dom}_f{hours[0]:03d}"
        recs0 = parse_idx(get(stem0 + ".idx").decode())
        glat = fetch_record(stem0 + ".grb", next(r for r in recs0 if r["var"] == "NLAT"))
        glon = fetch_record(stem0 + ".grb", next(r for r in recs0 if r["var"] == "ELON"))
        rec = next(r for r in recs0 if r["var"] == "NLAT")
        gid = eccodes.codes_new_from_message(get(stem0 + ".grb", (rec["start"], rec["end"])))
        nx = eccodes.codes_get(gid, "Nx")
        dx = eccodes.codes_get(gid, "DxInMetres") / 1000
        eccodes.codes_release(gid)
        points = {}
        for loc in pending:
            i, dist, theta = grid_lookup(glat, glon, loc["lat"], loc["lon"], nx)
            if dist <= 1.5 * dx:  # inside this domain
                points[loc["id"]] = (i, dist, dx, theta, "grid" if abs(loc["lat"]) > POLAR_CAP else "true")
        if not points:
            continue
        # surface pressure and terrain at each point decide which pressure levels the 2000 ft wind needs
        sfc = {name: fetch_record(stem0 + ".grb", next(r for r in recs0 if r["var"] == var and r["level"] == "sfc"))
               for name, var in (("psfc", "PRES"), ("hgt", "HGT"))}
        hgt_sfc = {lid: float(sfc["hgt"][pt[0]]) for lid, pt in points.items()}
        need = sorted(set().union(*(levels_for(float(sfc["psfc"][pt[0]]) / 100) for pt in points.values())), reverse=True)
        level_fields = [(f"{k}{l}", var, f"{l} mb") for l in need for k, var in (("u", "UGRD"), ("v", "VGRD"), ("z", "HGT"))]
        print(f"{dom}: {', '.join(points)} ({len(hours)} hours; 2000 ft wind from {need} hPa)", file=sys.stderr)

        def fetch_hour(fh):
            stem = f"{BASE}/{run}/{run}_WRF_{dom}_f{fh:03d}"
            recs = parse_idx(get(stem + ".idx").decode())
            vals = {}
            for name, var, lev in FIELDS + level_fields:
                rec = next((r for r in recs if r["var"] == var and r["level"] == lev), None)
                vals[name] = fetch_record(stem + ".grb", rec) if rec else None
            step = 1 if dom in ("d3", "d5", "d6") else 3
            rec = next((r for r in recs if r["var"] == "APCP" and r["desc"] == f"{fh-step}-{fh}hr acc"), None)
            vals["pcp"] = fetch_record(stem + ".grb", rec) if rec else None
            return fh, step, vals

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(fetch_hour, hours))

        for lid, (i, dist, dx, theta, dir_ref) in points.items():
            series = []
            for fh, step, v in results:
                if v["u10"] is None or v["t2"] is None:
                    continue
                at = lambda k: None if v[k] is None else float(v[k][i])
                ug, vg = at("u10"), at("v10")
                ue, ve = ug * math.sin(theta) - vg * math.cos(theta), ug * math.cos(theta) + vg * math.sin(theta)
                t2 = at("t2")
                cb = at("cldbase")
                lv = [(at(f"z{l}"), at(f"u{l}"), at(f"v{l}")) for l in need]
                ws2k, wd2k = wind_at_height([x for x in lv if None not in x], hgt_sfc[lid], (ug, vg), theta)
                series.append({
                    "time": (run_time + dt.timedelta(hours=fh)).strftime("%Y-%m-%dT%H:%M"),
                    "fh": fh,
                    "wind_speed_10m": round(math.hypot(ue, ve) * 1.94384, 1),
                    "wind_direction_10m": round((math.degrees(math.atan2(-ue, -ve)) + 360) % 360),
                    "wind_speed_2000ft": ws2k,
                    "wind_direction_2000ft": wd2k,
                    "temperature_2m": round(t2 - 273.15, 1),
                    "relative_humidity_2m": None if at("rh2") is None else round(at("rh2")),
                    "pressure_msl": None if at("mslp") is None else round(at("mslp") / 100, 1),
                    "cloud_base": None if cb is None or cb >= 8999 else round(cb),
                    "visibility_snow": None if at("snmr2") is None else vis_from_snow(at("snmr2"), t2),
                    "precipitation": None if v["pcp"] is None else round(at("pcp") / step, 2),
                    "ustar": None if at("ustar") is None else round(at("ustar"), 3),
                    "domain": dom,
                })
            entry = (dom, round(dx, 2), round(dist, 2), results[0][1] if results else None, dir_ref, series)
            if lid not in primary:
                primary[lid] = entry
                if dom in LONG_DOMAINS:
                    extension[lid] = None  # already long-range: no extension needed
            elif dom in LONG_DOMAINS and lid not in extension:
                extension[lid] = entry

    for lid, (dom, gkm, dist, step, dir_ref, series) in primary.items():
        rec = {"domain": dom, "grid_km": gkm, "point_dist_km": dist, "step_h": step, "dir_ref": dir_ref, "hourly": series}
        if extension.get(lid) and series:
            edom, egkm, _, estep, _, eseries = extension[lid]
            last = series[-1]["fh"]
            tail = [h for h in eseries if h["fh"] > last]
            if tail:
                rec["hourly"] = series + tail
                rec["extended"] = {"domain": edom, "grid_km": egkm, "step_h": estep, "from_fh": tail[0]["fh"]}
        out["locations"][lid] = rec
    for loc in locs:
        if loc["id"] not in primary:
            print(f"{loc['id']}: outside all AMPS domains", file=sys.stderr)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.suffix == ".gz":
        with gzip.open(dest, "wt", encoding="utf-8") as f:
            json.dump(out, f, separators=(",", ":"))
    else:
        dest.write_text(json.dumps(out, separators=(",", ":")))
    print(f"wrote {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
