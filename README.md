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
                                            │                                   ▼
                              verify.py ────┴─► verification.json ──► build_page.py ──► index.html
                              (3 loops + human sample)
```

| Script | What it does |
| --- | --- |
| `agent.py` | Research pass. Gemini 3.x with Google Search grounding, one JSON object per app, schema-clamped. |
| `verify.py` | The checks: link liveness, a blind second pass, adjudication of disagreements, and a human review sheet. |
| `analyze.py` | Clusters the results into the patterns and writes the page's headline sentences from the numbers. |
| `build_page.py` | Renders `index.html` from `results.csv` + `patterns.json` + `verification.json`. |

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
```

Get a free API key from [Google AI Studio](https://aistudio.google.com/apikey), then:

```bash
cp .env.example .env          # then paste your key into .env
```

## Run it

```bash
# 1. research all 100 apps  ->  results.csv, results.json
python agent.py

# 2. verification loops     ->  verification.json
python verify.py --urls                 # link liveness — needs no API key
python verify.py --recheck 25 --apply   # blind second pass, applies adjudicated fixes
python verify.py --template 15          # writes human_review.csv to fill in by hand
python verify.py --score                # scores it, before and after corrections

# 3. patterns + the page    ->  patterns.json, index.html
python analyze.py
python build_page.py --repo https://github.com/Exidious161/App-Research-Agent
```

The research pass checkpoints after every app, so it is safe to interrupt —
re-running skips anything already in `results.csv` instead of spending quota
twice.

### Options

| Flag | Default | What it does |
| --- | --- | --- |
| `agent.py --input` | `apps.csv` | Input CSV, needs `app_name`, `category`, `website` columns |
| `agent.py --out` | `results.csv` | Where to write results |
| `agent.py --limit N` | all | Only research the first N apps |
| `agent.py --sleep N` | `2.0` | Seconds to wait between apps |
| `agent.py --show-prompt` | off | Print the prompt and exit without calling the API |
| `verify.py --urls` | — | Fetch every cited URL, flag dead links and off-domain redirects |
| `verify.py --recheck N` | — | Blind second pass over N apps, adjudicating disagreements |
| `verify.py --apply` | off | With `--recheck`, write corrections back (keeps `results_pass1.csv`) |
| `verify.py --template N` | — | Stratified human review sheet across all 10 categories |
| `verify.py --score` | — | Score the filled-in sheet, reporting pass-1 and corrected accuracy |
| `build_page.py --repo/--live` | — | URLs for the page footer |

## How it keeps the output honest

**In the research pass**

- The system prompt forbids answering from memory and requires a search first.
- Every categorical field is matched against a fixed allow-list. Anything the
  model invents becomes `Unknown` rather than quietly entering the dataset.
- `evidence_url` is checked against the domains the search tool actually cited.
  A URL from a domain that never appeared in the citations gets its confidence
  downgraded from `High` to `Medium` — that is the signature of a remembered URL.
- Failures retry once, then write a blank `Unknown` row so one bad app doesn't
  kill the run.

**In verification**

- **Link liveness** — every cited URL is fetched. Dead links and off-domain
  redirects catch the failure that matters most: a confident answer citing a
  page that doesn't exist. Needs no API key, so it always runs.
- **Blind second pass** — a differently-worded prompt re-researches a sample
  from scratch and is *never shown pass 1*. If it saw the first answer it would
  mostly agree with it, and agreement would stop being evidence of anything.
- **Adjudication** — only disagreements cost a third call. A referee searches
  again and picks a winner, or rules both wrong. `--apply` writes the result
  back and preserves `results_pass1.csv` so the page can show before and after.
- **Human sample** — a stratified sample covering all 10 categories, checked by
  hand against the real docs. Scored against both pass 1 and the corrected set,
  and every miss is printed in full.

Auth is graded with a normaliser rather than string equality, because
"OAuth 2.0" and "OAuth2" are the same answer and a naive compare would score
them as a miss.

## Cost

The Gemini free tier covers a full run. Grounded search on Gemini 3.x models
includes **5,000 free search requests per month**, then $14 per 1,000. One pass
over 100 apps plus a 25-app recheck uses a few hundred, so it stays inside the
free allowance. Check
[current pricing](https://ai.google.dev/gemini-api/docs/pricing) before pointing
it at a much larger list.

## Deploying the page

`index.html` is fully self-contained — no CDN, no external assets — so any
static host works:

```bash
npx vercel deploy --prod        # or: netlify deploy --prod --dir .
```

Or push to GitHub and enable Pages on the repo root.
