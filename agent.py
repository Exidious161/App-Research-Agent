# Researches each app in apps.csv with Gemini + Google Search, writes results.csv.
# python agent.py --limit 5

import argparse
import csv
import json
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

import providers

COLUMNS = ["app", "category", "description", "auth", "credential_access", "api",
           "api_breadth", "mcp", "buildability", "blocker", "evidence_url",
           "confidence"]

# Anything the model returns that isn't in these lists becomes "Unknown".
CHOICES = {
    "credential_access": ["Self-serve", "Trial", "Paid", "Admin approval",
                          "Partner/contact sales", "Unknown"],
    "api": ["REST", "GraphQL", "REST + GraphQL", "SDK", "CLI", "Other", "Unknown"],
    "api_breadth": ["Broad", "Moderate", "Limited", "Unknown"],
    "mcp": ["Official MCP", "Third-party MCP", "No MCP found", "Unknown"],
    "buildability": ["Easy", "Conditional", "Blocked", "Unknown"],
    "confidence": ["High", "Medium", "Low"],
}

# Tried in order when the configured model isn't available to this key. The
# grounded-search models get renamed and retired often enough that pinning a
# single name is how the run dies three months later.
SYSTEM = """You are researching software APIs. Check the official developer
documentation and report what it says. Do not answer from memory.

- Search before answering. Prefer official developer docs, then the API
  reference, then the auth docs, then any MCP docs.
- If the docs don't confirm something, write "Unknown". Don't guess.
- Don't invent a blocker. If there isn't one, write "None".
- A community GitHub repo is not an official MCP server. Only say "Official MCP"
  if the vendor publishes or links it themselves.
- Reply with one JSON object, nothing else."""

PROMPT = """App: {app_name}
Category hint: {category}
Website: {website}

Work out whether a developer could build agent tools on this API.

category: product category, correct the hint if the docs disagree
description: one line, 15 words max
auth: what the API docs describe, e.g. OAuth 2.0, API key, Bearer token, Unknown
credential_access: one of "Self-serve", "Trial", "Paid", "Admin approval",
  "Partner/contact sales", "Unknown". Judge the normal path for a new developer.
  If an app has to be installed or approved by an org or workspace admin then
  it's Admin approval, even when signup is free.
api: REST / GraphQL / REST + GraphQL / SDK / CLI / Other / Unknown
api_breadth: Broad (many objects across the product) / Moderate (core objects) /
  Limited (few endpoints or read-only) / Unknown
mcp: Official MCP / Third-party MCP / No MCP found / Unknown
buildability:
  Easy = documented API, credentials obtainable, enough functionality to be useful
  Conditional = API exists but real restrictions (paid, admin approval,
    narrow permissions, thin functionality)
  Blocked = partner-only, contact sales only, no public API, other major blocker
  Unknown = not enough reliable information
blocker: the main obstacle in a few words, or None
evidence_url: the best official docs URL you actually opened. Must come from
  your search results.
confidence: High = official docs confirmed it, Medium = partly confirmed or
  secondary source, Low = little evidence
notes: a sentence on how you decided, naming the source

{{"category":"","description":"","auth":"","credential_access":"","api":"",
"api_breadth":"","mcp":"","buildability":"","blocker":"","evidence_url":"",
"confidence":"","notes":""}}"""


def domain(url):
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def parse_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}


def match_choice(value, options):
    value = (value or "").strip()
    for option in options:
        if value.lower() == option.lower():
            return option
    return "Unknown"


def build_row(data, app, urls):
    row = {
        "app": app["app_name"],
        "category": (data.get("category") or app.get("category") or "Unknown").strip(),
        "description": (data.get("description") or "Unknown").strip(),
        "auth": (data.get("auth") or "Unknown").strip(),
        "blocker": (data.get("blocker") or "Unknown").strip(),
        "evidence_url": (data.get("evidence_url") or "").strip(),
    }
    for field, options in CHOICES.items():
        row[field] = match_choice(data.get(field), options)

    if not row["evidence_url"].startswith("http"):
        row["evidence_url"] = "Unknown"
        row["confidence"] = "Low"
    elif domain(row["evidence_url"]) not in {domain(u) for u in urls} \
            and row["confidence"] == "High":
        # Nothing from that domain came back in the citations, so the URL might
        # be from memory. Matched on domain, not exact string, because search
        # citations often point at a redirector rather than the page itself.
        row["confidence"] = "Medium"

    if row["buildability"] == "Easy" and row["blocker"] in ("", "Unknown"):
        row["blocker"] = "None"

    return {c: row[c] for c in COLUMNS}


def blank_row(app):
    """Used when both attempts fail, so one bad app doesn't stop the run."""
    row = {c: "Unknown" for c in COLUMNS}
    row["app"] = app["app_name"]
    row["category"] = app.get("category", "Unknown")
    row["confidence"] = "Low"
    return row


def research(agent, app):
    prompt = PROMPT.format(app_name=app["app_name"],
                           category=app.get("category", ""),
                           website=app.get("website", ""))
    for attempt in (1, 2):
        try:
            text, urls = agent.research(SYSTEM, prompt)
            data = parse_json(text)
            if not data:
                raise ValueError("no JSON in reply")
            row = build_row(data, app, urls)
            extra = {
                "notes": (data.get("notes") or "").strip(),
                "sources_seen": urls,
                "researched_at": datetime.now().isoformat(timespec="seconds"),
                "model": agent.model,
                "provider": agent.name,
            }
            return row, extra
        except Exception as error:
            print("    attempt %d failed: %s" % (attempt, error))
            if attempt == 2:
                return blank_row(app), {"notes": "failed: %s" % error,
                                        "sources_seen": [], "researched_at": "",
                                        "model": agent.model, "provider": agent.name}
            time.sleep(5)


def check(agent, app):
    """Smoke test: does the key work, does search run, does JSON come back?"""
    print("Provider : %s" % agent.name)
    print("Model    : %s" % agent.model)
    print("Test app : %s" % app["app_name"])
    print()
    row, extra = research(agent, app)
    urls = extra["sources_seen"]

    print("Searched : %d cited URLs%s" % (
        len(urls), "" if urls else
        "   <-- search returned nothing, answers would come from memory"))
    for url in urls[:5]:
        print("           %s" % url)
    if row["auth"] == "Unknown" and row["confidence"] == "Low":
        print()
        print("Row came back blank. Reason: %s" % extra["notes"])
        raise SystemExit(1)
    print()
    print("Row: %s" % json.dumps(row, indent=2))
    print()
    print("Working. Run `python agent.py` for the full 100.")


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="apps.csv")
    parser.add_argument("--out", default="results.csv")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument("--provider", choices=sorted(providers.REGISTRY),
                        help="Default: whichever API key is set in .env")
    parser.add_argument("--model", help="Override the model for that provider")
    parser.add_argument("--show-prompt", action="store_true")
    parser.add_argument("--check", action="store_true",
                        help="Research one app and print the result, then exit.")
    args = parser.parse_args()

    load_dotenv()
    apps = read_csv(args.input)
    if args.limit:
        apps = apps[:args.limit]

    if args.show_prompt:
        print(SYSTEM)
        print("-" * 60)
        print(PROMPT.format(app_name=apps[0]["app_name"],
                            category=apps[0].get("category", ""),
                            website=apps[0].get("website", "")))
        return

    try:
        agent = providers.build(args.provider or os.getenv("PROVIDER"),
                                args.model or os.getenv("MODEL"))
    except providers.ProviderError as error:
        raise SystemExit(str(error))

    if args.check:
        return check(agent, apps[0])

    json_path = Path(args.out).with_suffix(".json")

    # Re-running picks up where it left off instead of paying for the same
    # apps twice.
    rows, records = [], []
    if Path(args.out).exists():
        rows = read_csv(args.out)
        if json_path.exists():
            records = json.loads(json_path.read_text(encoding="utf-8"))
        print("Found %d apps already done." % len(rows))

    done = {r["app"] for r in rows}
    todo = [a for a in apps if a["app_name"] not in done]
    print("Researching %d apps with %s (%s)" % (len(todo), agent.model, agent.name))
    print()

    for i, app in enumerate(todo, start=1):
        print("[%d/%d] %s" % (i, len(todo), app["app_name"]))
        row, extra = research(agent, app)
        rows.append(row)
        records.append(dict(row, **extra))

        print("    %s | %s | %s | %s | %s | %s" % (
            row["auth"], row["credential_access"], row["api"], row["mcp"],
            row["buildability"], row["confidence"]))
        if extra["notes"]:
            print("    why: %s" % extra["notes"])

        # Save every time. The run takes a while and losing it would hurt.
        write_csv(rows, args.out)
        json_path.write_text(json.dumps(records, indent=2), encoding="utf-8")
        time.sleep(args.sleep)

    print()
    print("Done. %d rows in %s" % (len(rows), args.out))


if __name__ == "__main__":
    main()
