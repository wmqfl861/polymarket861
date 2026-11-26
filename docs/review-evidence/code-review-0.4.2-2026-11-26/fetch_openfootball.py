"""Download the pinned OpenFootball files (five leagues, 2010/11 to 2026/27) into a directory."""
import http.client, pathlib, sys, time, urllib.request, urllib.error
COMMIT = "e6744429ee395bc86f247348c6184bb08d4eb361"
out = pathlib.Path(sys.argv[1])
missing, got = [], 0
for y in range(2010, 2027):
    season = f"{y}-{(y + 1) % 100:02d}"
    for league in ("en", "es", "de", "it", "fr"):
        url = f"https://raw.githubusercontent.com/openfootball/football.json/{COMMIT}/{season}/{league}.1.json"
        target = out / season / f"{league}.1.json"
        if target.exists():
            got += 1
            continue
        raw = None
        for attempt in range(5):
            try:
                raw = urllib.request.urlopen(url, timeout=60).read()
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    missing.append((season, league, exc.code))
                    break
                time.sleep(2)
            except (OSError, http.client.HTTPException):
                time.sleep(2)
        if raw is None:
            continue
        (out / season).mkdir(parents=True, exist_ok=True)
        (out / season / f"{league}.1.json").write_bytes(raw)
        got += 1
print("downloaded", got, "missing", missing)
