#!/usr/bin/env python3
"""
Sports Fund Intelligence Digest
────────────────────────────────────────────────────────────────────────────
Three-layer news pipeline per fund:
  1. NewsAPI.org    — broad keyword search with date filtering
  2. RSS / Google News — Sportico, SBJ, Bloomberg, Reuters, per-fund GNews
  3. Perplexity Sonar — llama-3.1-sonar-large-128k-online for paywalled sources

Runs weekly via GitHub Actions; emails an HTML digest every Monday 7 AM ET.
"""

import json
import os
import re
import smtplib
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Environment / secrets ─────────────────────────────────────────────────────
NEWSAPI_KEY        = os.environ.get("NEWSAPI_KEY", "")
PERPLEXITY_KEY     = os.environ.get("PERPLEXITY_API_KEY", "")
GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL    = os.environ.get("RECIPIENT_EMAIL", GMAIL_ADDRESS)

# ── Constants ─────────────────────────────────────────────────────────────────
FUNDS_FILE           = Path(__file__).parent / "funds.json"
NEWSAPI_BASE         = "https://newsapi.org/v2/everything"
PERPLEXITY_BASE      = "https://api.perplexity.ai/chat/completions"
PERPLEXITY_MODEL     = "llama-3.1-sonar-large-128k-online"
PERPLEXITY_RPS_DELAY = 3.0   # seconds between Perplexity requests
GOOGLE_NEWS_RSS      = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
LOOKBACK_DAYS        = 7
REQUEST_TIMEOUT      = 20    # seconds for HTTP calls

# ── Category detection patterns ───────────────────────────────────────────────
CATEGORY_PATTERNS: dict[str, str] = {
    "NEW DEALS":          r"acqui|investment|stake|buys|backing|invests|funded|portfolio|deal|partnership|minority|majority|purchase",
    "FUNDRAISING":        r"fundrais|fund raise|capital raise|hard cap|limited partner|\bLP\b|billion fund|million fund|new fund|fund launch|raising capital|fund close|\bclose\b",
    "EXITS":              r"\bexit\b|divest|sold stake|secondary sale|\bIPO\b|going public|spin.off|sell.* stake|full exit",
    "PEOPLE":             r"\bhire\b|hires|appoints|joins|departs|leaves|named|promoted|new partner|managing director|alumni|launches firm|new firm",
    "MARKET/REGULATORY":  r"regulation|regulatory|league rule|ownership rule|\bSEC\b|antitrust|governance|approval|restriction|policy change|compliance",
    "VALUATIONS":         r"valuat|valued at|worth \$|bid|franchise value|secondary transaction|market value|bid activity",
    "MEDIA RIGHTS":       r"media rights|broadcast|streaming|TV deal|television deal|rights deal|content deal|media contract",
}

# ── Priority RSS feeds ────────────────────────────────────────────────────────
PRIORITY_RSS_FEEDS: list[tuple[str, str]] = [
    ("https://www.sportico.com/feed/",                                       "Sportico"),
    ("https://www.sportsbusinessjournal.com/RSS/SBJ-News.aspx",              "Sports Business Journal"),
    ("https://www.sportsbusinessjournal.com/RSS/Transactions.aspx",          "SBJ Transactions"),
    ("https://alternativeswatch.com/feed/",                                  "Alternatives Watch"),
    ("https://thesportsplaymaker.com/feed/",                                 "The Sports Playmaker"),
    ("https://feeds.bloomberg.com/markets/news.rss",                         "Bloomberg Markets"),
    ("https://feeds.reuters.com/reuters/businessNews",                       "Reuters Business"),
    ("https://feeds.a.dj.com/rss/RSSWSJD.xml",                              "WSJ"),
    ("https://fortune.com/feed/",                                            "Fortune"),
    ("https://www.ft.com/rss/home",                                          "Financial Times"),
]

# ── Auto-detection search queries ─────────────────────────────────────────────
AUTO_DETECT_QUERIES: list[str] = [
    '"sports private equity" "new fund"',
    '"sports fund" launch',
    '"sports PE" raises',
    '"sports investment" "debut fund"',
    '("Blackstone" OR "KKR" OR "Apollo" OR "Ares" OR "Sixth Street" OR "RedBird") "sports fund" launch',
    '"new sports investment firm"',
    '"sports-focused" fund raise',
    '"former" ("Blackstone" OR "KKR" OR "Apollo" OR "Ares") "sports"',
]

# ── Fund I/O ──────────────────────────────────────────────────────────────────

def load_funds() -> list[dict]:
    with FUNDS_FILE.open() as f:
        return json.load(f)["funds"]


def save_funds(funds: list[dict]) -> None:
    FUNDS_FILE.write_text(json.dumps({"funds": funds}, indent=2))
    log.info("funds.json updated — %d funds total", len(funds))


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_recent(pub_struct) -> bool:
    """Return True if feedparser time_struct is within the look-back window."""
    if not pub_struct:
        return True
    try:
        pub_dt = datetime(*pub_struct[:6], tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
        return pub_dt >= cutoff
    except Exception:
        return True


def categorize(title: str, summary: str) -> str:
    text = (title + " " + summary).lower()
    for category, pattern in CATEGORY_PATTERNS.items():
        if re.search(pattern, text, re.I):
            return category
    return "GENERAL"


# ── Highlights ranking ────────────────────────────────────────────────────────

# Base score per category (higher = more important)
_CATEGORY_SCORE: dict[str, int] = {
    "NEW DEALS":          30,
    "FUNDRAISING":        30,
    "EXITS":              25,
    "VALUATIONS":         20,
    "MARKET/REGULATORY":  15,
    "MEDIA RIGHTS":       15,
    "PEOPLE":             10,
    "GENERAL":             0,
}

# Patterns that signal a high-value story; each match adds to the score
_SIGNAL_BOOSTS: list[tuple[str, int]] = [
    # Deal / fund size with a dollar figure
    (r"\$\s*\d+(?:\.\d+)?\s*(?:b(?:illion)?|bn)",   50),  # $Xbn / $X billion
    (r"\$\s*\d+(?:\.\d+)?\s*(?:m(?:illion)?|mn)",   20),  # $Xm / $X million
    # Landmark fund events
    (r"hard cap",                                     25),
    (r"fund close|final close|closes? on",            25),
    (r"debut fund|inaugural fund|first fund",         20),
    # Deal specifics
    (r"majority stake|controlling stake",             20),
    (r"acqui(?:res?|sition)|buyout",                  15),
    (r"ipo|going public",                             20),
    # Senior people moves at well-known firms
    (r"(?:ceo|cfo|coo|managing partner|founding partner)"
     r".{0,30}(?:join|appoint|hire|depart|leave|name)",   15),
]


def rank_article(article: dict, category: str) -> int:
    """Return a numeric priority score — higher is more important."""
    text  = (article.get("title", "") + " " + article.get("summary", "")).lower()
    score = _CATEGORY_SCORE.get(category, 0)
    for pattern, boost in _SIGNAL_BOOSTS:
        if re.search(pattern, text, re.I):
            score += boost
    # Perplexity articles tend to cover more substantive sources — small bonus
    if article.get("layer") == "perplexity":
        score += 5
    return score


def pick_highlights(
    fund_sections: list[dict],
    max_highlights: int = 8,
    min_highlights: int = 5,
) -> list[dict]:
    """
    Scan every article across all fund sections and return the top stories.

    Returns a list of dicts: {fund_name, title, url, category, score}
    sorted descending by score, capped at max_highlights.
    """
    candidates: list[dict] = []

    for section in fund_sections:
        fund_name    = section["name"]
        cat_articles = section["category_articles"]

        for category, articles in cat_articles.items():
            for article in articles:
                score = rank_article(article, category)
                if score <= 0:
                    continue
                candidates.append(
                    {
                        "fund_name": fund_name,
                        "title":     article.get("title", "").strip(),
                        "url":       article.get("url", "#"),
                        "category":  category,
                        "score":     score,
                    }
                )

    # Sort by score descending, then deduplicate by URL
    candidates.sort(key=lambda x: x["score"], reverse=True)
    seen_urls:   set[str] = set()
    seen_titles: set[str] = set()
    highlights:  list[dict] = []

    for c in candidates:
        norm_title = re.sub(r"\W+", " ", c["title"].lower()).strip()
        if c["url"] in seen_urls or norm_title in seen_titles:
            continue
        seen_urls.add(c["url"])
        seen_titles.add(norm_title)
        highlights.append(c)
        if len(highlights) == max_highlights:
            break

    # If we're below the minimum, top up with next-best regardless of score
    if len(highlights) < min_highlights:
        for c in candidates:
            if len(highlights) >= min_highlights:
                break
            norm_title = re.sub(r"\W+", " ", c["title"].lower()).strip()
            if c["url"] in seen_urls or norm_title in seen_titles:
                continue
            seen_urls.add(c["url"])
            seen_titles.add(norm_title)
            highlights.append(c)

    return highlights


def dedup(articles: list[dict]) -> list[dict]:
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    out: list[dict] = []
    for a in articles:
        norm = re.sub(r"\W+", " ", a.get("title", "").lower()).strip()
        url = a.get("url", "")
        if url in seen_urls or norm in seen_titles or not norm:
            continue
        seen_urls.add(url)
        seen_titles.add(norm)
        out.append(a)
    return out


def clean_html(raw: str) -> str:
    return BeautifulSoup(raw, "html.parser").get_text(separator=" ").strip()[:400]


# ── Layer 1: NewsAPI ──────────────────────────────────────────────────────────

def newsapi_search(query: str) -> list[dict]:
    """Search NewsAPI for articles matching query, within the look-back window."""
    if not NEWSAPI_KEY:
        return []
    from_date = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    params = {
        "q":        query,
        "from":     from_date,
        "sortBy":   "relevancy",
        "language": "en",
        "apiKey":   NEWSAPI_KEY,
        "pageSize": 20,
    }
    try:
        resp = requests.get(NEWSAPI_BASE, params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            log.warning("NewsAPI quota hit — skipping query: %s", query)
            return []
        resp.raise_for_status()
        return [
            {
                "title":   a.get("title", "").strip(),
                "url":     a.get("url", ""),
                "source":  a.get("source", {}).get("name", "NewsAPI"),
                "summary": a.get("description", "") or "",
                "layer":   "newsapi",
            }
            for a in resp.json().get("articles", [])
            if a.get("title") and a.get("url")
        ]
    except requests.RequestException as exc:
        log.warning("NewsAPI error for %r: %s", query, exc)
        return []


# ── Layer 2: RSS / Google News ────────────────────────────────────────────────

def parse_rss(url: str, source_name: str = "") -> list[dict]:
    """Parse an RSS feed and return recent articles."""
    try:
        feed = feedparser.parse(
            url,
            request_headers={"User-Agent": "sports-fund-digest/1.0"},
        )
        out: list[dict] = []
        for entry in feed.entries:
            if not is_recent(entry.get("published_parsed")):
                continue
            out.append({
                "title":   entry.get("title", "").strip(),
                "url":     entry.get("link", ""),
                "source":  source_name or feed.feed.get("title", url),
                "summary": clean_html(entry.get("summary", "")),
                "layer":   "rss",
            })
        return out
    except Exception as exc:
        log.warning("RSS error %r: %s", url, exc)
        return []


def google_news_search(query: str) -> list[dict]:
    url = GOOGLE_NEWS_RSS.format(query=quote_plus(query))
    return parse_rss(url, "Google News")


# ── Layer 3: Perplexity Sonar ─────────────────────────────────────────────────

# Module-level timestamp so the rate limiter works across all calls in a run.
_perplexity_last_call: float = 0.0


def _perplexity_rate_limit() -> None:
    """Block until at least PERPLEXITY_RPS_DELAY seconds since the last call."""
    global _perplexity_last_call
    elapsed = time.monotonic() - _perplexity_last_call
    wait = PERPLEXITY_RPS_DELAY - elapsed
    if wait > 0:
        log.debug("Perplexity rate-limit: sleeping %.1fs", wait)
        time.sleep(wait)
    _perplexity_last_call = time.monotonic()


def _parse_perplexity_response(content: str, citations: list[str]) -> list[dict]:
    """
    Convert a Perplexity answer + citations list into article dicts.

    The model returns prose with inline [1], [2] … citation markers.
    We split on sentence boundaries and pair each cited sentence with its URL.
    Uncited sentences are surfaced as a single "summary" article if meaningful.
    """
    articles: list[dict] = []
    seen_urls: set[str] = set()

    # Match every sentence that ends with one or more [N] citation markers.
    # e.g. "Arctos Partners closed a $4 billion fund. [1][3]"
    sentence_re = re.compile(
        r"([^.!?\n]{20,}?)(\s*(?:\[\d+\])+)\s*[.!?]?",
        re.DOTALL,
    )

    for match in sentence_re.finditer(content):
        sentence  = match.group(1).strip()
        ref_block = match.group(2)
        indices   = [int(n) - 1 for n in re.findall(r"\[(\d+)\]", ref_block)]

        for idx in indices:
            if idx < 0 or idx >= len(citations):
                continue
            url = citations[idx]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Derive a tidy source name from the domain
            domain_match = re.search(r"https?://(?:www\.)?([^/]+)", url)
            source = domain_match.group(1) if domain_match else "Perplexity"

            articles.append({
                "title":   sentence[:200],
                "url":     url,
                "source":  f"{source} (via Perplexity)",
                "summary": sentence,
                "layer":   "perplexity",
            })

    # Emit any cited URLs that weren't paired to a sentence (fallback)
    for i, url in enumerate(citations):
        if url not in seen_urls:
            domain_match = re.search(r"https?://(?:www\.)?([^/]+)", url)
            source = domain_match.group(1) if domain_match else "Perplexity"
            articles.append({
                "title":   f"[Source {i+1}] {source}",
                "url":     url,
                "source":  f"{source} (via Perplexity)",
                "summary": "",
                "layer":   "perplexity",
            })

    return articles


def perplexity_search(fund_name: str) -> list[dict]:
    """
    Run a Perplexity Sonar query for a single fund.
    Returns article dicts or [] if the key is missing / the call fails.
    """
    if not PERPLEXITY_KEY:
        return []

    _perplexity_rate_limit()

    prompt = (
        f"Search for news published in the last 7 days about '{fund_name}' "
        f"in the context of sports private equity, sports investments, fund activity, "
        f"deals, fundraising, or management changes. "
        f"Focus on sources like Sports Business Journal, Financial Times, Bloomberg, "
        f"Sportico, PitchBook, and Mergermarket — including paywalled content. "
        f"List each distinct news item as a separate sentence. "
        f"Cite every source with an inline citation."
    )

    payload = {
        "model":    PERPLEXITY_MODEL,
        "messages": [
            {
                "role":    "system",
                "content": (
                    "You are a financial news research assistant specialising in "
                    "sports private equity. Return factual, citation-dense summaries. "
                    "Each distinct news item should be its own sentence with inline "
                    "citations. Do not speculate or add commentary."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "return_citations": True,
        "temperature":       0.1,
        "max_tokens":        1024,
    }

    headers = {
        "Authorization": f"Bearer {PERPLEXITY_KEY}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

    try:
        resp = requests.post(
            PERPLEXITY_BASE,
            json=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 429:
            log.warning("Perplexity rate-limited for fund: %s — skipping", fund_name)
            return []
        resp.raise_for_status()
        data = resp.json()

        content   = data["choices"][0]["message"]["content"]
        citations = data.get("citations", [])

        articles = _parse_perplexity_response(content, citations)
        log.info("  Perplexity → %d articles for '%s'", len(articles), fund_name)
        return articles

    except requests.RequestException as exc:
        log.warning("Perplexity HTTP error for '%s': %s", fund_name, exc)
        return []
    except (KeyError, IndexError, ValueError) as exc:
        log.warning("Perplexity parse error for '%s': %s", fund_name, exc)
        return []


# ── Per-fund aggregation ──────────────────────────────────────────────────────

def fetch_fund_news(fund: dict, priority_pool: list[dict]) -> list[dict]:
    """
    Aggregate all three layers for a single fund and return deduplicated articles.
    """
    name     = fund["name"]
    keywords = fund.get("keywords", [name])
    articles: list[dict] = []

    # --- Layer 1: NewsAPI (up to 2 keyword variants to conserve quota) ---
    for kw in keywords[:2]:
        articles += newsapi_search(f'"{kw}" (sports OR "private equity" OR "sports fund")')

    # --- Layer 2a: Google News RSS (up to 2 keyword variants) ---
    for kw in keywords[:2]:
        articles += google_news_search(f'"{kw}" sports')

    # --- Layer 2b: Filter shared priority-RSS pool for this fund ---
    name_lower = name.lower()
    for kw in keywords:
        kw_lower = kw.lower()
        for a in priority_pool:
            text = (a.get("title", "") + " " + a.get("summary", "")).lower()
            if kw_lower in text or name_lower in text:
                articles.append(a)

    # --- Layer 3: Perplexity Sonar ---
    articles += perplexity_search(name)

    return dedup(articles)


# ── Auto-detection of new funds ───────────────────────────────────────────────

def detect_new_funds(existing_names: set[str]) -> list[dict]:
    """
    Broad search for newly emerging sports PE firms not yet in funds.json.
    Uses NewsAPI + Google News + one Perplexity query.
    """
    candidates: list[dict] = []

    for query in AUTO_DETECT_QUERIES:
        candidates += newsapi_search(query)
        candidates += google_news_search(query)

    # One Perplexity sweep for newly launched sports funds
    if PERPLEXITY_KEY:
        _perplexity_rate_limit()
        payload = {
            "model": PERPLEXITY_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a financial research assistant. Return only factual, "
                        "citation-dense information about new sports-focused investment firms."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "List any brand-new sports private equity funds, sports investment firms, "
                        "or sports-focused vehicles launched or announced in the last 7 days. "
                        "Include fund name, founding team background, and AUM if available. "
                        "Cite every source inline."
                    ),
                },
            ],
            "return_citations": True,
            "temperature":       0.1,
            "max_tokens":        512,
        }
        headers = {
            "Authorization": f"Bearer {PERPLEXITY_KEY}",
            "Content-Type":  "application/json",
        }
        try:
            resp = requests.post(
                PERPLEXITY_BASE, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
            )
            if resp.ok:
                data      = resp.json()
                content   = data["choices"][0]["message"]["content"]
                citations = data.get("citations", [])
                candidates += _parse_perplexity_response(content, citations)
        except Exception as exc:
            log.warning("Perplexity auto-detect sweep failed: %s", exc)

    # --- Extract candidate firm names from article text ---
    firm_re = re.compile(
        r"(?:launch(?:es|ed)?|form(?:s|ed)?|debut(?:s|ed)?|rais(?:es|ed)?|found(?:s|ed)?|start(?:s|ed)?)"
        r".{0,60}(?:sports|sport).{0,40}(?:fund|firm|capital|partners|ventures|equity|management)",
        re.I,
    )
    name_re = re.compile(
        r"\b([A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]{2,}){1,4}"
        r"(?:\s+(?:Capital|Partners|Ventures|Fund|Equity|Sports|Management|Group|Advisors))?)\b",
    )

    new_funds: list[dict] = []
    seen_candidates: set[str] = set()

    for a in dedup(candidates):
        text = a.get("title", "") + " " + a.get("summary", "")
        if not firm_re.search(text):
            continue
        for m in name_re.finditer(text):
            candidate = m.group(1).strip()
            norm      = candidate.lower()
            if norm in seen_candidates:
                continue
            if len(candidate.split()) < 2 or len(candidate.split()) > 7:
                continue
            # Skip if it's already a tracked fund (substring match in either direction)
            if any(
                ex_name.lower() in norm or norm in ex_name.lower()
                for ex_name in existing_names
            ):
                continue
            seen_candidates.add(norm)
            new_funds.append(
                {
                    "name":           candidate,
                    "category":       "AUTO_DETECTED",
                    "keywords":       [candidate],
                    "date_added":     datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "auto_detected":  True,
                    "source_article": a.get("url", ""),
                }
            )
            log.info("Auto-detected potential new fund: %s", candidate)

    return new_funds


# ── HTML email builder ────────────────────────────────────────────────────────

CATEGORY_COLORS: dict[str, str] = {
    "NEW DEALS":          "#1a7a3a",
    "FUNDRAISING":        "#1a4a7a",
    "EXITS":              "#7a1a1a",
    "PEOPLE":             "#5a1a7a",
    "MARKET/REGULATORY":  "#7a4a1a",
    "VALUATIONS":         "#1a6a6a",
    "MEDIA RIGHTS":       "#7a6a1a",
    "GENERAL":            "#555555",
}

LAYER_LABELS: dict[str, str] = {
    "newsapi":    "NewsAPI",
    "rss":        "RSS",
    "perplexity": "Perplexity",
}


def _badge(label: str, color: str) -> str:
    return (
        f'<span style="display:inline-block;background:{color};color:#fff;'
        f'font-size:10px;font-weight:700;padding:2px 8px;border-radius:3px;'
        f'margin:2px 4px 2px 0;letter-spacing:0.4px;white-space:nowrap;">'
        f"{label}</span>"
    )


def _category_badge(cat: str) -> str:
    return _badge(cat, CATEGORY_COLORS.get(cat, "#6b7280"))


def _layer_dot(layer: str) -> str:
    colors = {"newsapi": "#3b82f6", "rss": "#22c55e", "perplexity": "#a855f7"}
    color  = colors.get(layer, "#6b7280")
    label  = LAYER_LABELS.get(layer, layer)
    return (
        f'<span style="display:inline-block;width:6px;height:6px;border-radius:50%;'
        f'background:{color};margin-right:4px;vertical-align:middle;"></span>'
        f'<span style="color:#64748b;font-size:10px;vertical-align:middle;">{label}</span>'
    )


def build_html(
    week_of: str,
    fund_sections: list[dict],
    new_fund_names: list[str],
    all_funds: list[dict],
    highlights: list[dict] | None = None,
) -> str:
    """Render the complete HTML email."""

    # ── HIGHLIGHTS section ─────────────────────────────────────────────────
    highlights_html = ""
    if highlights:
        # Priority label per category (short, fits on one line)
        cat_label: dict[str, str] = {
            "NEW DEALS":          "DEAL",
            "FUNDRAISING":        "FUNDRAISING",
            "EXITS":              "EXIT",
            "PEOPLE":             "PEOPLE",
            "MARKET/REGULATORY":  "REGULATORY",
            "VALUATIONS":         "VALUATION",
            "MEDIA RIGHTS":       "MEDIA RIGHTS",
            "GENERAL":            "NEWS",
        }
        rows = ""
        for h in highlights:
            fund   = h["fund_name"].replace("<", "&lt;").replace(">", "&gt;")
            title  = h["title"].replace("<", "&lt;").replace(">", "&gt;")
            url    = h["url"]
            clabel = cat_label.get(h["category"], h["category"])
            clr    = CATEGORY_COLORS.get(h["category"], "#555555")
            rows += f"""
<tr>
  <td style="padding:0 0 12px;vertical-align:top;width:12px;
             color:#c9a84c;font-size:14px;line-height:1.5;">&#9679;</td>
  <td style="padding:0 0 12px 8px;vertical-align:top;">
    <span style="background:{clr};color:#fff;font-size:9px;font-weight:700;
                 padding:2px 6px;border-radius:3px;letter-spacing:0.5px;
                 margin-right:7px;vertical-align:middle;">{clabel}</span>
    <strong style="color:#ffffff;font-size:13px;font-weight:800;">{fund}</strong>
    <span style="color:#aaaaaa;font-size:13px;"> &mdash; </span>
    <a href="{url}"
       style="color:#dddddd;font-size:13px;text-decoration:none;line-height:1.5;">{title}</a>
  </td>
</tr>"""

        highlights_html = f"""
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#1a1a2e;border-radius:8px;border-left:4px solid #c9a84c;
              margin-bottom:20px;">
  <tr><td style="padding:22px 24px 10px 24px;">
    <div style="color:#c9a84c;font-size:13px;font-weight:800;letter-spacing:0.3px;
                margin-bottom:16px;">&#128273; This Week&#39;s Highlights</div>
    <table width="100%" cellpadding="0" cellspacing="0">{rows}</table>
  </td></tr>
</table>"""

    # ── NEW THIS WEEK banner ───────────────────────────────────────────────
    new_banner = ""
    if new_fund_names:
        items = "".join(
            f'<tr><td style="color:#e2e8f0;font-size:13px;padding:3px 0;">&#8227; {n}</td></tr>'
            for n in new_fund_names
        )
        new_banner = f"""
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#1a1a1a;border-left:4px solid #c9a84c;border-radius:4px;margin-bottom:24px;">
  <tr><td style="padding:16px 20px;">
    <div style="color:#c9a84c;font-size:10px;font-weight:700;letter-spacing:1.5px;
                margin-bottom:10px;">&#9733; NEW FUNDS DETECTED THIS WEEK</div>
    <table cellpadding="0" cellspacing="0">{items}</table>
  </td></tr>
</table>"""

    # ── Fund sections ──────────────────────────────────────────────────────
    sections_html = ""
    for section in fund_sections:
        fund_name    = section["name"]
        cat_articles = section["category_articles"]
        cats_used    = sorted(cat_articles.keys())

        cat_badges_html = "".join(_category_badge(c) for c in cats_used)

        items_html = ""
        for cat in cats_used:
            arts = cat_articles[cat][:5]   # cap at 5 per category per fund
            items_html += (
                f'<tr><td style="padding:14px 0 4px;">'
                f'<div style="color:#888888;font-size:10px;font-weight:700;'
                f'letter-spacing:1.4px;margin-bottom:8px;">{cat}</div></td></tr>'
            )
            for i, a in enumerate(arts):
                title  = a.get("title", "").replace("<", "&lt;").replace(">", "&gt;")
                url    = a.get("url", "#")
                src    = a.get("source", "").replace("<", "&lt;").replace(">", "&gt;")
                layer  = a.get("layer", "")
                dot    = _layer_dot(layer)
                # Divider between items (not before the first)
                divider = (
                    '<tr><td style="padding:0 0 8px 16px;border-left:1px solid #2a2a2a;">'
                    '<div style="border-top:1px solid #2a2a2a;margin-bottom:8px;"></div>'
                    '</td></tr>'
                    if i > 0 else ""
                )
                items_html += f"""{divider}
<tr><td style="padding:0 0 10px 16px;border-left:1px solid #2a2a2a;">
  <a href="{url}"
     style="color:#ffffff;text-decoration:none;font-size:13px;line-height:1.6;
            font-weight:600;">{title}</a>
  <div style="margin-top:4px;">
    {dot}
    <span style="color:#c9a84c;font-size:11px;font-weight:500;margin-left:6px;">{src}</span>
  </div>
</td></tr>"""

        sections_html += f"""
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#1a1a1a;border-radius:8px;border-left:4px solid #c9a84c;
              margin-bottom:14px;">
  <tr><td style="padding:22px 24px 18px 24px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="padding-bottom:12px;">
          <span style="color:#ffffff;font-size:18px;font-weight:800;
                       letter-spacing:-0.2px;">{fund_name}</span>
          <div style="margin-top:8px;">{cat_badges_html}</div>
        </td>
      </tr>
      {items_html}
    </table>
  </td></tr>
</table>"""

    if not sections_html:
        sections_html = (
            '<p style="color:#64748b;text-align:center;padding:40px 0;">'
            "No significant news detected this week.</p>"
        )

    # ── Footer fund universe ───────────────────────────────────────────────
    fund_groups: dict[str, list[str]] = defaultdict(list)
    for f in all_funds:
        fund_groups[f.get("category", "OTHER")].append(f["name"])

    footer_rows = ""
    for grp, names in sorted(fund_groups.items()):
        grp_label = grp.replace("_", " ").title()
        name_cells = "".join(
            f'<span style="color:#888888;font-size:11px;">{n}</span>'
            f'<span style="color:#333333;">&nbsp;&bull;&nbsp;</span>'
            for n in sorted(names)
        )
        footer_rows += f"""
<tr><td style="padding:0 0 12px;">
  <div style="color:#555555;font-size:9px;font-weight:700;letter-spacing:1.5px;
              margin-bottom:4px;">{grp_label}</div>
  <div style="line-height:1.8;">{name_cells}</div>
</td></tr>"""

    # ── Legend ─────────────────────────────────────────────────────────────
    legend = (
        f'{_layer_dot("newsapi")}&nbsp;&nbsp;'
        f'{_layer_dot("rss")}&nbsp;&nbsp;'
        f'{_layer_dot("perplexity")}'
    )

    total = len(all_funds)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sports Fund Intel — Week of {week_of}</title>
</head>
<body style="margin:0;padding:0;background:#0f0f0f;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">

<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#0f0f0f;padding:32px 16px;">
<tr><td>
<table width="640" align="center" cellpadding="0" cellspacing="0"
       style="max-width:640px;width:100%;margin:0 auto;">

  <!-- HEADER -->
  <tr><td style="background:#0f3460;border-radius:12px 12px 0 0;padding:44px 40px 36px;">
    <div style="color:#c9a84c;font-size:10px;font-weight:700;letter-spacing:3px;
                margin-bottom:12px;">WEEKLY INTELLIGENCE DIGEST</div>
    <div style="color:#ffffff;font-size:34px;font-weight:800;
                letter-spacing:-0.5px;line-height:1.1;margin-bottom:8px;">
      &#9917; Sports Fund Intel
    </div>
    <div style="color:#94a3b8;font-size:15px;margin-bottom:16px;">Week of {week_of}</div>
    <div style="border-top:1px solid #1e3a5f;padding-top:14px;">{legend}</div>
  </td></tr>

  <!-- BODY -->
  <tr><td style="background:#0f0f0f;padding:28px 32px;">
    {highlights_html}
    {new_banner}
    {sections_html}
  </td></tr>

  <!-- FOOTER -->
  <tr><td style="background:#111111;border-radius:0 0 12px 12px;
                  padding:24px 32px;border-top:1px solid #222222;">
    <div style="color:#555555;font-size:9px;font-weight:700;letter-spacing:2px;
                margin-bottom:16px;">TRACKED UNIVERSE &mdash; {total} FUNDS</div>
    <table width="100%" cellpadding="0" cellspacing="0">{footer_rows}</table>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-top:1px solid #222222;margin-top:16px;">
      <tr><td style="padding-top:14px;color:#444444;font-size:11px;">
        Generated automatically every Monday 7&nbsp;AM&nbsp;ET via GitHub Actions.
        Sources: NewsAPI &bull; RSS &bull; Perplexity Sonar.
      </td></tr>
    </table>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


# ── Send email ────────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str) -> None:
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        log.error("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set — cannot send email")
        return

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            smtp.sendmail(GMAIL_ADDRESS, [RECIPIENT_EMAIL], msg.as_string())
        log.info("Email sent → %s", RECIPIENT_EMAIL)
    except smtplib.SMTPAuthenticationError:
        log.error("SMTP authentication failed — check GMAIL_ADDRESS and GMAIL_APP_PASSWORD")
        raise
    except smtplib.SMTPException as exc:
        log.error("SMTP error: %s", exc)
        raise


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info("Sports Fund Intelligence Digest — starting")
    log.info("Layers active: NewsAPI=%s | RSS=yes | Perplexity=%s",
             "yes" if NEWSAPI_KEY else "NO KEY",
             "yes" if PERPLEXITY_KEY else "NO KEY")
    log.info("=" * 60)

    week_of = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # 1. Load fund universe
    funds          = load_funds()
    existing_names = {f["name"] for f in funds}
    log.info("Loaded %d funds from funds.json", len(funds))

    # 2. Pre-fetch shared priority RSS pool (one pass, shared across all funds)
    log.info("Fetching priority RSS feeds …")
    priority_pool: list[dict] = []
    for feed_url, feed_name in PRIORITY_RSS_FEEDS:
        arts = parse_rss(feed_url, feed_name)
        log.info("  %-40s → %d articles", feed_name, len(arts))
        priority_pool.extend(arts)
    priority_pool = dedup(priority_pool)
    log.info("Priority RSS pool: %d deduplicated articles", len(priority_pool))

    # 3. Per-fund aggregation (all three layers)
    log.info("Processing %d funds …", len(funds))
    fund_sections: list[dict] = []

    for fund in funds:
        log.info("  → %s", fund["name"])
        articles = fetch_fund_news(fund, priority_pool)
        if not articles:
            log.info("     (no news this week)")
            continue

        cat_articles: dict[str, list[dict]] = defaultdict(list)
        for a in articles:
            cat = categorize(a.get("title", ""), a.get("summary", ""))
            cat_articles[cat].append(a)

        fund_sections.append(
            {"name": fund["name"], "category_articles": dict(cat_articles)}
        )
        log.info("     %d articles across %d categories", len(articles), len(cat_articles))

    log.info("%d / %d funds had news this week", len(fund_sections), len(funds))

    # 4. Auto-detect new funds
    log.info("Running auto-detection sweep for new funds …")
    detected       = detect_new_funds(existing_names)
    new_fund_names: list[str] = []

    if detected:
        for nf in detected:
            funds.append(nf)
            new_fund_names.append(nf["name"])
        save_funds(funds)
        log.info("%d new fund(s) added to funds.json", len(detected))
    else:
        log.info("No new funds detected this week")

    # 5. Pick highlights from all collected news
    highlights = pick_highlights(fund_sections)
    log.info("Selected %d highlight(s) for summary section", len(highlights))

    # 6. Build HTML and send
    html    = build_html(week_of, fund_sections, new_fund_names, funds, highlights)
    subject = f"\u26bd Sports Fund Intel \u2014 Week of {week_of}"

    log.info("Sending digest: %s", subject)
    send_email(subject, html)
    log.info("=" * 60)
    log.info("Done.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
