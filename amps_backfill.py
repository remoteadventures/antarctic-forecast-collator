"""Archive any recent AMPS runs missing from archive/amps (UCAR keeps about a week of runs).

Exports each missing, fully uploaded run from the last LOOKBACK_H hours straight into the archive,
at most MAX_PER_CALL per invocation so a scheduled job stays short.
Usage: python amps_backfill.py [MAX_PER_CALL=2]
"""
import datetime as dt, pathlib, re, subprocess, sys
from amps_point import BASE, listing
from amps_export import MIN_D5_HOURS

HERE = pathlib.Path(__file__).parent
LOOKBACK_H = 108  # 96 h of history plus the 12 h trend window


def main():
    max_runs = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    now = dt.datetime.now(dt.timezone.utc)
    runs = sorted((d for d in listing(BASE + "/") if re.fullmatch(r"\d{10}", d)), reverse=True)
    done = 0
    for run in runs:
        t = dt.datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
        if now - t > dt.timedelta(hours=LOOKBACK_H):
            break
        out = HERE / f"archive/amps/{run[:4]}/{run[4:6]}/amps_{run}.json.gz"
        if out.exists():
            continue
        n = sum(1 for f in listing(f"{BASE}/{run}/") if re.fullmatch(rf"{run}_WRF_d5_f\d+\.grb", f))
        if n < MIN_D5_HOURS:
            print(f"{run}: still uploading ({n} d5 hours), skipped")
            continue
        print(f"{run}: exporting to archive")
        subprocess.run([sys.executable, str(HERE / "amps_export.py"), run, f"--out={out}"], check=True)
        done += 1
        if done >= max_runs:
            break
    print(f"backfill: {done} run(s) archived")


if __name__ == "__main__":
    main()
