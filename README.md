# DMC Competitor Finder

Competitive price intelligence for travel packages. Upload a full-page screenshot of one of your packages and the system extracts every inclusion, searches the web for competitors selling the same or closest package, verifies each competitor page, and tells you **who sells what, at which price, and what each side offers that the other does not**.

It runs as a single Streamlit process: no separate API server, no message queue, no external database. OpenAI is the only LLM vendor. Access is protected by a password.

---

## Table of contents

1. [Features](#features)
2. [How it works](#how-it-works)
3. [Match levels and price statistics](#match-levels-and-price-statistics)
4. [Gap analysis](#gap-analysis)
5. [Accuracy safeguards](#accuracy-safeguards)
6. [Quick start](#quick-start)
7. [Configuration](#configuration)
8. [Using the application](#using-the-application)
9. [Excel report](#excel-report)
10. [Deployment](#deployment)
11. [Architecture](#architecture)
12. [Security](#security)
13. [Cost](#cost)
14. [Testing](#testing)
15. [Limitations](#limitations)
16. [Troubleshooting](#troubleshooting)

---

## Features

- **Screenshot in, analysis out.** Upload a GoFullPage (or any full-page) PNG/JPG/WEBP of a package page. Works for any destination and any package structure, including multi-city routes (for example Beijing + Shanghai, or Bali + Kuala Lumpur).
- **Reviewable extraction.** Extracted details (destination, cities, nights, hotel class, price, transfers, flights, components) can be corrected before the search starts. The component list drives all matching.
- **Multi-engine web discovery.** OpenAI web search (with regional markets) and DuckDuckGo are queried together and merged. Listing pages are expanded to reach the individual package pages behind them.
- **Evidence-based verification.** Every competitor page is read and checked component by component. A component only counts when the page contains a verbatim quote that proves it.
- **Tiered matching.** Competitors are classified as Same, Near, Partial or Rejected, with the reason shown. Single activities (for example a stand-alone desert safari) are never treated as a package.
- **Always a price.** A competitor missing some of your inclusions is still listed with its price, together with the exact components it lacks.
- **Gap analysis.** For every competitor: which of your components they show, which they do not, and which extras they offer that you do not. A verdict states whether your price looks high, in line, or competitive for what you offer.
- **Analytics.** Price distribution, your rank and position versus the market, price versus coverage, seller types, and a component coverage heatmap.
- **Excel export.** A formatted multi-sheet workbook with live formulas, FX assumptions, evidence quotes and a gap analysis sheet.
- **Operational basics.** Password login with throttling, persistent history (SQLite), live progress and log, cancel/retry/delete, and clear error messages instead of stack traces.

---

## How it works

```
Screenshot ──► Extract ──► Review (optional) ──► Plan queries ──► Discover ──► Select pages
                                                                                    │
Excel ◄── Analytics ◄── Score & classify ◄── Expand listing links ◄── Verify pages ◄┘
```

| Stage | What happens |
|---|---|
| **1. Extract** | The screenshot is tiled into overlapping slices (width 1500 px, slice height 1700 px, 140 px overlap, up to 14 slices) and read by an OpenAI vision model. The result is a structured reference package: destination, cities, nights/days, hotel class, price and basis, transfers, flights, visa, and a normalized component list with a match rule per component. Transfers are consolidated into one component, meals attached to an activity stay part of that activity, and "A and B" items are split. |
| **2. Review** | Optional but recommended. Edit any field, add or remove components, or set a per-component match rule. |
| **3. Plan queries** | The model writes diverse search queries (exact signature items, "N nights N days package price", inbound/B2B DMC, regional markets). Template queries fill any gap. Multi-city routes are searched by their city names. |
| **4. Discover** | Every query runs on every enabled engine in parallel. The best queries are repeated per regional market (default IN, PK, AE, GB, SG) using OpenAI's location-aware search. One failing engine never stops the run. |
| **5. Select pages** | URLs are normalized (tracking parameters removed), de-duplicated and ranked by how many searches returned them. Your own domains, social networks, wikis, files and similar noise are dropped. At most 6 pages per domain are kept so one marketplace cannot crowd out others. |
| **6. Verify** | Each page is fetched (direct request, then Jina Reader for JavaScript-heavy or bot-blocking pages, optionally a headless browser) and analysed by the LLM: offer type, whole-package price, nights/days/stars, per-component status with quotes, and extras. All claims are then checked against the page text (see [Accuracy safeguards](#accuracy-safeguards)). |
| **7. Expand** | Listing pages that look like catalogues are followed: package-looking links on the same domain are ranked by destination, nights and signature words and verified in a second wave. |
| **8. Score** | Each offer is classified, its price is converted to USD per person, and its value verdict versus your package is computed. |
| **9. Analyse and export** | Statistics, coverage, extras and the price verdict are computed and stored with the run. The Excel workbook is generated on demand. |

Search depth presets:

| Depth | Queries | Pages verified | Linked pages | Regional markets |
|---|---|---|---|---|
| `fast` | 8 | 25 | 10 | none |
| `standard` (default) | 12 | 45 | 25 | 3 markets x top 3 queries |
| `deep` | 20 | 80 | 50 | 6 markets x top 4 queries |

Both the page and query limits can be overridden per run under *Advanced options*.

---

## Match levels and price statistics

No competitor sells a package that is identical in every detail, so the system ranks the closest packages and shows exactly how they differ.

| Level | Definition |
|---|---|
| **Same** | A whole multi-day package with accommodation and one price for everything, where **every** component of your package is proven on the page. Nights equal, hotel class not contradicting, transfer type not conflicting, no bundled flights. |
| **Near** | At least 70 percent of your components are proven (threshold depends on strictness), with no hard conflict such as different nights, different hotel class, or an explicitly excluded component. The missing items are listed. |
| **Partial** | A whole package with a price, but fewer components proven or a different length or hotel class. Shown with its price and missing items. |
| **Rejected** | Not a comparable package: a single activity or ticket, hotel-only or flight-only offer, a different destination, a page with a single-item price, or an unreadable page. The reason is recorded. |

**Strictness** (selected per run):

| Setting | Near threshold | Used in main statistics |
|---|---|---|
| `strict` | 80 percent | Same only; hotel class must be stated on the page |
| `standard` (default) | 70 percent | Same + Near |
| `relaxed` | 50 percent | Same + Near |

**Price statistics** (minimum, median, maximum, average, quartiles, your rank and position) use offers that meet all of the following:

- level is Same or Near (Same only in strict mode);
- the price is for the whole package and was found verbatim on the page;
- the price basis is resolvable (per person, per couple divided by two, or group/total divided by the traveller count);
- the page carries no "sold out / expired / not available" warning;
- flights are not bundled (unless the run option allows it).

If no Same or Near offer exists, the statistics fall back to every whole package with a verified price, and the interface states that this fallback is in use. A separate line always shows the range across all priced whole packages, including partial matches.

Prices very different from yours are flagged for double-checking but are not discarded, because a legitimately cheaper or more expensive competitor is exactly what you want to see.

---

## Gap analysis

The *Gap analysis* tab answers the question the tool exists for: **is my package priced correctly for what it contains?**

For each competitor:

| List | Meaning |
|---|---|
| **You both offer** | Your components proven on their page, with the supporting quote. |
| **You offer, they do not show** | Your components their page does not prove. "Excluded on their page" is marked separately from "not shown". |
| **They offer, you do not** | Extras on their page that your package lacks (extra tours, tickets, meals, nights, upgrades), each backed by a verbatim quote. |

Across all competitors:

- a bar chart of how many competitors show each of your components;
- the most common extras, with the median price of competitors that offer them;
- a competitor-by-competitor table with a one-line price-versus-content verdict, for example *"Cheaper by $74 (14%) and shows all your components and offers 1 extra you do not: Abu Dhabi city tour"* or *"Pricier by $120 (24%) and does not show 2 of your 11 components"*;
- an overall verdict computed from **like-for-like** competitors only (those that show everything you offer): *Your price looks HIGH*, *COMPETITIVE*, or *IN LINE with the market*. If no competitor shows all your components, the verdict compares price against reduced content instead.

"Not shown" means the competitor's page did not prove the item. They may still include it.

---

## Accuracy safeguards

The language model is treated as an untrusted extractor. Its output is verified deterministically:

- **Quotes required.** A component is marked present only when the model supplies a quote and that quote (a 6-word window for long quotes) is found in the fetched page text. Otherwise it is downgraded to "not stated" and flagged.
- **Prices must exist on the page.** The price must appear in the page text, tolerating thousands separators and Indian digit grouping. Unverified prices are displayed with a warning and excluded from statistics.
- **Nights, days and hotel class are grounded.** Values the model reports are accepted only if the number appears next to "nights", "days" or "star" on the page; otherwise they are dropped and flagged.
- **Offer type gates.** Only multi-day packages with an entire-package price can be Same or Near. Single activities and single-item prices are rejected.
- **Extras are quoted.** An extra without a verbatim quote, or one that merely rewords a component you already have, is discarded.
- **Currency handling.** Prices are converted to USD using live rates (open.er-api.com) with built-in fallback rates; per-couple and group prices are normalized per person. The FX snapshot is stored with each run and written into the workbook.

---

## Quick start

### Requirements

- Python 3.11 or newer
- An OpenAI API key with access to a vision-capable model and web search (default model `gpt-4.1`)

### Install and run

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt

copy .env.example .env            # macOS / Linux: cp .env.example .env
# edit .env: set OPENAI_API_KEY and APP_PASSWORD

streamlit run streamlit_app.py    # or run.bat / ./run.sh
```

Open the printed URL, sign in with `APP_PASSWORD`, upload a screenshot and follow the steps.

### Capturing a good screenshot

- Scroll the whole page first so every section (Services, Itinerary, Meals, Accommodation) has loaded.
- Expand "Read more" and collapsed sections before capturing.
- Capture with GoFullPage as PNG. A screenshot is more reliable than a page link, because many package pages are rendered by JavaScript and expose no readable text to a crawler.

---

## Configuration

All settings are read from environment variables or `.env` (locally) and from Streamlit Secrets (when deployed). See `.env.example`.

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | none (required) | OpenAI API key. |
| `OPENAI_MODEL` | `gpt-4.1` | Model used for vision extraction, query planning, page analysis and web search. |
| `OPENAI_SEARCH_CONTEXT` | `high` | Web search context size: `low`, `medium` or `high`. |
| `APP_PASSWORD` | none (required) | Shared login password. |
| `APP_PASSWORD_SHA256` | none | Alternative: SHA-256 hex digest of the password, so the plain text is never stored. |
| `APP_AUTH_DISABLED` | `false` | Disable the login (local testing only). |
| `ENABLE_OPENAI_SEARCH` | `true` | Use OpenAI web search. |
| `ENABLE_DUCKDUCKGO` | `true` | Use DuckDuckGo (no key needed). |
| `SEARCH_COUNTRIES` | `IN,PK,AE,GB,SG` | Regional markets for location-aware searches. Maximum 10 two-letter codes; a longer or invalid list is ignored and the default is used, to keep search cost bounded. |
| `OWN_DOMAINS` | `ubidtours.com,travelomatix.com` | Domains never reported as competitors. |
| `VERIFY_WORKERS` | `6` | Parallel page verifications. |
| `FETCH_TIMEOUT_SECONDS` | `20` | Per-page download timeout. |
| `JINA_FALLBACK` | `true` | Retry unreadable pages through Jina Reader. |
| `FETCH_RENDER` | `false` | Also try a headless Chromium render (requires `pip install playwright` and `playwright install chromium`). |
| `LIVE_FX` | `true` | Fetch live exchange rates; falls back to built-in rates when offline. |
| `PRICE_OUTLIER_LOW` / `PRICE_OUTLIER_HIGH` | `0.35` / `3.0` | Prices below or above these multiples of yours are flagged for checking (they are not excluded). |
| `DB_PATH` | `data/dmc_finder.db` | SQLite database file; uploads are stored next to it. |
| `MAX_UPLOAD_MB` | `25` | Maximum screenshot size. |
| `LOG_LEVEL` | `INFO` | Python log level. |

Settings that apply per run (depth, strictness, page and query limits, flight handling, extra queries) are chosen in the interface.

---

## Using the application

1. **New analysis** - upload a screenshot, choose review on/off, depth and strictness, then *Analyse screenshot*.
2. **Review** - check the extracted fields and component table. Edit, add or remove components; optionally add a match rule per component. Then *Save and search the internet*.
3. **Progress** - a live progress bar and log. The run continues if you leave the page; reopen it from the sidebar history. *Cancel* stops it.
4. **Results**
   - Headline metrics: your price, market median (and your delta), cheapest, most expensive, your rank, and offers in the statistics.
   - **Price vs content** banner with the like-for-like verdict.
   - **📊 Analytics** - price per competitor, price versus coverage, seller types, match-level counts, component coverage heatmap.
   - **🏷️ Competitors** - filterable table with price, match level, components, missing items, extras, flags and source link; evidence quotes per offer.
   - **🧩 Gap analysis** - component coverage, common extras, competitor table and side-by-side comparison.
   - **🔗 Pages to check** - pages found but not readable automatically (blocked, JavaScript-only, timeout).
   - **📦 Your package** - the reference as extracted.
   - **🪵 Log** - the full run log.
5. **Export to Excel**, **Search again** (re-runs the search with the same reference), or delete the run.

---

## Excel report

| Sheet | Content |
|---|---|
| **Summary** | Your price, counts, min / max / median / average, your delta, percentage and rank (all live formulas), match-level counts, and the range across all priced whole packages. |
| **Closest Matches** | Same and Near competitors with price, USD conversion, per-component Yes/No, match score, missing components, extras, price-versus-content note, flags and source link. |
| **Partial Matches** | Whole packages with a price but fewer components or a different length or class. |
| **Gap Analysis** | Component coverage, common extras, and a competitor-by-competitor comparison with the items each side lacks. |
| **Rejected** | Pages that were read and rejected, with the reason. |
| **Pages To Check Manually** | Pages that could not be read. |
| **Evidence** | Verbatim quotes proving each component for each competitor. |
| **Reference Package** | Your package as extracted, including the component list. |
| **Assumptions** | FX table, price-basis rules, run metadata and method notes. |

USD columns are formulas driven by the FX table on the *Assumptions* sheet; blue cells are inputs you can change and the workbook recalculates. Cached values are included, so previews show numbers without recalculation.

---

## Deployment

### Streamlit Community Cloud

1. Push the repository to GitHub (a private repository is recommended; never commit `.env` or `data/`).
2. In [share.streamlit.io](https://share.streamlit.io) create a new app: choose the repository and set the main file to `streamlit_app.py`.
3. Under **App settings > Secrets**, paste the contents of `.streamlit/secrets.toml.example` and fill in `OPENAI_API_KEY` and `APP_PASSWORD`.
4. Deploy.

Notes: the Community Cloud filesystem is ephemeral, so run history stored in SQLite can reset when the app restarts (download Excel reports you need). Login is per browser session; a page refresh asks for the password again.

### Docker

```bash
docker build -t dmc-finder .
docker run -p 8501:8501 --env-file .env -v dmc_data:/app/data dmc-finder
```

The volume keeps the database and uploaded screenshots across restarts.

### Your own server or VM

Run `streamlit run streamlit_app.py --server.address 0.0.0.0 --server.port 8501` behind a reverse proxy with TLS, and point `DB_PATH` at a persistent directory.

### Pushing to GitHub

```bash
git init
git branch -M main
git add .
git status                       # confirm .env and data/ are not listed
git commit -m "Initial commit"
git remote add origin https://github.com/<user>/<repo>.git
git push -u origin main
```

---

## Architecture

```
streamlit_app.py        Streamlit UI (analysis pages, charts, tabs)
ui/auth.py              Password gate with throttling
app/
  bootstrap.py          One shared AppService per server process
  core.py               Application service: create, review, start, retry, cancel, delete, export
  config.py             Settings from environment / .env / Streamlit secrets
  models.py             Pydantic models: ReferencePackage, Component, Offer, options
  db.py                 SQLite persistence (analyses, offers, events, fx_rates), WAL mode
  errors.py             Typed application errors
  logging_setup.py      Logging configuration
  services/
    imageprep.py        Screenshot validation and tiling
    llm.py              OpenAI client: vision, JSON text, web search, error mapping
    extractor.py        Screenshot to ReferencePackage
    search_providers.py OpenAI search + DuckDuckGo, merged in parallel
    searcher.py         Query planning, discovery, URL normalization and selection
    fetcher.py          Page download, text extraction, Jina fallback, link extraction
    verifier.py         Per-page offer extraction and deterministic grounding
    matcher.py          Gates, match levels, USD normalization, value verdicts
    analytics.py        Statistics, coverage, extras, price verdict
    exporter.py         Excel workbook (XlsxWriter)
    currency.py         FX rates (live with fallback) and conversion
    pipeline.py         Orchestration and background job runner
tests/                  Unit, integration and browser tests (fake LLM, search and fetcher)
```

**Concurrency model.** The application runs in one Streamlit process. Analyses run in background threads owned by a process-wide singleton; progress and results are stored in SQLite and the interface polls them, so a run survives page refreshes and browser disconnects. Runs interrupted by a server restart are marked as failed and can be retried.

**Persistence.** SQLite with WAL mode and per-call connections. Tables: `analyses`, `offers`, `events`, `fx_rates`. Uploaded screenshots are stored beside the database.

**Error handling.** Provider failures are mapped to typed errors with actionable messages (invalid key, exhausted quota, unavailable model, rate limit, network). Individual engine, page or query failures degrade the run gracefully; only a total failure aborts it.

---

## Security

- Access requires a shared password (`APP_PASSWORD` or `APP_PASSWORD_SHA256`), compared in constant time. After 5 failed attempts a session is locked for 60 seconds, and a global limiter slows distributed guessing.
- The application refuses to serve without a configured password unless `APP_AUTH_DISABLED=true` is set explicitly.
- Secrets live in environment variables or Streamlit Secrets, never in code. `.gitignore` excludes `.env`, `data/` and `secrets.toml`.
- Uploads are validated as real PNG/JPEG/WEBP images, size-limited, and renamed to server-generated IDs. Analysis IDs are validated before any lookup.
- Competitor pages are fetched with size limits and timeouts, and their content is treated as untrusted text.
- If a key is ever committed or shared by mistake, rotate it immediately.

---

## Cost

Typical cost on `gpt-4.1` is roughly **$1 to $1.5 per standard run** (about 40 pages verified). OpenAI web search is billed per call (regional searches are a few cents each), so `deep` runs and many regional markets cost more. Check OpenAI's pricing page for current rates. `SEARCH_COUNTRIES` is capped at 10 markets to keep costs predictable.

---

## Testing

```bash
pip install -r requirements.txt
python -m pytest
ruff check .
```

The suite covers price and evidence grounding, match levels and strictness, the single-activity rejection, extras and value verdicts, search-engine merging and failure tolerance, the OpenAI client (request shape, citations, error mapping) through a fake transport, the end-to-end pipeline with fake LLM/search/fetcher, authentication, and the Excel export. When LibreOffice is installed, the workbook is recalculated independently and compared with the cached values. Browser tests drive the real Streamlit UI against fakes.

Continuous integration is defined in `.github/workflows/ci.yml` (lint and tests).

---

## Limitations

- Prices are what each public page displays (usually "from" prices). They are not live quotes and may not apply to your travel dates.
- DMC net rates are normally behind agent logins and cannot be found by public search.
- JavaScript-heavy or bot-protected pages are retried through Jina Reader; pages that still cannot be read are listed under *Pages to check*.
- Components not stated on a competitor's page are reported as "not shown", which is not proof that the competitor excludes them.
- Result quality depends on the extracted component list; reviewing it before searching is recommended.
- The login is a single shared password, not per-user accounts.

---

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| "OPENAI_API_KEY is not set" | Set it in `.env` or Streamlit Secrets and restart. |
| "OpenAI quota/credit is exhausted" | Add billing credit in the OpenAI dashboard. |
| "Model not found" | Set `OPENAI_MODEL` to a model your key can use (for example `gpt-4.1` or `gpt-4o`). |
| App shows "Login is not configured" | Set `APP_PASSWORD` (or `APP_PASSWORD_SHA256`). |
| Few or no competitors | Use `deep` depth, add extra search queries, review the component list (too many or too specific components lowers matches), and check the *Rejected* and *Pages to check* tabs. |
| Many unverified prices | The page does not contain the number as text (often image-based prices). Open the source link to check manually. |
| DuckDuckGo returns no results | Occasional rate limiting; OpenAI search still runs and the run continues. |
| History disappeared on Streamlit Cloud | The Cloud filesystem is ephemeral. Download reports, or deploy with a persistent volume. |
| Excel shows no numbers in a viewer | Open it in Excel or LibreOffice; cached values are included for most viewers. |
