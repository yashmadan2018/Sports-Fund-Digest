# Sports Fund Intelligence Digest

A GitHub Actions workflow that runs every Monday at 7 AM ET, scrapes sports private equity news across three data layers, and emails a formatted HTML digest to your Gmail.

---

## How it works

| Layer | Source | Notes |
|---|---|---|
| 1 | **NewsAPI.org** | Keyword search per fund, 7-day window |
| 2 | **RSS / Google News** | Sportico, SBJ, Bloomberg, Reuters, WSJ + per-fund Google News feed |
| 3 | **Perplexity Sonar** | `llama-3.1-sonar-large-128k-online` — catches paywalled SBJ, FT, Bloomberg |

New funds are auto-detected weekly and appended to `funds.json`, which is committed back to the repo so the tracked universe grows automatically.

---

## Setup (one-time, ~10 minutes)

### Step 1 — Fork or clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/sports-fund-digest.git
cd sports-fund-digest
```

Then push to your own GitHub repo if you cloned it:

```bash
git remote set-url origin https://github.com/YOUR_USERNAME/sports-fund-digest.git
git push -u origin main
```

---

### Step 2 — Get a NewsAPI key (free)

1. Go to [newsapi.org](https://newsapi.org) and click **Get API Key**
2. Sign up for the free Developer plan (100 requests/day)
3. Copy your API key — you'll add it as a secret in Step 5

> The script falls back to RSS + Perplexity only if the key is missing or the quota is exhausted. It will not crash.

---

### Step 3 — Get a Perplexity API key

1. Go to [perplexity.ai/settings/api](https://www.perplexity.ai/settings/api)
2. Create an account if needed, then generate an API key
3. The script uses model `llama-3.1-sonar-large-128k-online` and rate-limits itself to 1 request per 3 seconds
4. Copy the key for Step 5

> If the key is missing, the Perplexity layer is silently skipped — NewsAPI and RSS still run.

---

### Step 4 — Create a Gmail App Password

Gmail requires an App Password (not your regular password) for SMTP access.

1. Go to your Google Account → **Security**
2. Enable **2-Step Verification** if not already on
3. Search for **App Passwords** (or go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords))
4. Choose app: **Mail** / device: **Other** → type `Sports Fund Bot`
5. Click **Generate** — copy the 16-character password shown
6. You'll use your full Gmail address (`you@gmail.com`) and this app password in Step 5

---

### Step 5 — Add GitHub Secrets

In your GitHub repo go to **Settings → Secrets and variables → Actions → New repository secret** and add each of the following:

| Secret name | Value |
|---|---|
| `NEWSAPI_KEY` | Your NewsAPI key from Step 2 |
| `PERPLEXITY_API_KEY` | Your Perplexity key from Step 3 |
| `GMAIL_ADDRESS` | Your full Gmail address, e.g. `you@gmail.com` |
| `GMAIL_APP_PASSWORD` | The 16-character app password from Step 4 |
| `RECIPIENT_EMAIL` | The address to send the digest to (can be the same Gmail or any address) |

> `RECIPIENT_EMAIL` is optional — if omitted, the digest is sent to `GMAIL_ADDRESS`.

---

### Step 6 — Trigger a manual test run

1. In your repo, click the **Actions** tab
2. Select **Weekly Sports Fund Digest** from the left sidebar
3. Click **Run workflow** → **Run workflow**
4. Watch the logs in real time — look for `Email sent →` near the end
5. Check your inbox (and spam folder the first time) for the digest

The scheduled run fires automatically every **Monday at 12:00 UTC (7:00 AM ET)**.

---

## Adding funds manually to funds.json

Open `funds.json` and append an entry to the `"funds"` array:

```json
{
  "name": "New Fund Name",
  "category": "DEDICATED_SPORTS_FIRST_US",
  "keywords": ["New Fund Name", "New Fund alternate keyword"],
  "date_added": "2026-04-13",
  "auto_detected": false
}
```

**Category values used in the seed list:**

| Value | Meaning |
|---|---|
| `DEDICATED_SPORTS_FIRST_US` | US-based funds whose primary mandate is sports |
| `LARGE_CAP_SPORTS_VEHICLE` | Large PE firms with a dedicated sports vehicle |
| `EU_INTERNATIONAL` | European / international sports funds |
| `SPORTS_ADJACENT` | Operators and holding companies active in sports investment |
| `AUTO_DETECTED` | Added automatically by the weekly sweep |

**Keywords tips:**
- Add 1–3 variants the fund is commonly referred to by (abbreviations, formal vs. informal names)
- More specific keywords reduce false-positive matches — prefer `"Arctos Sports"` over just `"Arctos"`

Commit and push the change; the next run will pick it up immediately.

---

## File structure

```
sports-fund-digest/
├── .github/
│   └── workflows/
│       └── weekly_digest.yml   # GitHub Actions schedule + dispatch
├── digest.py                   # Main script (all three data layers)
├── funds.json                  # Tracked fund universe (grows automatically)
├── requirements.txt            # Python dependencies
└── README.md
```

---

## Error handling reference

| Failure | Behaviour |
|---|---|
| NewsAPI quota exhausted (429) | Falls back to RSS + Perplexity; logs a warning |
| RSS feed unreachable | Skips that feed; logs a warning; others continue |
| Perplexity key missing | Layer 3 silently skipped; no crash |
| Perplexity rate-limited (429) | Skips that fund's Perplexity query; logs a warning |
| Fund has zero news | Omitted from email silently |
| SMTP auth failure | Logged as error; workflow exits non-zero so GitHub flags it |

---

## Dependencies

| Package | Purpose |
|---|---|
| `feedparser` | Parse RSS / Atom feeds |
| `requests` | HTTP calls (NewsAPI, Perplexity, raw feeds) |
| `beautifulsoup4` | Strip HTML tags from RSS summaries |
| `lxml` | Fast HTML parser backend for BeautifulSoup |
