# App Research Agent

Researches 100 apps against their official developer documentation and answers,
for each one: what auth the API uses, whether a new developer can actually get
credentials, how broad the API surface is, whether an official MCP server
exists, and whether you could realistically build an agent toolkit on it today.

Then it argues with itself. Three verification loops and a human spot-check run
over the results, and the accuracy of both the raw pass and the corrected set is
reported on the final page.

The deliverable is a single self-contained `index.html`, generated from the data
— no number on that page is typed by hand.

## The pipeline

```
apps.csv ──► agent.py ──────► results.csv ──┬─► analyze.py ──► patterns.json ──┐
              (research)      results.json  │                                   │
                 │                          │                                   ▼
            fetcher.py                      │
          (reads the docs)     verify.py ───┴─► verification.json ──► build_page.py ──► index.html
                               (3 loops + human sample)
```

| Script | What it does |
| --- | --- |
| `agent.py` | Research pass. One JSON object per app, schema-clamped, run concurrently. |
| `fetcher.py` | Finds and reads each app's developer docs over plain HTTP. No search engine. |
| `providers.py` | The model backends, behind one interface. Swap without touching the agent. |
| `verify.py` | The checks: link liveness, a blind second pass, adjudication, a human review sheet. |
| `analyze.py` | Clusters results into the patterns and writes the page's headline sentences from the numbers. |
| `build_page.py` | Renders `index.html` from `results.csv` + `patterns.json` + `verification.json`. |

## Where the evidence comes from

Most research agents pay a hosted search tool to find the docs. That is the
expensive line item — Anthropic's `web_search` bills per search, and Google
moved Search grounding behind the **paid** Gemini tier in 2026, so the free tier
no longer covers it.

This does not need one. `apps.csv` already says where every app lives, so
`fetcher.py` guesses the handful of URLs a developer portal actually uses
(`developers.x.com`, `docs.x.com`, `x.com/developers`, …), fetches whichever
resolves, and follows the in-page links that look like auth and API reference.
That costs nothing but HTTP.

It also produces *better* evidence than a search citation. The exact page text
behind every answer is kept next to the answer, so a reviewer can open the same
bytes the model read. The model is told to answer only from those pages and to
write `Unknown` for anything they do not establish.

The trade is recall: an app whose portal sits on an unguessable domain comes
back with no pages. The run reports those by name instead of guessing, and they
are the apps worth pointing a search-backed provider at.

## Providers

| `--provider` | What it does | Cost |
| --- | --- | --- |
| `docs` *(default)* | `fetcher.py` reads the docs, an LLM reasons over the text | **Free** apart from the LLM tokens |
| `anthropic` | Claude with the server-side `web_search` tool | Tokens + per-search billing |
| `gemini` | Gemini with Google Search grounding | Tokens + grounding (paid tier only) |

`docs` still needs an LLM to read the pages, but only for plain completions —
no tool use, no grounding. That is the part a free tier still covers.

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env
```

Then put **one** key in `.env`:

- **Google AI Studio** — <https://aistudio.google.com/apikey>. Free, no card.
  The free tier covers Flash models at ~1,000 requests/day, which is more than a
  full run plus the recheck loops need. Grounding is *not* free any more, which
  is exactly why the `docs` provider does its own fetching.
- **Anthropic** — <https://console.anthropic.com/settings/keys>. New accounts get
  a one-time ~$5 trial credit; after that it is pay-as-you-go.

Check it works before spending a run on it:

```bash
python agent.py --check          # researches one app end to end and prints the row
```

## Run it

```bash
# 1. research all 100 apps  ->  results.csv, results.json
python agent.py                          # ~2-3 min at the default 6 workers

# 2. verification loops     ->  verification.json
python verify.py --urls                  # link liveness — needs no API key
python verify.py --recheck 25 --apply    # blind second pass, applies adjudicated fixes
python verify.py --template 15           # writes human_review.csv to fill in by hand
python verify.py --score                 # scores it, before and after corrections

# 3. patterns + the page    ->  patterns.json, index.html
python analyze.py
python build_page.py --repo https://github.com/Exidious161/App-Research-Agent
```

The research pass checkpoints as results land, so it is safe to interrupt —
re-running skips anything already in `results.csv` instead of paying twice.

### Options

| Flag | Default | What it does |
| --- | --- | --- |
| `agent.py --provider` | auto | `docs`, `anthropic` or `gemini`. Auto-picks `docs` when any key is set |
| `agent.py --model` | per provider | Override the model |
| `agent.py --workers N` | `6` | Apps researched at once. Lower it if you hit rate limits |
| `agent.py --limit N` | all | Only research the first N apps |
| `agent.py --check` | — | Research one app, print the result, exit |
| `agent.py --show-prompt` | off | Print the prompt and exit without calling the API |
| `agent.py --input/--out` | `apps.csv` / `results.csv` | Input and output paths |
| `verify.py --urls` | — | Fetch every cited URL, flag dead links and off-domain redirects |
| `verify.py --recheck N` | — | Blind second pass over N apps, adjudicating disagreements |
| `verify.py --apply` | off | With `--recheck`, write corrections back (keeps `results_pass1.csv`) |
| `verify.py --template N` | — | Stratified human review sheet across all 10 categories |
| `verify.py --score` | — | Score the filled-in sheet, reporting pass-1 and corrected accuracy |
| `build_page.py --repo/--live` | — | URLs for the page footer |

## How it keeps the output honest

**In the research pass**

- The model is given the fetched pages and told to answer only from them, and to
  write `Unknown` rather than guess.
- Every categorical field is matched against a fixed allow-list. Anything the
  model invents becomes `Unknown` rather than quietly entering the dataset.
- `evidence_url` is checked against the pages actually fetched. A URL from a
  domain that was never fetched gets its confidence downgraded from `High` to
  `Medium` — that is the signature of a remembered URL.
- An app whose docs could not be fetched at all is recorded as `Unknown` at
  `Low` confidence, not filled in from memory.
- Failures retry once, then write a blank `Unknown` row so one bad app doesn't
  kill the run.

**In verification**

- **Link liveness** — every cited URL is fetched. Dead links and off-domain
  redirects catch the failure that matters most: a confident answer citing a
  page that doesn't exist. Needs no API key, so it always runs.
- **Blind second pass** — a differently-worded prompt re-researches a sample
  from scratch and is *never shown pass 1*. If it saw the first answer it would
  mostly agree with it, and agreement would stop being evidence of anything.
- **Adjudication** — only disagreements cost a third call. A referee looks again
  and picks a winner, or rules both wrong. `--apply` writes the result back and
  preserves `results_pass1.csv` so the page can show before and after.
- **Human sample** — a stratified sample covering all 10 categories, checked by
  hand against the real docs. Scored against both pass 1 and the corrected set,
  and every miss is printed in full.

Auth is graded with a normaliser rather than string equality, because
"OAuth 2.0" and "OAuth2" are the same answer and a naive compare would score
them as a miss.

## Deploying the page

`index.html` is fully self-contained — no CDN, no external assets — so any
static host works:

```bash
npx vercel deploy --prod        # or: netlify deploy --prod --dir .
```

Or push to GitHub and enable Pages on the repo root.
