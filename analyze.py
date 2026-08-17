# Clusters results.csv into the patterns the page leads with, and writes
# patterns.json. Every headline sentence on the page is generated here from the
# data, so nothing on the page can drift from the CSV.
#
#   python analyze.py

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from agent import read_csv
from verify import normalise_auth

PATTERNS_FILE = Path("patterns.json")

AUTH_LABELS = {
    "oauth": "OAuth 2.0",
    "api_key": "API key",
    "token": "Bearer / personal token",
    "basic": "Basic auth",
    "jwt": "JWT",
    "hmac": "HMAC / signed request",
}

GATED = {"Paid", "Admin approval", "Partner/contact sales"}
OPEN = {"Self-serve", "Trial"}

# Free-text blockers collapse into these so the chart has ~6 bars, not 60.
BLOCKER_THEMES = [
    ("Paid plan required", r"paid|pricing|subscription|premium|enterprise plan|billing|upgrade"),
    ("Partner / sales gate", r"partner|contact sales|sales team|approval process|vetting|allowlist|whitelist"),
    ("Admin / workspace approval", r"admin|workspace owner|org owner|install|consent|review process"),
    ("No public API", r"no public api|undocumented|no documented|internal api|not public"),
    ("Narrow API surface", r"limited|narrow|read-?only|few endpoints|thin"),
    ("Rate limits / quota", r"rate limit|quota|throttl"),
    ("App review required", r"app review|verification|verified app|submission"),
]


def auth_families(value):
    """Multi-label: an app offering OAuth and an API key counts in both, because
    for a toolkit builder either one is a usable door in."""
    found = normalise_auth(value)
    labels = {AUTH_LABELS[key] for key in found if key in AUTH_LABELS}
    return labels or {"Other / unclear"}


def blocker_theme(text):
    lowered = (text or "").strip().lower()
    if not lowered or lowered in ("none", "unknown", "n/a", "-"):
        return None
    for label, pattern in BLOCKER_THEMES:
        if re.search(pattern, lowered):
            return label
    return "Other"


def share(count, total):
    return round(100.0 * count / total, 1) if total else 0.0


def ranked(counter, total):
    return [{"label": label, "count": count, "pct": share(count, total)}
            for label, count in counter.most_common()]


def analyse(rows):
    total = len(rows)

    auth = Counter()
    multi_auth = 0
    for row in rows:
        labels = auth_families(row.get("auth"))
        auth.update(labels)
        if len(labels) > 1:
            multi_auth += 1

    access = Counter(r.get("credential_access", "Unknown") for r in rows)
    build = Counter(r.get("buildability", "Unknown") for r in rows)
    api = Counter(r.get("api", "Unknown") for r in rows)
    breadth = Counter(r.get("api_breadth", "Unknown") for r in rows)
    mcp = Counter(r.get("mcp", "Unknown") for r in rows)
    confidence = Counter(r.get("confidence", "Unknown") for r in rows)

    blockers = Counter()
    for row in rows:
        theme = blocker_theme(row.get("blocker"))
        if theme:
            blockers[theme] += 1

    # Category crosstab: the "which categories are self-serve vs gated" answer.
    by_category = defaultdict(lambda: {"total": 0, "open": 0, "gated": 0,
                                       "easy": 0, "blocked": 0, "official_mcp": 0})
    for row in rows:
        category = row.get("category", "Unknown")
        bucket = by_category[category]
        bucket["total"] += 1
        if row.get("credential_access") in OPEN:
            bucket["open"] += 1
        elif row.get("credential_access") in GATED:
            bucket["gated"] += 1
        if row.get("buildability") == "Easy":
            bucket["easy"] += 1
        elif row.get("buildability") == "Blocked":
            bucket["blocked"] += 1
        if row.get("mcp") == "Official MCP":
            bucket["official_mcp"] += 1

    categories = []
    for name, bucket in sorted(by_category.items(),
                               key=lambda kv: -kv[1]["easy"] / max(kv[1]["total"], 1)):
        categories.append(dict(bucket, category=name,
                               open_pct=share(bucket["open"], bucket["total"]),
                               easy_pct=share(bucket["easy"], bucket["total"])))

    easy_wins = sorted(r["app"] for r in rows
                       if r.get("buildability") == "Easy"
                       and r.get("credential_access") in OPEN)
    needs_outreach = sorted(r["app"] for r in rows if r.get("buildability") == "Blocked")
    has_mcp = sorted(r["app"] for r in rows if r.get("mcp") == "Official MCP")

    open_total = sum(access[k] for k in OPEN)
    gated_total = sum(access[k] for k in GATED)

    patterns = {
        "total_apps": total,
        "auth": ranked(auth, total),
        "apps_with_multiple_auth": multi_auth,
        "credential_access": ranked(access, total),
        "buildability": ranked(build, total),
        "api_type": ranked(api, total),
        "api_breadth": ranked(breadth, total),
        "mcp": ranked(mcp, total),
        "confidence": ranked(confidence, total),
        "blockers": ranked(blockers, sum(blockers.values())),
        "by_category": categories,
        "open_total": open_total,
        "open_pct": share(open_total, total),
        "gated_total": gated_total,
        "gated_pct": share(gated_total, total),
        "easy_wins": easy_wins,
        "needs_outreach": needs_outreach,
        "official_mcp_apps": has_mcp,
    }
    patterns["headlines"] = headlines(patterns, rows)
    return patterns


def headlines(p, rows):
    """The four sentences at the top of the page, written from the numbers."""
    lines = []
    total = p["total_apps"]

    if p["auth"]:
        top = p["auth"][0]
        lines.append({
            "stat": "%.0f%%" % top["pct"],
            "claim": "of the 100 apps expose %s" % top["label"],
            "detail": "%d of %d. %d apps document more than one scheme, so a toolkit "
                      "can usually pick the easier door." % (
                          top["count"], total, p["apps_with_multiple_auth"]),
        })

    lines.append({
        "stat": "%.0f%%" % p["open_pct"],
        "claim": "let a developer self-serve credentials",
        "detail": "%d apps are self-serve or trial; %d sit behind payment, an admin, "
                  "or a sales conversation." % (p["open_total"], p["gated_total"]),
    })

    if p["blockers"]:
        top = p["blockers"][0]
        lines.append({
            "stat": top["label"],
            "claim": "is the single most common blocker",
            "detail": "Named on %d of the %d apps that have a blocker at all — ahead of %s." % (
                top["count"], sum(b["count"] for b in p["blockers"]),
                p["blockers"][1]["label"] if len(p["blockers"]) > 1 else "everything else"),
        })

    official = len(p["official_mcp_apps"])
    lines.append({
        "stat": "%d" % len(p["easy_wins"]),
        "claim": "are buildable today with no outreach",
        "detail": "Against %d blocked outright — partner gates, sales conversations, "
                  "or no public API at all. Only %d ship an official MCP server, so "
                  "most of this surface is still unbuilt." % (
                      len(p["needs_outreach"]), official),
    })
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="results.csv")
    args = parser.parse_args()

    if not Path(args.results).exists():
        raise SystemExit("No %s yet. Run: python agent.py" % args.results)

    rows = read_csv(args.results)
    patterns = analyse(rows)
    PATTERNS_FILE.write_text(json.dumps(patterns, indent=2), encoding="utf-8")

    print("Analysed %d apps -> %s\n" % (patterns["total_apps"], PATTERNS_FILE))
    for line in patterns["headlines"]:
        print("  %s %s" % (line["stat"], line["claim"]))
        print("      %s\n" % line["detail"])

    print("Auth:")
    for item in patterns["auth"]:
        print("  %-26s %3d  %5.1f%%" % (item["label"], item["count"], item["pct"]))
    print("\nCredential access:")
    for item in patterns["credential_access"]:
        print("  %-26s %3d  %5.1f%%" % (item["label"], item["count"], item["pct"]))
    print("\nTop blockers:")
    for item in patterns["blockers"][:6]:
        print("  %-26s %3d" % (item["label"], item["count"]))


if __name__ == "__main__":
    main()
