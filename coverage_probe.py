"""One-off: how much of the 100 can the free docs fetcher actually reach?"""
import csv, time, json
from concurrent.futures import ThreadPoolExecutor
import fetcher

apps = list(csv.DictReader(open("apps.csv", encoding="utf-8")))
start = time.time()

def one(app):
    try:
        return app, fetcher.gather(app["website"], max_pages=5,
                                   docs_hint=app.get("docs_hint") or None), ""
    except Exception as e:
        return app, [], str(e)[:60]

results = []
with ThreadPoolExecutor(max_workers=12) as pool:
    for app, pages, err in pool.map(one, apps):
        results.append((app, pages, err))
        print("  %-28s %d pages" % (app["app_name"][:28], len(pages)), flush=True)

elapsed = time.time() - start
hits = [r for r in results if r[1]]
chars = sum(len(t) for _, pgs, _ in results for _, t in pgs)
print()
print("Fetched docs for %d/%d apps in %.1f s" % (len(hits), len(apps), elapsed))
print("Evidence: %.2f MB over %d pages" % (chars / 1e6, sum(len(p) for _, p, _ in results)))
print()
misses = [(a["app_name"], a["website"], e) for a, p, e in results if not p]
print("MISSES (%d):" % len(misses))
for name, site, err in misses:
    print("  %-26s %-42s %s" % (name[:26], site[:42], err))

json.dump({a["app_name"]: [u for u, _ in p] for a, p, _ in results},
          open("fetch_coverage.json", "w", encoding="utf-8"), indent=1)
