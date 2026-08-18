# Builds index.html from results.csv + patterns.json + verification.json.
#
#   python build_page.py --repo https://github.com/you/App-Research-Agent
#
# Nothing here is hand-written prose about the numbers: every stat comes out of
# the JSON, so the page cannot disagree with the data. Sections whose input is
# missing render as an honest "not run yet" instead of vanishing.

import argparse
import csv
import html
import json
from datetime import date
from pathlib import Path

from agent import read_csv

OUT = Path("index.html")


def e(value):
    return html.escape(str(value if value is not None else ""))


def load_json(path):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


# --------------------------------------------------------------------- pieces

def bar_chart(items, accent="a"):
    if not items:
        return '<p class="muted">No data.</p>'
    top = max(i["count"] for i in items) or 1
    rows = []
    for item in items:
        width = 100.0 * item["count"] / top
        rows.append(
            '<div class="bar-row">'
            '<div class="bar-label">%s</div>'
            '<div class="bar-track"><div class="bar-fill acc-%s" style="width:%.1f%%"></div></div>'
            '<div class="bar-num">%d<span class="pct">%.0f%%</span></div>'
            '</div>' % (e(item["label"]), accent, width, item["count"], item["pct"])
        )
    return '<div class="bars">%s</div>' % "".join(rows)


def stat_cards(headlines):
    if not headlines:
        return ""
    cards = []
    for line in headlines:
        cards.append(
            '<div class="card">'
            '<div class="stat">%s</div>'
            '<div class="claim">%s</div>'
            '<p class="detail">%s</p>'
            '</div>' % (e(line["stat"]), e(line["claim"]), e(line["detail"]))
        )
    return '<div class="cards">%s</div>' % "".join(cards)


def category_matrix(categories):
    if not categories:
        return '<p class="muted">No data.</p>'
    rows = []
    for c in categories:
        total = c["total"] or 1
        open_pct = 100.0 * c["open"] / total
        gated_pct = 100.0 * c["gated"] / total
        unknown_pct = max(0.0, 100.0 - open_pct - gated_pct)
        rows.append(
            '<tr>'
            '<td class="cat-name">%s</td>'
            '<td class="num">%d</td>'
            '<td class="split">'
            '<div class="stack">'
            '<span class="seg seg-open" style="width:%.1f%%" title="self-serve or trial: %d"></span>'
            '<span class="seg seg-gated" style="width:%.1f%%" title="gated: %d"></span>'
            '<span class="seg seg-unk" style="width:%.1f%%"></span>'
            '</div></td>'
            '<td class="num">%.0f%%</td>'
            '<td class="num">%d</td>'
            '<td class="num">%d</td>'
            '<td class="num">%d</td>'
            '</tr>' % (e(c["category"]), c["total"], open_pct, c["open"],
                       gated_pct, c["gated"], unknown_pct, c["easy_pct"],
                       c["easy"], c["blocked"], c["official_mcp"])
        )
    return (
        '<div class="scroll"><table class="matrix">'
        '<thead><tr>'
        '<th>Category</th><th class="num">Apps</th>'
        '<th>Credential access <span class="key"><i class="seg-open"></i>self-serve '
        '<i class="seg-gated"></i>gated</span></th>'
        '<th class="num">Easy</th><th class="num">#</th>'
        '<th class="num">Blocked</th><th class="num">Official MCP</th>'
        '</tr></thead><tbody>%s</tbody></table></div>' % "".join(rows)
    )


BUILD_CLASS = {"Easy": "ok", "Conditional": "warn", "Blocked": "bad"}
ACCESS_CLASS = {"Self-serve": "ok", "Trial": "ok", "Paid": "warn",
                "Admin approval": "warn", "Partner/contact sales": "bad"}


def findings_table(rows):
    if not rows:
        return ('<p class="muted">No <code>results.csv</code> yet. '
                'Run <code>python agent.py</code> first.</p>')
    body = []
    for r in rows:
        url = r.get("evidence_url", "")
        link = ('<a href="%s" target="_blank" rel="noopener">docs</a>' % e(url)) \
            if url.startswith("http") else '<span class="muted">—</span>'
        body.append(
            '<tr data-cat="%s" data-build="%s" data-access="%s">'
            '<td class="app">%s<span class="desc">%s</span></td>'
            '<td class="cat">%s</td>'
            '<td>%s</td>'
            '<td><span class="tag %s">%s</span></td>'
            '<td>%s<span class="sub">%s</span></td>'
            '<td>%s</td>'
            '<td><span class="tag %s">%s</span></td>'
            '<td class="blocker">%s</td>'
            '<td class="conf %s">%s</td>'
            '<td>%s</td>'
            '</tr>' % (
                e(r.get("category")), e(r.get("buildability")), e(r.get("credential_access")),
                e(r.get("app")), e(r.get("description")),
                e(r.get("category")),
                e(r.get("auth")),
                ACCESS_CLASS.get(r.get("credential_access"), ""), e(r.get("credential_access")),
                e(r.get("api")), e(r.get("api_breadth")),
                e(r.get("mcp")),
                BUILD_CLASS.get(r.get("buildability"), ""), e(r.get("buildability")),
                e(r.get("blocker")),
                (r.get("confidence") or "").lower(), e(r.get("confidence")),
                link,
            )
        )

    categories = sorted({r.get("category", "") for r in rows})
    options = "".join('<option value="%s">%s</option>' % (e(c), e(c)) for c in categories)
    return (
        '<div class="controls">'
        '<input id="q" type="search" placeholder="Search app, auth, blocker…" '
        'aria-label="Search the findings">'
        '<select id="fcat" aria-label="Filter by category">'
        '<option value="">All categories</option>%s</select>'
        '<select id="fbuild" aria-label="Filter by buildability">'
        '<option value="">All verdicts</option>'
        '<option value="Easy">Easy</option>'
        '<option value="Conditional">Conditional</option>'
        '<option value="Blocked">Blocked</option></select>'
        '<span id="count" class="muted"></span>'
        '</div>'
        '<div class="scroll"><table id="findings">'
        '<thead><tr>'
        '<th>App</th><th>Category</th><th>Auth</th><th>Credentials</th>'
        '<th>API</th><th>MCP</th><th>Verdict</th><th>Blocker</th>'
        '<th>Conf.</th><th>Evidence</th>'
        '</tr></thead><tbody>%s</tbody></table></div>' % (options, "".join(body))
    )


def verification_section(verification, patterns):
    urls = verification.get("url_check") or {}
    recheck = verification.get("blind_recheck") or {}
    human = verification.get("human_review") or {}

    if not (urls or recheck or human):
        return ('<p class="muted">No <code>verification.json</code> yet. Run '
                '<code>python verify.py --urls</code>, then <code>--recheck</code> '
                'and <code>--score</code>.</p>')

    tiles = []
    if urls:
        pct = 100.0 * urls["live"] / max(urls["checked"], 1)
        tiles.append(("%.0f%%" % pct, "evidence URLs resolve",
                      "%d of %d cited pages return a live response; %d are dead and "
                      "%d redirect off the cited domain."
                      % (urls["live"], urls["checked"], urls["dead"],
                         urls.get("redirected_off_domain", 0))))
    if recheck:
        tiles.append(("%.0f%%" % recheck["agreement_overall"],
                      "blind second-pass agreement",
                      "A second pass researched %d apps from scratch without seeing "
                      "pass 1. %d field disagreements went to an adjudicator."
                      % (recheck["sample_size"], len(recheck.get("corrections", [])))))
    if human:
        before = human.get("accuracy_before_corrections")
        detail = "%d of %d fields correct across %d hand-checked apps." % (
            human["correct"], human["fields_judged"], human["apps_reviewed"])
        if before is not None:
            detail += " Pass 1 alone scored %.0f%% — the loops added %.0f points." % (
                before, human["accuracy"] - before)
        tiles.append(("%.0f%%" % human["accuracy"], "human-verified accuracy", detail))

    tile_html = "".join(
        '<div class="card small"><div class="stat">%s</div>'
        '<div class="claim">%s</div><p class="detail">%s</p></div>' % (e(a), e(b), e(c))
        for a, b, c in tiles)

    blocks = ['<div class="cards">%s</div>' % tile_html]

    if human and human.get("accuracy_before_corrections") is not None:
        before = human["accuracy_before_corrections"]
        after = human["accuracy"]
        blocks.append(
            '<div class="beforeafter">'
            '<div class="ba-row"><span class="ba-label">Pass 1, raw agent</span>'
            '<div class="bar-track"><div class="bar-fill acc-c" style="width:%.1f%%"></div></div>'
            '<span class="ba-num">%.0f%%</span></div>'
            '<div class="ba-row"><span class="ba-label">After verification loops</span>'
            '<div class="bar-track"><div class="bar-fill acc-a" style="width:%.1f%%"></div></div>'
            '<span class="ba-num">%.0f%%</span></div>'
            '</div>' % (before, before, after, after))

    if human and human.get("by_field"):
        rows = "".join(
            '<tr><td>%s</td><td class="num">%.0f%%</td></tr>' % (e(f), v)
            for f, v in sorted(human["by_field"].items(), key=lambda kv: -kv[1]))
        blocks.append(
            '<h3>Accuracy by field</h3><div class="scroll">'
            '<table class="matrix"><thead><tr><th>Field</th>'
            '<th class="num">Correct</th></tr></thead><tbody>%s</tbody></table></div>' % rows)

    misses = (human or {}).get("misses") or []
    if misses:
        rows = "".join(
            '<tr><td>%s</td><td>%s</td><td class="was">%s</td><td class="is">%s</td>'
            '<td class="note">%s</td></tr>' % (
                e(m["app"]), e(m["field"]), e(m["agent"]), e(m["human"]), e(m["notes"]))
            for m in misses)
        blocks.append(
            '<h3>Where the agent was wrong <span class="muted">(every miss, '
            'not a selection)</span></h3><div class="scroll">'
            '<table class="matrix misses"><thead><tr>'
            '<th>App</th><th>Field</th><th>Agent said</th><th>Actually</th>'
            '<th>Why it missed</th></tr></thead><tbody>%s</tbody></table></div>' % rows)
    elif human:
        blocks.append('<p class="muted">No misses in the hand-checked sample.</p>')

    dead = [r for r in (urls.get("results") or []) if not r["ok"]]
    if dead:
        rows = "".join(
            '<tr><td>%s</td><td class="mono">%s</td><td>%s</td></tr>' % (
                e(d["app"]), e(d["url"]), e(d.get("error") or d.get("status")))
            for d in dead)
        blocks.append(
            '<h3>Dead evidence links</h3><div class="scroll">'
            '<table class="matrix"><thead><tr><th>App</th><th>Cited URL</th>'
            '<th>Result</th></tr></thead><tbody>%s</tbody></table></div>' % rows)

    return "".join(blocks)


AGENT_STEPS = [
    ("1", "Input", "apps.csv — 100 apps across 10 categories, with a category hint "
                   "and homepage for each."),
    ("2", "Research pass", "Gemini 3.x with Google Search grounding. The system prompt "
                           "forbids answering from memory and demands a search first. "
                           "Returns one JSON object per app."),
    ("3", "Schema clamp", "Every categorical field is matched against a fixed allow-list. "
                          "Anything the model invents becomes <code>Unknown</code> rather "
                          "than silently entering the dataset."),
    ("4", "Citation check", "The URLs the search tool actually cited are pulled off the "
                            "response. If the model's <code>evidence_url</code> is from a "
                            "domain that never appeared, confidence drops High → Medium."),
    ("5", "Blind second pass", "A differently-worded prompt re-researches a sample from "
                               "scratch, never shown pass 1. Agreement means two independent "
                               "runs landed together, not that the model ratified itself."),
    ("6", "Adjudication", "Only disagreements cost a third call. A referee searches again "
                          "and picks a winner — or rules both wrong."),
    ("7", "Link liveness", "Every cited URL is fetched. Dead links and off-domain "
                           "redirects catch the failure that matters most: a confident "
                           "answer citing a page that does not exist."),
    ("8", "Human sample", "A stratified sample across all 10 categories is checked by hand "
                          "against the real docs, and scored against pass 1 and the "
                          "corrected set."),
]

HUMAN_NEEDED = [
    ("Deciding what &ldquo;gated&rdquo; means",
     "The model happily called a free-signup app self-serve when the API in fact needs a "
     "workspace admin to install it. That distinction had to be written into the prompt by "
     "hand, as an explicit rule, before the column meant anything."),
    ("Official vs community MCP",
     "Left alone the agent counted any GitHub repo named <code>*-mcp</code> as an official "
     "server. The rule that only a vendor-published or vendor-linked server counts is a human "
     "judgement encoded into the prompt."),
    ("The ambiguous long tail",
     "Small or private-beta apps returned confident answers with thin evidence. These needed "
     "a person to open the docs and decide, which is exactly where the sample check was "
     "pointed."),
    ("Grading free-text auth",
     "&ldquo;OAuth 2.0&rdquo; and &ldquo;OAuth2&rdquo; are the same answer; a naive string "
     "compare scored them as a miss and made accuracy look worse than it was. The "
     "normaliser in <code>verify.py</code> is hand-written."),
]


def agent_section():
    steps = "".join(
        '<li><span class="step-n">%s</span><div><h4>%s</h4><p>%s</p></div></li>'
        % (n, t, d) for n, t, d in AGENT_STEPS)
    humans = "".join(
        '<div class="human"><h4>%s</h4><p>%s</p></div>' % (t, d)
        for t, d in HUMAN_NEEDED)
    return (
        '<ol class="pipeline">%s</ol>'
        '<h3>Where a human was needed</h3>'
        '<div class="humans">%s</div>' % (steps, humans))


CSS = """
*{box-sizing:border-box}
:root{
  --bg:#0c0d10; --panel:#131519; --panel2:#181b21; --line:#242830;
  --text:#e8eaee; --dim:#9aa1ad; --faint:#6b7280;
  --ok:#3ecf8e; --warn:#f5a524; --bad:#f0616d; --accent:#7c8cff;
}
@media (prefers-color-scheme:light){
  :root:not([data-theme="dark"]){
    --bg:#fbfbfd; --panel:#fff; --panel2:#f5f6f8; --line:#e3e6ec;
    --text:#12141a; --dim:#5b6472; --faint:#8b93a1;
  }
}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:0 22px}
a{color:var(--accent)}
code{font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--panel2);
  border:1px solid var(--line);border-radius:4px;padding:1px 5px}
.muted{color:var(--dim)}
.num{text-align:right;font-variant-numeric:tabular-nums}

header{padding:56px 0 34px;border-bottom:1px solid var(--line)}
.eyebrow{color:var(--accent);font-size:12px;font-weight:650;letter-spacing:.10em;
  text-transform:uppercase;margin:0 0 12px}
h1{margin:0 0 12px;font-size:40px;line-height:1.12;letter-spacing:-.022em;font-weight:680}
.sub{color:var(--dim);font-size:17px;max-width:66ch;margin:0}
.meta{display:flex;flex-wrap:wrap;gap:8px;margin-top:20px}
.chip{background:var(--panel);border:1px solid var(--line);border-radius:999px;
  padding:5px 12px;font-size:12.5px;color:var(--dim)}
.chip b{color:var(--text);font-weight:620}

section{padding:46px 0;border-bottom:1px solid var(--line)}
h2{font-size:13px;font-weight:680;letter-spacing:.09em;text-transform:uppercase;
  color:var(--dim);margin:0 0 6px}
.lede{font-size:22px;letter-spacing:-.012em;margin:0 0 26px;max-width:62ch;font-weight:600}
h3{font-size:15px;margin:32px 0 12px;font-weight:640}
h4{margin:0 0 4px;font-size:14.5px;font-weight:640}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(228px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px}
.card .stat{font-size:34px;font-weight:690;letter-spacing:-.03em;color:var(--accent);
  line-height:1.05}
.card.small .stat{font-size:29px}
.card .claim{font-size:14.5px;font-weight:600;margin-top:7px}
.card .detail{color:var(--dim);font-size:13px;margin:9px 0 0;line-height:1.5}

.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:30px}
.bars{display:flex;flex-direction:column;gap:9px}
.bar-row{display:grid;grid-template-columns:170px 1fr 74px;align-items:center;gap:12px}
.bar-label{font-size:13px;color:var(--dim);text-align:right;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.bar-track{background:var(--panel2);border-radius:5px;height:20px;overflow:hidden}
.bar-fill{height:100%;border-radius:5px}
.acc-a{background:linear-gradient(90deg,#7c8cff,#9aa6ff)}
.acc-b{background:linear-gradient(90deg,#3ecf8e,#5fdca3)}
.acc-c{background:linear-gradient(90deg,#f5a524,#f7b955)}
.bar-num{font-size:13px;font-variant-numeric:tabular-nums;font-weight:620}
.bar-num .pct{color:var(--faint);font-weight:400;margin-left:6px;font-size:11.5px}

.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:11px;
  background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font-weight:620;color:var(--dim);font-size:11.5px;
  letter-spacing:.05em;text-transform:uppercase;padding:11px 12px;
  border-bottom:1px solid var(--line);white-space:nowrap;
  position:sticky;top:0;background:var(--panel2);z-index:1}
td{padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--panel2)}
.app{font-weight:620;min-width:190px}
.app .desc{display:block;font-weight:400;color:var(--dim);font-size:12px;
  margin-top:2px;max-width:34ch}
.cat{color:var(--dim);white-space:nowrap;font-size:12px}
.sub{display:block;color:var(--faint);font-size:11.5px}
.blocker{color:var(--dim);max-width:26ch;font-size:12.5px}
.conf{font-size:11.5px;font-weight:620}
.conf.high{color:var(--ok)} .conf.medium{color:var(--warn)} .conf.low{color:var(--bad)}
.mono{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;
  max-width:34ch;overflow-wrap:anywhere}

.tag{display:inline-block;padding:2.5px 9px;border-radius:999px;font-size:11.5px;
  font-weight:600;white-space:nowrap;border:1px solid transparent}
.tag.ok{background:rgba(62,207,142,.13);color:var(--ok);border-color:rgba(62,207,142,.28)}
.tag.warn{background:rgba(245,165,36,.13);color:var(--warn);border-color:rgba(245,165,36,.28)}
.tag.bad{background:rgba(240,97,109,.13);color:var(--bad);border-color:rgba(240,97,109,.28)}

.controls{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px;align-items:center}
.controls input,.controls select{background:var(--panel);color:var(--text);
  border:1px solid var(--line);border-radius:8px;padding:9px 12px;font-size:13.5px;
  font-family:inherit}
.controls input{flex:1;min-width:210px}
.controls input:focus,.controls select:focus{outline:2px solid var(--accent);
  outline-offset:-1px}

.matrix .cat-name{font-weight:600;white-space:nowrap}
.stack{display:flex;height:17px;border-radius:5px;overflow:hidden;
  background:var(--panel2);min-width:150px}
.seg-open{background:var(--ok)} .seg-gated{background:var(--bad)}
.seg-unk{background:var(--line)}
.key{font-weight:400;text-transform:none;letter-spacing:0;color:var(--faint);
  margin-left:8px;font-size:11px}
.key i{display:inline-block;width:9px;height:9px;border-radius:2px;
  margin:0 3px 0 8px;vertical-align:baseline}
.misses .was{color:var(--bad)} .misses .is{color:var(--ok);font-weight:600}
.misses .note{color:var(--dim);max-width:38ch;font-size:12.5px}

.beforeafter{margin:22px 0;display:flex;flex-direction:column;gap:11px;max-width:640px}
.ba-row{display:grid;grid-template-columns:200px 1fr 56px;align-items:center;gap:12px}
.ba-label{font-size:13px;color:var(--dim);text-align:right}
.ba-num{font-weight:660;font-variant-numeric:tabular-nums}

.pipeline{list-style:none;padding:0;margin:0;display:grid;
  grid-template-columns:repeat(auto-fit,minmax(258px,1fr));gap:14px}
.pipeline li{display:flex;gap:13px;background:var(--panel);border:1px solid var(--line);
  border-radius:11px;padding:16px}
.step-n{flex:none;width:25px;height:25px;border-radius:7px;background:var(--panel2);
  border:1px solid var(--line);color:var(--accent);font-size:12px;font-weight:680;
  display:grid;place-items:center}
.pipeline p{margin:0;color:var(--dim);font-size:12.5px;line-height:1.5}
.humans{display:grid;grid-template-columns:repeat(auto-fit,minmax(268px,1fr));gap:14px}
.human{background:var(--panel);border:1px solid var(--line);border-left:2px solid var(--warn);
  border-radius:11px;padding:16px}
.human p{margin:0;color:var(--dim);font-size:12.5px;line-height:1.5}

.run{background:var(--panel);border:1px solid var(--line);border-radius:11px;
  padding:18px 20px;overflow-x:auto}
.run pre{margin:0;font:12.5px/1.75 ui-monospace,Menlo,monospace;color:var(--text)}
.run .cmt{color:var(--faint)}

footer{padding:34px 0 60px;color:var(--faint);font-size:12.5px}
footer a{color:var(--dim)}
@media(max-width:720px){
  h1{font-size:29px} .lede{font-size:18px}
  .bar-row{grid-template-columns:118px 1fr 66px}
  .ba-row{grid-template-columns:130px 1fr 46px}
}
"""

JS = """
(function(){
  var q=document.getElementById('q'), fc=document.getElementById('fcat'),
      fb=document.getElementById('fbuild'), tbl=document.getElementById('findings'),
      out=document.getElementById('count');
  if(!tbl) return;
  var rows=[].slice.call(tbl.tBodies[0].rows);
  function apply(){
    var term=(q.value||'').toLowerCase(), cat=fc.value, build=fb.value, shown=0;
    rows.forEach(function(r){
      var hit=(!term||r.textContent.toLowerCase().indexOf(term)>-1)
        && (!cat||r.dataset.cat===cat) && (!build||r.dataset.build===build);
      r.style.display=hit?'':'none';
      if(hit) shown++;
    });
    out.textContent=shown+' of '+rows.length+' apps';
  }
  [q,fc,fb].forEach(function(el){
    el.addEventListener('input',apply); el.addEventListener('change',apply);
  });
  apply();
})();
"""


def build(rows, patterns, verification, repo, live):
    generated = date.today().isoformat()
    total = patterns.get("total_apps", len(rows))

    links = []
    if repo:
        links.append('<a href="%s" target="_blank" rel="noopener">Source repo</a>' % e(repo))
    if live:
        links.append('<a href="%s" target="_blank" rel="noopener">Live page</a>' % e(live))
    link_html = ' &middot; '.join(links)

    human = (verification.get("human_review") or {})
    accuracy_chip = ('<span class="chip">Human-verified <b>%.0f%%</b></span>'
                     % human["accuracy"]) if human.get("accuracy") is not None else ""

    run_block = (
        '<div class="run"><pre>'
        '<span class="cmt"># 1. research all 100 apps -&gt; results.csv</span>\n'
        'python agent.py\n\n'
        '<span class="cmt"># 2. verification loops -&gt; verification.json</span>\n'
        'python verify.py --urls              <span class="cmt"># no API key needed</span>\n'
        'python verify.py --recheck 25 --apply\n'
        'python verify.py --template 15       <span class="cmt"># fill in by hand</span>\n'
        'python verify.py --score\n\n'
        '<span class="cmt"># 3. patterns + this page</span>\n'
        'python analyze.py\n'
        'python build_page.py</pre></div>'
    )

    # A machine-readable copy of everything on the page, so an agent reading this
    # file does not have to scrape the tables.
    payload = json.dumps({"generated": generated, "apps": rows,
                          "patterns": patterns, "verification": verification},
                         indent=None, separators=(",", ":"))

    return """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>100 Apps: What It Takes to Make Them Agent-Callable</title>
<meta name="description" content="Agent-driven API research across %(total)d apps: auth, credential access, API surface, MCP coverage, buildability — with the accuracy check.">
<style>%(css)s</style>
</head><body>

<header><div class="wrap">
  <p class="eyebrow">AI Product Ops &middot; Take-home</p>
  <h1>100 apps, researched by an agent,<br>then checked against the docs.</h1>
  <p class="sub">Which of these could become an agent toolkit today, what auth stands in
  the way, and which ones need a conversation before a single line gets written.</p>
  <div class="meta">
    <span class="chip"><b>%(total)d</b> apps</span>
    <span class="chip"><b>10</b> categories</span>
    <span class="chip">Gemini 3.x + Search grounding</span>
    %(accuracy_chip)s
    <span class="chip">Generated <b>%(generated)s</b></span>
  </div>
</div></header>

<section><div class="wrap">
  <h2>The headline</h2>
  <p class="lede">Four things the data says, before any of the detail.</p>
  %(cards)s
</div></section>

<section><div class="wrap">
  <h2>The patterns</h2>
  <p class="lede">Auth is not the hard part. Getting credentials is.</p>
  <div class="grid2">
    <div><h3>Auth methods <span class="muted">(apps can offer more than one)</span></h3>
      %(auth_chart)s</div>
    <div><h3>Credential access</h3>%(access_chart)s</div>
    <div><h3>Buildability verdict</h3>%(build_chart)s</div>
    <div><h3>Most common blockers</h3>%(blocker_chart)s</div>
    <div><h3>API surface</h3>%(api_chart)s</div>
    <div><h3>MCP coverage</h3>%(mcp_chart)s</div>
  </div>
  <h3>Self-serve vs gated, by category</h3>
  <p class="muted" style="margin-top:-4px;font-size:13.5px">Sorted by the share of
  apps that are buildable today. This is where the easy wins and the outreach list
  separate.</p>
  %(matrix)s
</div></section>

<section><div class="wrap">
  <h2>The verification</h2>
  <p class="lede">Three loops that fail in different ways, plus a hand-check.
  Misses are listed in full, not summarised.</p>
  %(verification)s
</div></section>

<section><div class="wrap">
  <h2>The agent</h2>
  <p class="lede">A research pass, a schema clamp, and three checks that argue with it.</p>
  %(agent)s
  <h3>Run it yourself</h3>
  %(run)s
</div></section>

<section><div class="wrap">
  <h2>The findings</h2>
  <p class="lede">All %(total)d apps. Every row links to the documentation page the
  verdict came from.</p>
  %(table)s
</div></section>

<footer><div class="wrap">
  <p>Built for the Composio AI Product Ops take-home. %(links)s</p>
  <p>Every number on this page is generated from <code>results.csv</code>,
  <code>patterns.json</code> and <code>verification.json</code> by
  <code>build_page.py</code> — none of it is typed by hand. The full dataset is
  embedded below as JSON for machine readers.</p>
</div></footer>

<script type="application/json" id="dataset">%(payload)s</script>
<script>%(js)s</script>
</body></html>
""" % {
        "css": CSS, "js": JS, "total": total, "generated": generated,
        "accuracy_chip": accuracy_chip,
        "cards": stat_cards(patterns.get("headlines", [])),
        "auth_chart": bar_chart(patterns.get("auth", []), "a"),
        "access_chart": bar_chart(patterns.get("credential_access", []), "b"),
        "build_chart": bar_chart(patterns.get("buildability", []), "b"),
        "blocker_chart": bar_chart(patterns.get("blockers", []), "c"),
        "api_chart": bar_chart(patterns.get("api_type", []), "a"),
        "mcp_chart": bar_chart(patterns.get("mcp", []), "a"),
        "matrix": category_matrix(patterns.get("by_category", [])),
        "verification": verification_section(verification, patterns),
        "agent": agent_section(),
        "run": run_block,
        "table": findings_table(rows),
        "links": link_html,
        "payload": payload,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="results.csv")
    parser.add_argument("--patterns", default="patterns.json")
    parser.add_argument("--verification", default="verification.json")
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--repo", default="", help="source repo URL for the footer")
    parser.add_argument("--live", default="", help="deployed page URL for the footer")
    args = parser.parse_args()

    rows = read_csv(args.results) if Path(args.results).exists() else []
    patterns = load_json(args.patterns)
    verification = load_json(args.verification)

    if not rows:
        print("Warning: no %s — the findings table will be empty." % args.results)
    if not patterns:
        print("Warning: no %s — run analyze.py for the headline stats." % args.patterns)

    page = build(rows, patterns, verification, args.repo, args.live)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")

    # Vercel serves public/ as the static root, because a requirements.txt at
    # the repo root makes it guess Python and fail the build. Keep a copy there
    # so deploying is just `vercel deploy --prod` with no extra steps.
    if out.parent != Path("public"):
        mirror = Path("public") / "index.html"
        mirror.parent.mkdir(exist_ok=True)
        mirror.write_text(page, encoding="utf-8")
        print("Also wrote %s for static hosting" % mirror)
    print("Wrote %s (%.1f KB, %d apps)" % (args.out, len(page) / 1024, len(rows)))


if __name__ == "__main__":
    main()
