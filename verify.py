# Verification loops over results.csv. Three checks that fail in different ways:
#
#   python verify.py --urls          every evidence_url actually resolves (no API key)
#   python verify.py --recheck 25    blind independent second pass, then adjudicate
#   python verify.py --template 15   write human_review.csv to fill in by hand
#   python verify.py --score         read the filled-in sheet, compute accuracy
#
# Everything lands in verification.json, which build_page.py renders.

import argparse
import csv
import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from dotenv import load_dotenv

import providers

from agent import (CHOICES, domain, match_choice,
                   parse_json, read_csv, write_csv, COLUMNS)

# The fields accuracy is actually measured on. category/description are prose and
# two correct answers can be worded differently, so they aren't graded.
GRADED = ["auth", "credential_access", "api", "mcp", "buildability"]

VERIFY_FILE = Path("verification.json")
HUMAN_FILE = Path("human_review.csv")

# Deliberately worded differently from agent.py's prompt, and it never sees pass 1.
# If it saw the first answer it would mostly agree with it, and agreement would
# stop being evidence of anything.
RECHECK_SYSTEM = """You are auditing whether an API can be automated against.
Search the official developer documentation before answering. Never answer from
memory. If the docs do not say, answer "Unknown". Reply with one JSON object."""

RECHECK_PROMPT = """Look up the developer documentation for: {app_name} ({website})

Answer only from what the documentation states:

auth: the authentication scheme the API reference documents
credential_access: how a brand-new developer obtains working credentials. One of
  "Self-serve", "Trial", "Paid", "Admin approval", "Partner/contact sales", "Unknown".
  Requiring an org or workspace admin to install or approve the app counts as
  "Admin approval" even if the account itself is free.
api: REST / GraphQL / REST + GraphQL / SDK / CLI / Other / Unknown
mcp: does the vendor themselves publish a Model Context Protocol server?
  "Official MCP" only if the vendor publishes or links it. A community repo is
  "Third-party MCP". Otherwise "No MCP found" or "Unknown".
buildability: Easy / Conditional / Blocked / Unknown — could a developer ship
  agent tools against this today, given credential access and API breadth
evidence_url: the documentation page you relied on

{{"auth":"","credential_access":"","api":"","mcp":"","buildability":"","evidence_url":""}}"""

ADJUDICATE_SYSTEM = """You are settling a disagreement between two research passes.
Search the official documentation yourself and decide which is right. You may
conclude both are wrong. Reply with one JSON object."""

ADJUDICATE_PROMPT = """App: {app_name} ({website})
Field in dispute: {field}

Pass 1 answered: {value_a}
Pass 2 answered: {value_b}

Allowed values: {allowed}

Search the official developer docs and decide the correct value. Do not split the
difference to be agreeable; pick the one the documentation supports, or a third
value if both are wrong.

{{"correct_value":"","evidence_url":"","reason":""}}"""


def normalise_auth(value):
    """Auth is free text, so "OAuth 2.0" and "OAuth2" must not count as a miss.

    Reduces a string to the set of schemes it mentions and compares those.
    """
    text = (value or "").lower()
    found = set()
    if re.search(r"oauth", text):
        found.add("oauth")
    if re.search(r"api[\s_-]?key", text):
        found.add("api_key")
    if re.search(r"bearer|personal access token|\bpat\b|access token", text):
        found.add("token")
    if re.search(r"basic auth|\bbasic\b", text):
        found.add("basic")
    if re.search(r"\bjwt\b", text):
        found.add("jwt")
    if re.search(r"hmac|signature|signed request", text):
        found.add("hmac")
    return found or {text.strip()}


def agrees(field, a, b):
    if field == "auth":
        left, right = normalise_auth(a), normalise_auth(b)
        # Overlap rather than equality: one pass may list a scheme the other
        # omitted without either being wrong.
        return bool(left & right)
    return (a or "").strip().lower() == (b or "").strip().lower()


# ---------------------------------------------------------------- URL liveness

def check_url(url):
    """Does the cited page exist, and is it plausibly about this app?

    Runs with no API key, so it is the one loop that always works. Catches the
    failure mode that matters most: a confident answer citing a URL the model
    made up.
    """
    if not url or not url.startswith("http"):
        return {"url": url, "ok": False, "status": None, "error": "no url"}
    try:
        response = requests.get(url, timeout=20, allow_redirects=True,
                                headers={"User-Agent": "Mozilla/5.0 (research-agent)"})
        return {
            "url": url,
            "ok": response.status_code < 400,
            "status": response.status_code,
            # A redirect off the cited domain usually means the deep link is dead
            # and you landed on a marketing page.
            "redirected_off_domain": domain(response.url) != domain(url),
            "final_url": response.url,
            "error": None,
        }
    except requests.RequestException as error:
        return {"url": url, "ok": False, "status": None,
                "error": type(error).__name__}


def run_url_check(rows):
    urls = [r.get("evidence_url", "") for r in rows]
    print("Checking %d evidence URLs...\n" % len(urls))

    with ThreadPoolExecutor(max_workers=8) as pool:
        checks = list(pool.map(check_url, urls))

    results = []
    for row, check in zip(rows, checks):
        results.append(dict(check, app=row["app"]))
        flag = "ok " if check["ok"] else "DEAD"
        note = ""
        if check["ok"] and check.get("redirected_off_domain"):
            flag, note = "WARN", "  -> redirected to %s" % check["final_url"]
        print("  %s  %-28s %s%s" % (flag, row["app"][:28], check["url"][:60], note))

    live = sum(1 for c in checks if c["ok"])
    off = sum(1 for c in checks if c["ok"] and c.get("redirected_off_domain"))
    print("\n%d/%d resolve, %d redirect off-domain." % (live, len(checks), off))
    return {
        "checked": len(checks),
        "live": live,
        "dead": len(checks) - live,
        "redirected_off_domain": off,
        "results": results,
    }


# ------------------------------------------------------------- blind re-search

def recheck_one(agent, app):
    text, urls = agent.research(
        RECHECK_SYSTEM,
        RECHECK_PROMPT.format(app_name=app["app"], website=app.get("website", "")))
    data = parse_json(text)
    out = {"evidence_url": (data.get("evidence_url") or "").strip()}
    for field in GRADED:
        if field == "auth":
            out[field] = (data.get("auth") or "Unknown").strip()
        else:
            out[field] = match_choice(data.get(field), CHOICES[field])
    return out, urls


def adjudicate(agent, app, field, value_a, value_b):
    allowed = ", ".join(CHOICES[field]) if field in CHOICES else "free text"
    text, _ = agent.research(
        ADJUDICATE_SYSTEM,
        ADJUDICATE_PROMPT.format(app_name=app["app"], website=app.get("website", ""),
                                 field=field, value_a=value_a, value_b=value_b,
                                 allowed=allowed))
    data = parse_json(text)
    value = (data.get("correct_value") or "").strip()
    if field in CHOICES:
        value = match_choice(value, CHOICES[field])
    return {
        "field": field,
        "pass1": value_a,
        "pass2": value_b,
        "resolved": value or "Unknown",
        "evidence_url": (data.get("evidence_url") or "").strip(),
        "reason": (data.get("reason") or "").strip(),
    }


def run_recheck(rows, sample_size, apply_fixes):
    load_dotenv()
    try:
        agent = providers.build(os.getenv("PROVIDER"), os.getenv("MODEL"))
    except providers.ProviderError as error:
        raise SystemExit(str(error))

    inputs = {a["app_name"]: a for a in read_csv("apps.csv")}
    random.seed(7)  # same sample every run, so the numbers are reproducible
    sample = random.sample(rows, min(sample_size, len(rows)))

    print("Blind second pass over %d apps with %s\n" % (len(sample), model))

    comparisons, corrections = [], []
    agree_count = {f: 0 for f in GRADED}

    for i, row in enumerate(sample, start=1):
        app = dict(row, website=inputs.get(row["app"], {}).get("website", ""))
        print("[%d/%d] %s" % (i, len(sample), row["app"]))
        try:
            second, _ = recheck_one(agent, app)
        except Exception as error:
            print("    recheck failed: %s" % error)
            continue

        record = {"app": row["app"], "fields": {}}
        for field in GRADED:
            ok = agrees(field, row.get(field), second.get(field))
            agree_count[field] += ok
            record["fields"][field] = {
                "pass1": row.get(field), "pass2": second.get(field), "agree": ok,
            }
            print("    %-18s %s  %s | %s" % (
                field, "==" if ok else "!=", row.get(field), second.get(field)))

        disputed = [f for f in GRADED if not record["fields"][f]["agree"]]
        record["disputed"] = disputed

        # Only disagreements cost a third call. Agreement is already two
        # independent passes landing on the same answer.
        for field in disputed:
            try:
                verdict = adjudicate(agent, app, field,
                                     row.get(field), second.get(field))
            except Exception as error:
                print("    adjudication failed on %s: %s" % (field, error))
                continue
            verdict["app"] = row["app"]
            # Which pass the referee sided with, or neither.
            if agrees(field, verdict["resolved"], row.get(field)):
                verdict["winner"] = "pass1"
            elif agrees(field, verdict["resolved"], second.get(field)):
                verdict["winner"] = "pass2"
            else:
                verdict["winner"] = "neither"
            corrections.append(verdict)
            print("    -> %s resolved to %s (%s)" % (field, verdict["resolved"],
                                                     verdict["winner"]))

        comparisons.append(record)
        time.sleep(2)

    total = len(comparisons)
    rates = {f: round(100.0 * agree_count[f] / total, 1) for f in GRADED} if total else {}
    overall = round(sum(agree_count.values()) / (total * len(GRADED)) * 100, 1) if total else 0

    if apply_fixes and corrections:
        # Keep pass 1 intact so the page can show accuracy before and after.
        if not Path("results_pass1.csv").exists():
            write_csv(rows, "results_pass1.csv")
            print("\nSaved pass 1 to results_pass1.csv")
        by_app = {r["app"]: r for r in rows}
        changed = 0
        for fix in corrections:
            if fix["winner"] == "pass1" or fix["resolved"] == "Unknown":
                continue
            row = by_app.get(fix["app"])
            if row and row.get(fix["field"]) != fix["resolved"]:
                row[fix["field"]] = fix["resolved"]
                changed += 1
        write_csv(rows, "results.csv")
        print("Applied %d corrections to results.csv" % changed)

    print("\nBlind agreement: %.1f%% across %d apps" % (overall, total))
    return {
        "sample_size": total,
        "agreement_by_field": rates,
        "agreement_overall": overall,
        "comparisons": comparisons,
        "corrections": corrections,
        "applied": bool(apply_fixes),
    }


# --------------------------------------------------------------- human review

def write_template(rows, size):
    """A stratified sample, so the hand-check covers every category rather than
    ten CRMs."""
    random.seed(7)
    by_category = {}
    for row in rows:
        by_category.setdefault(row.get("category", "Unknown"), []).append(row)

    picked, categories = [], sorted(by_category)
    while len(picked) < min(size, len(rows)):
        added = False
        for category in categories:
            pool = [r for r in by_category[category] if r not in picked]
            if pool and len(picked) < size:
                picked.append(random.choice(pool))
                added = True
        if not added:
            break

    header = ["app", "evidence_url"]
    for field in GRADED:
        header += ["agent_" + field, "human_" + field]
    header.append("notes")

    with open(HUMAN_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in picked:
            line = [row["app"], row.get("evidence_url", "")]
            for field in GRADED:
                line += [row.get(field, ""), ""]      # human_ column left blank
            line.append("")
            writer.writerow(line)

    print("Wrote %s with %d apps across %d categories." % (
        HUMAN_FILE, len(picked), len({r.get('category') for r in picked})))
    print("Open each evidence_url, fill the human_* columns, then: python verify.py --score")


def score_human(rows):
    if not HUMAN_FILE.exists():
        raise SystemExit("No %s. Run --template first." % HUMAN_FILE)
    reviewed = read_csv(HUMAN_FILE)
    pass1 = {r["app"]: r for r in read_csv("results_pass1.csv")} \
        if Path("results_pass1.csv").exists() else {}

    hits, misses = [], []
    scored = {f: [0, 0] for f in GRADED}      # [correct, judged]
    before = {f: [0, 0] for f in GRADED}      # same, against pass 1

    for row in reviewed:
        for field in GRADED:
            human = (row.get("human_" + field) or "").strip()
            if not human:
                continue                       # not reviewed yet, don't count it
            agent = (row.get("agent_" + field) or "").strip()
            ok = agrees(field, agent, human)
            scored[field][1] += 1
            scored[field][0] += ok
            (hits if ok else misses).append({
                "app": row["app"], "field": field, "agent": agent,
                "human": human, "notes": (row.get("notes") or "").strip(),
                "evidence_url": row.get("evidence_url", ""),
            })
            original = pass1.get(row["app"], {}).get(field)
            if original is not None:
                before[field][1] += 1
                before[field][0] += agrees(field, original, human)

    judged = sum(v[1] for v in scored.values())
    correct = sum(v[0] for v in scored.values())
    if not judged:
        raise SystemExit("No human_* columns filled in yet.")

    accuracy = round(100.0 * correct / judged, 1)
    result = {
        "apps_reviewed": len({r["app"] for r in reviewed
                              if any((r.get("human_" + f) or "").strip() for f in GRADED)}),
        "fields_judged": judged,
        "correct": correct,
        "accuracy": accuracy,
        "by_field": {f: round(100.0 * c / n, 1) for f, (c, n) in scored.items() if n},
        "hits": hits,
        "misses": misses,
    }

    judged_before = sum(v[1] for v in before.values())
    if judged_before:
        result["accuracy_before_corrections"] = round(
            100.0 * sum(v[0] for v in before.values()) / judged_before, 1)

    print("Human-checked accuracy: %.1f%% (%d/%d fields over %d apps)" % (
        accuracy, correct, judged, result["apps_reviewed"]))
    if "accuracy_before_corrections" in result:
        print("Pass 1 alone was %.1f%%" % result["accuracy_before_corrections"])
    for field, rate in result["by_field"].items():
        print("  %-18s %.1f%%" % (field, rate))
    if misses:
        print("\n%d misses:" % len(misses))
        for miss in misses:
            print("  %s / %s: agent said %r, actually %r" % (
                miss["app"], miss["field"], miss["agent"], miss["human"]))
    return result


def load_verification():
    if VERIFY_FILE.exists():
        return json.loads(VERIFY_FILE.read_text(encoding="utf-8"))
    return {}


def save_verification(section, data):
    report = load_verification()
    report[section] = data
    VERIFY_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("\nSaved '%s' to %s" % (section, VERIFY_FILE))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="results.csv")
    parser.add_argument("--urls", action="store_true",
                        help="check every evidence_url resolves (no API key needed)")
    parser.add_argument("--recheck", type=int, metavar="N",
                        help="blind second pass over N apps, adjudicating disagreements")
    parser.add_argument("--apply", action="store_true",
                        help="with --recheck, write the adjudicated corrections back")
    parser.add_argument("--template", type=int, metavar="N",
                        help="write human_review.csv with a stratified sample of N apps")
    parser.add_argument("--score", action="store_true",
                        help="score the filled-in human_review.csv")
    args = parser.parse_args()

    if not Path(args.results).exists():
        raise SystemExit("No %s yet. Run: python agent.py" % args.results)
    rows = read_csv(args.results)

    did_something = False
    if args.urls:
        save_verification("url_check", run_url_check(rows))
        did_something = True
    if args.recheck:
        save_verification("blind_recheck", run_recheck(rows, args.recheck, args.apply))
        did_something = True
    if args.template:
        write_template(rows, args.template)
        did_something = True
    if args.score:
        save_verification("human_review", score_human(rows))
        did_something = True

    if not did_something:
        parser.print_help()


if __name__ == "__main__":
    main()
