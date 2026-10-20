"""Archive observations and model forecasts for verification.

Run on a schedule (see .github/workflows/harvest.yml). Each step is idempotent:
- METAR/TAF for every station in app/locations.json -> archive/obs/YYYY/MM/{metar,taf}.jsonl (deduplicated)
- Open-Meteo point forecasts, one file per model run -> archive/openmeteo/YYYY/MM/<model>_<run>.json.gz
- AMPS: copy app/data/amps.json into archive/amps/YYYY/MM/<run>.json.gz when it is a new run
Usage: python harvest.py [obs] [openmeteo] [amps]   (default: all)
"""
import datetime as dt, gzip, json, pathlib, sys, urllib.request

HERE = pathlib.Path(__file__).parent
ARCHIVE = HERE / "archive"
UA = {"User-Agent": "antarctic-forecast-collator-harvest/0.1"}
LOCS = json.loads((HERE / "app/locations.json").read_text())

# Must match the models shown in app/index.html (id -> Open-Meteo metadata name)
OM_MODELS = {
    "ecmwf_ifs": "ecmwf_ifs",
    "ecmwf_aifs025_single": "ecmwf_aifs025_single",
    "gfs_global": "ncep_gfs025",
    "icon_global": "dwd_icon",
    "ukmo_global_deterministic_10km": "ukmo_global_deterministic_10km",
    "gem_global": "cmc_gem_gdps_15km",
    "meteofrance_arpege_world": "meteofrance_arpege_world025",
}
OM_HOURLY = ["temperature_2m", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "cloud_cover",
             "cloud_cover_low", "visibility", "precipitation", "snowfall", "pressure_msl"]
# Forecast soundings, kept so the cloud-base diagnosis in app/index.html can be calibrated against obs
PROFILE_LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500]
OM_HOURLY += ["surface_pressure", "dew_point_2m"] + [f"{k}_{l}hPa" for l in PROFILE_LEVELS
                                                      for k in ("temperature", "dew_point", "geopotential_height")]
OM_MODELS["ecmwf_ifs025"] = "ecmwf_ifs025"  # IFS 9 km has no pressure levels; the 0.25° run supplies its soundings
OM_HOURS = 48  # forecast horizon kept per run (~1 MB/day with soundings; move old months out of git if it grows)


def get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        body = r.read()
    return json.loads(body) if body.strip() else []


def month_dir(kind, when):
    d = ARCHIVE / kind / f"{when:%Y}" / f"{when:%m}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_gz(path, obj):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))


def append_unique(path, records, key):
    """Append records to a JSON-lines file, skipping keys already present."""
    seen = set()
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                seen.add(key(json.loads(line)))
    new = [r for r in records if key(r) not in seen]
    if new:
        with path.open("a") as f:
            for r in sorted(new, key=key):
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
    return len(new)


def harvest_obs():
    codes = [c for l in LOCS for c in [*([l["metar"]] if isinstance(l.get("metar"), str) else l.get("metar") or []), l.get("taf"), l["id"]]]
    stations = sorted({c for c in codes if c and len(c) == 4})
    ids = ",".join(stations)
    metars = get_json(f"https://aviationweather.gov/api/data/metar?ids={ids}&format=json&hours=30")
    tafs = get_json(f"https://aviationweather.gov/api/data/taf?ids={ids}&format=json")
    added = 0
    for m in metars:
        t = dt.datetime.fromtimestamp(m["obsTime"], dt.timezone.utc)
        added += append_unique(month_dir("obs", t) / "metar.jsonl", [m], lambda r: (r["icaoId"], r["obsTime"], r.get("rawOb")))
    for tf in tafs:
        t = dt.datetime.fromisoformat(tf["issueTime"].replace("Z", "+00:00"))
        added += append_unique(month_dir("obs", t) / "taf.jsonl", [tf], lambda r: (r["icaoId"], r["issueTime"], r.get("rawTAF")))
    print(f"obs: {len(metars)} METAR, {len(tafs)} TAF fetched for {ids}; {added} new records")


def harvest_openmeteo():
    lats = ",".join(str(l["lat"]) for l in LOCS)
    lons = ",".join(str(l["lon"]) for l in LOCS)
    for model, meta_name in OM_MODELS.items():
        try:
            meta = get_json(f"https://api.open-meteo.com/data/{meta_name}/static/meta.json")
            run = dt.datetime.fromtimestamp(meta["last_run_initialisation_time"], dt.timezone.utc)
        except Exception as e:
            print(f"openmeteo {model}: no run metadata ({e}); skipped")
            continue
        path = month_dir("openmeteo", run) / f"{model}_{run:%Y%m%d%H}.json.gz"
        if path.exists():
            continue
        # single-runs API returns exactly this model run (the normal endpoint blends runs for past hours)
        url = (f"https://single-runs-api.open-meteo.com/v1/forecast?latitude={lats}&longitude={lons}"
               f"&hourly={','.join(OM_HOURLY)}&models={model}&run={run:%Y-%m-%dT%H:00}"
               f"&forecast_hours={OM_HOURS + 1}&wind_speed_unit=kn&timezone=GMT")
        data = get_json(url)
        data = data if isinstance(data, list) else [data]
        out = {"model": model, "run": run.isoformat(), "fetched": dt.datetime.now(dt.timezone.utc).isoformat(),
               "locations": {l["id"]: {"grid_lat": d["latitude"], "grid_lon": d["longitude"], "hourly": d["hourly"]}
                             for l, d in zip(LOCS, data)}}
        write_gz(path, out)
        print(f"openmeteo {model}: archived run {run:%Y-%m-%d %HZ}")


def harvest_amps():
    src = HERE / "app/data/amps.json"
    if not src.exists():
        print("amps: no export to archive")
        return
    data = json.loads(src.read_text())
    run = dt.datetime.fromisoformat(data["run"])
    path = month_dir("amps", run) / f"amps_{run:%Y%m%d%H}.json.gz"
    if path.exists():
        print(f"amps: run {run:%Y-%m-%d %HZ} already archived")
        return
    write_gz(path, data)
    print(f"amps: archived run {run:%Y-%m-%d %HZ}")


if __name__ == "__main__":
    steps = sys.argv[1:] or ["obs", "openmeteo", "amps"]
    failed = []
    for step in steps:
        try:
            {"obs": harvest_obs, "openmeteo": harvest_openmeteo, "amps": harvest_amps}[step]()
        except Exception as e:  # keep going so one source being down doesn't lose the others
            print(f"{step}: FAILED {e}")
            failed.append(step)
    sys.exit(1 if failed else 0)
