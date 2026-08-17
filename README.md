# App Research Agent

Researches each app in `apps.csv` against its official developer documentation
and writes a structured `results.csv`, using Gemini with Google Search grounding.

For every app it answers: what the auth looks like, whether a new developer can
actually get credentials, how broad the API is, whether an official MCP server
exists, and whether you could realistically build agent tools on it.

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
```

Get a free API key from [Google AI Studio](https://aistudio.google.com/apikey),
then:

```bash
cp .env.example .env
```

Open `.env` and replace the placeholder with your key:

```
GEMINI_API_KEY=AIza...your-actual-key...
```

## Usage

```bash
python agent.py                 # research every app in apps.csv
python agent.py --limit 1       # just the first app, to check it works
python agent.py --show-prompt   # print the prompt and exit, no API call
```

Results are written to `results.csv`, plus a `results.json` that also keeps the
model's reasoning notes and the full list of URLs it cited.

The run checkpoints after every app, so it's safe to interrupt — re-running
skips anything already in `results.csv` instead of spending quota twice.

### Options

| Flag | Default | What it does |
| --- | --- | --- |
| `--input` | `apps.csv` | Input CSV, needs `app_name`, `category`, `website` columns |
| `--out` | `results.csv` | Where to write results |
| `--limit N` | all | Only research the first N apps |
| `--sleep N` | `2.0` | Seconds to wait between apps |
| `--show-prompt` | off | Print the prompt and exit without calling the API |

## Cost

The Gemini free tier covers this. Grounded search on Gemini 3.x models includes
a free monthly allowance of search requests, and the five apps in the default
`apps.csv` use a handful of them. Check
[current pricing](https://ai.google.dev/gemini-api/docs/pricing) before pointing
it at a much larger list.

## How it keeps the output honest

- The system prompt forbids answering from memory and requires a search first.
- Every categorical field is validated against a fixed list of allowed values;
  anything else becomes `Unknown`.
- `evidence_url` is checked against the domains the search actually cited. If
  the model returns a URL that never appeared in its citations, the confidence
  is downgraded from `High` to `Medium`.
- Failures retry once, then write a blank `Unknown` row so one bad app doesn't
  kill the whole run.
