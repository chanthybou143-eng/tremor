"""Pull Unit 1 raw readings per NEM day (00:00-24:00 UTC+10) from the public /api/history endpoint.
Same paging as fetch_unit1.py (from/to in seconds, after_id, resolution=raw, 10k rows/page).
Writes unit1_raw/unit1_nem_<day>.json; a day still in progress is fetched up to now and named *_partial."""
import json, sys, time, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://tremorgrid.pythonanywhere.com"
US = 1_000_000
NEM = timezone(timedelta(hours=10))


def get(url):
    for attempt in range(8):
        try:
            with urllib.request.urlopen(url, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(10)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(10)
    raise RuntimeError("giving up after repeated errors")


def fetch_range(lo, hi):
    rows, url = [], f"{BASE}/api/history?unit=unit-1&from={lo}&to={hi - 1e-6}&limit=10000&resolution=raw"
    while True:
        page = get(url)
        assert page["resolution"] == "raw", page.get("resolution")
        rows += page["readings"]
        if not page["truncated"]:
            return rows
        url = (f"{BASE}/api/history?unit=unit-1&from={page['next_from_us'] / US}&to={hi - 1e-6}"
               f"&after_id={page['next_after_id']}&limit=10000&resolution=raw")
        time.sleep(2.5)


if __name__ == "__main__":
    for day in sys.argv[1:]:
        lo = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=NEM).timestamp()
        hi = lo + 86400
        partial = hi > time.time()
        rows = fetch_range(lo, min(hi, time.time()))
        name = f"unit1_raw/unit1_nem_{day}{'_partial' if partial else ''}.json"
        with open(name, "w") as f:
            json.dump(rows, f)
        print(f"{day}: {len(rows)} rows -> {name}", flush=True)
