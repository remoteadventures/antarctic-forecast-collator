# Antarctic Forecast Collator (HTML prototype)

Side-by-side model forecasts for Antarctic sites: now + next 24 h, with latest METAR/TAF.

## Run

```bash
python3 serve.py            # http://localhost:8765
```

Refresh AMPS (about 5 min; needs the venv with `eccodes` + `numpy`):

```bash
python amps_export.py latest   # writes app/data/amps.json for every site in app/locations.json
```

## Pieces

| File | Role |
|---|---|
| `app/index.html` | The page. Fetches Open-Meteo directly from the browser. |
| `app/locations.json` | Dropdown sites, with the METAR/TAF station for each. |
| `app/data/amps.json` | AMPS point forecasts, from `amps_export.py`. |
| `serve.py` | Static server + `/api/obs` proxy to aviationweather.gov (no browser CORS). |
| `amps_export.py` / `amps_point.py` | Pull AMPS GRIB records by byte range from UCAR's real-time feed. |
| `amps_backfill.py` | Exports any recent AMPS runs missing from `archive/amps` (UCAR keeps ~7 days). |
| `amps_history.py` | Builds `app/data/amps_history.json` (last 96 h of runs) for the run-to-run trend. |

## Sources

- **AMPS** (Polar WRF, UCAR): 0.85 km d5 for Ross Island, d3 elsewhere; 00Z/12Z runs. No gust or cloud cover fields in the GRIB; visibility is estimated from falling snow only.
- **Open-Meteo**: ECMWF IFS 9 km, ECMWF AIFS, GFS, ICON, UKMO 10 km, GEM, ARPEGE. Visibility only from ECMWF IFS, GFS and UKMO.
- **Observations**: METAR/TAF from aviationweather.gov. NZFX has a TAF but its METARs aren't on the feed, so NZCM is used.
- **BLSN**: derived risk (Li & Pomeroy 1997 threshold), not a model output.

## Scheduled harvest (GitHub Actions)

`.github/workflows/harvest.yml` runs every 3 hours (and on demand from the Actions tab):

1. `amps_export.py latest --if-new`: refreshes `app/data/amps.json` when a new, fully uploaded AMPS run exists.
2. `harvest.py`: appends to the verification archive.
3. `amps_backfill.py 2`: archives up to two missing AMPS runs, if any.
4. `amps_history.py`: rebuilds the run-to-run trend data.
5. Commits anything new.

| Path | Contents |
|---|---|
| `archive/obs/YYYY/MM/metar.jsonl`, `taf.jsonl` | Every METAR/TAF for the stations in `locations.json`, deduplicated |
| `archive/openmeteo/YYYY/MM/<model>_<run>.json.gz` | One file per model run: 48 h incl. soundings (1000–500 hPa) from Open-Meteo's single-runs API, all sites |
| `archive/amps/YYYY/MM/amps_<run>.json.gz` | Each AMPS export |

Run it locally with `python harvest.py` (or `python harvest.py obs` for one step).

## iPhone / iPad app (`ios/`)

A SwiftUI app hosting the same `app/` page, so web and app share one codebase.

- `ios/ForecastCollator.xcodeproj`: open in Xcode, pick a simulator or your device, and Run.
- `AppSchemeHandler.swift` serves the bundled page and handles every network request natively:
  - METAR/TAF are fetched directly, so no proxy is needed.
  - AMPS files come from this repo on GitHub, falling back to the copy bundled at build time.
  - Every response is saved on the device. The app opens instantly on the last saved data ("Saved data · updating…") and works offline ("Offline · showing data saved …").
- Pull down to refresh. The app also refreshes when you return to it after 10 minutes.

Installing on your own iPhone/iPad with a free Apple ID: in Xcode, open the project, select the ForecastCollator target → Signing & Capabilities → Team: your Apple ID (Xcode → Settings → Accounts to add it). Plug in the device, select it, and press Run. On the device, trust the developer under Settings → General → VPN & Device Management. Free-account installs expire after 7 days; run from Xcode again to renew.
