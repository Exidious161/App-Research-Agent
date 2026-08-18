"""Finds and reads an app's developer documentation, without a search engine.

Hosted search grounding is the expensive part of a research agent -- Anthropic
web_search bills per search, and Gemini moved Google Search grounding behind the
paid tier. But we already know where every app lives: apps.csv carries a website
or a docs hint for all 100. So instead of asking a search engine where the docs
are, guess the handful of URLs a developer portal actually uses, fetch what
resolves, and follow the links that look like auth and API reference pages.

That costs nothing but HTTP, and it produces better evidence than a search
citation: the exact page text the model read is kept alongside the answer, so a
human can check the same bytes the agent saw.

The trade is recall. A portal on an unguessable domain is a miss, and gather()
reports that honestly rather than inventing a URL.
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# A bare User-Agent is enough for most docs sites, but a few (Meta) reject it
# with a 400 and only answer to a full browser header set.
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

TIMEOUT = 20
MAX_PAGE_CHARS = 20000     # per page, after stripping markup
# A JavaScript-only shell strips down to almost nothing. Salesforce redirects
# developers.salesforce.com to a Help SPA that yields 50 characters -- treating
# that as "docs found" is worse than reporting a miss, because the model then
# answers from an empty page. Anything thinner than this keeps looking.
MIN_LANDING_CHARS = 600
MAX_CANDIDATES_TRIED = 16   # every candidate; the bare root is last
MAX_TOTAL_CHARS = 60000    # across every page handed to the model

# Subdomain and path shapes that developer portals actually use.
SUBDOMAINS = ["developers", "developer", "docs", "api", "apidocs", "api-docs"]
PATHS = ["/developers", "/developer", "/docs", "/api", "/docs/api",
         "/api/docs", "/developers/docs"]

# Link text or href worth following from a docs landing page. Ordered: auth
# matters most, because auth is the field most often wrong.
FOLLOW = [
    ("auth", 10), ("oauth", 10), ("authentication", 10), ("authorization", 9),
    ("api-key", 9), ("api_key", 9), ("apikey", 9), ("token", 8), ("scopes", 7),
    ("getting-started", 6), ("quickstart", 6), ("get-started", 6),
    ("reference", 5), ("rest", 5), ("graphql", 5), ("endpoints", 5),
    ("pricing", 4), ("plans", 4), ("mcp", 8),
]

# Tried against the docs host when link-following comes up empty. These are
# the paths documentation frameworks actually generate.
# Sitemap sections that mention auth constantly but never explain it: release
# notes, blog posts, per-class SDK reference, deprecated docs.
SITEMAP_NOISE = ("/changelog", "/blog", "/legacy", "/release", "/news",
                 "/whatsnew", "/deprecat", "/archive", "/community",
                 "/interfaces/", "/classes/", "/modules/", "/enums/")

AUTH_PATHS = [
    "/authentication", "/docs/authentication", "/reference/authentication",
    "/api/authentication", "/docs/auth", "/auth", "/oauth", "/docs/oauth",
    "/getting-started", "/docs/getting-started", "/reference/authorization",
]

SKIP_EXT = (".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
            ".css", ".js", ".xml", ".woff", ".woff2")

_TAG_STRIP = re.compile(
    r"<(script|style|noscript|svg|head|nav|footer)\b.*?</\1>",
    re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK = re.compile(r"\n{3,}")


def registrable(url):
    """Host without the leading www, e.g. https://www.attio.com/x -> attio.com"""
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _base_domain(website):
    """attio.com from https://www.attio.com/, keeping any known docs subdomain."""
    host = registrable(website)
    parts = host.split(".")
    # Strip a leading docs-ish subdomain so we can re-derive all the variants.
    if len(parts) > 2 and parts[0] in SUBDOMAINS:
        return ".".join(parts[1:])
    return host


def candidates(website, docs_hint=None):
    """The URLs a developer portal plausibly lives at, best guess first.

    docs_hint wins outright. It exists for the handful of apps whose published
    docs URL is stale or whose portal is on a domain no rule would guess.
    """
    if not website and not docs_hint:
        return []
    if website and not website.startswith("http"):
        website = "https://" + website
    host = registrable(website) if website else ""
    base = _base_domain(website) if website else ""
    if not base:
        return [docs_hint] if docs_hint else []

    urls = []
    if docs_hint:
        urls.append(docs_hint if docs_hint.startswith("http")
                    else "https://" + docs_hint)
    # A website that already names a path is pointing at the docs -- prefer it
    # over the bare host, which is usually a generic developer landing page.
    if website and urlparse(website).path not in ("", "/"):
        urls.append(website)
    if host and host != base:
        urls.append("https://%s/" % host)
    for sub in SUBDOMAINS:
        urls.append("https://%s.%s/" % (sub, base))
    for path in PATHS:
        urls.append("https://%s%s" % (base, path))
    urls.append("https://%s/" % base)

    seen, ordered = set(), []
    for url in urls:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def html_to_text(markup):
    text = _TAG_STRIP.sub(" ", markup)
    text = _TAG.sub("\n", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
                .replace("&#39;", "'"))
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK.sub("\n\n", text).strip()


def fetch(url, session=None, want="html"):
    """Returns (final_url, body) or None. Never raises.

    want="xml" is for sitemaps, which are served as application/xml and would
    otherwise be dropped by the HTML content-type guard.
    """
    getter = session or requests
    try:
        response = getter.get(url, headers=HEADERS, timeout=TIMEOUT,
                              allow_redirects=True)
    except Exception:
        return None
    if response.status_code != 200:
        return None
    kind = response.headers.get("Content-Type", "text/html").lower()
    if want not in kind and not (want == "xml" and "text" in kind):
        return None
    return response.url, response.text


def links_worth_following(base_url, markup, limit=6):
    """Rank in-page links by how likely they are to answer the auth question."""
    host = registrable(base_url)
    scored = {}
    for href, anchor in re.findall(r'<a[^>]+href="([^"#?]+)"[^>]*>(.*?)</a>',
                                   markup, re.IGNORECASE | re.DOTALL):
        if href.startswith(("mailto:", "javascript:", "tel:")):
            continue
        url = urljoin(base_url, href)
        if not url.startswith("http") or url.lower().endswith(SKIP_EXT):
            continue
        # Stay on the docs host. Off-site links are marketing, not reference.
        if registrable(url) != host:
            continue
        haystack = (href + " " + _TAG.sub(" ", anchor)).lower()
        score = sum(weight for word, weight in FOLLOW if word in haystack)
        if score and url not in scored:
            scored[url] = score
    ranked = sorted(scored.items(), key=lambda kv: -kv[1])
    return [url for url, _ in ranked[:limit]]


def from_sitemap(base_url, session=None, limit=8):
    """Auth and reference URLs pulled from the docs site sitemap.

    Works where link-scraping does not: a sitemap is static XML, so a
    JavaScript-rendered docs site still lists every page in it.
    """
    parts = urlparse(base_url)
    root = "%s://%s" % (parts.scheme, parts.netloc)
    locations = []
    for name in ("/sitemap.xml", "/sitemap_index.xml", "/docs/sitemap.xml"):
        got = fetch(root + name, session, want="xml")
        if not got:
            continue
        locations = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", got[1], re.I)
        # A sitemap index points at more sitemaps; follow the first couple.
        if locations and locations[0].endswith(".xml"):
            nested = []
            for child in locations[:3]:
                child_got = fetch(child, session, want="xml")
                if child_got:
                    nested += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>",
                                         child_got[1], re.I)
            locations = nested or locations
        if locations:
            break

    scored = {}
    for url in locations:
        low = url.lower()
        if not url.startswith("http") or low.endswith(SKIP_EXT):
            continue
        if registrable(url) != registrable(base_url):
            continue
        if any(noise in low for noise in SITEMAP_NOISE):
            continue
        score = sum(weight for word, weight in FOLLOW if word in low)
        if not score:
            continue
        # The canonical "Authentication" page sits near the top of the tree;
        # anything buried many levels deep is a detail page about it.
        depth = urlparse(url).path.strip("/").count("/")
        scored[url] = score - depth
    return [u for u, _ in sorted(scored.items(), key=lambda kv: -kv[1])[:limit]]


def gather(website, max_pages=5, session=None, docs_hint=None):
    """Fetch an app's docs and return [(url, text), ...], best pages first.

    Empty list means the docs could not be located from the website alone --
    a real finding to report, not something to paper over.
    """
    session = session or requests.Session()
    pages, seen = [], set()

    # Keep going past a page that fetched fine but carries no readable text,
    # and remember the best thin result in case nothing better turns up.
    landing, best_thin = None, None
    for url in candidates(website, docs_hint)[:MAX_CANDIDATES_TRIED]:
        got = fetch(url, session)
        if not got:
            continue
        text = html_to_text(got[1])
        if len(text) >= MIN_LANDING_CHARS:
            landing = (got[0], got[1], text)
            break
        if best_thin is None or len(text) > len(best_thin[2]):
            best_thin = (got[0], got[1], text)

    landing = landing or best_thin
    if not landing:
        return []

    final_url, markup, text = landing
    seen.add(final_url)
    pages.append((final_url, text[:MAX_PAGE_CHARS]))

    targets = [u for u in links_worth_following(final_url, markup,
                                                limit=max_pages * 2)
               if u not in seen]

    # Many docs sites are JavaScript apps whose landing page carries almost no
    # crawlable links, so link-following alone finds nothing and the model is
    # left with a page that never mentions auth. The sitemap is static XML and
    # lists the auth pages regardless of how the site renders.
    if len(targets) < max_pages - 1:
        targets += [u for u in from_sitemap(final_url, session)
                    if u not in seen and u not in targets]

    # Last resort: the paths documentation frameworks conventionally generate.
    if len(targets) < max_pages - 1:
        root = "%s://%s" % (urlparse(final_url).scheme, urlparse(final_url).netloc)
        targets += [root + path for path in AUTH_PATHS
                    if root + path not in seen and root + path not in targets]

    targets = targets[:max_pages - 1]

    MIN_FOLLOWED_CHARS = 400

    def grab(url):
        got = fetch(url, session)
        if not got:
            return None
        text = html_to_text(got[1])
        # A guessed path that does not exist often still returns 200 with a
        # near-empty shell. Feeding that to the model is worse than nothing.
        if len(text) < MIN_FOLLOWED_CHARS:
            return None
        return got[0], text[:MAX_PAGE_CHARS]

    if targets:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for result in pool.map(grab, targets):
                if result and result[0] not in seen:
                    seen.add(result[0])
                    pages.append(result)

    return pages


def as_context(pages, budget=MAX_TOTAL_CHARS):
    """Flatten fetched pages into one prompt block, tagged with their URLs."""
    chunks, used = [], 0
    for url, text in pages:
        if used >= budget:
            break
        slice_ = text[:max(0, budget - used)]
        if not slice_.strip():
            continue
        chunks.append("=== SOURCE: %s ===\n%s" % (url, slice_))
        used += len(slice_)
    return "\n\n".join(chunks)


if __name__ == "__main__":
    import sys
    site = sys.argv[1] if len(sys.argv) > 1 else "https://www.pipedrive.com"
    start = time.time()
    found = gather(site)
    print("%s -> %d pages in %.1fs" % (site, len(found), time.time() - start))
    for page_url, body in found:
        print("  %-70s %6d chars" % (page_url[:70], len(body)))
