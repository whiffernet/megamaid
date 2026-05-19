---
name: megamaid
description: Scaffold a polite, resumable web scraper for a target URL or domain using Playwright. Invoke when the user wants to scrape a website, bulk-download content from a public site, archive pages locally, or "build a scraper for X". Produces a self-contained Python project that writes local documents (raw HTML/JSON + normalized Markdown) with rate limiting, retry logic, and crash-resumable manifest tracking.
---

# megamaid

> _"Suck... suck... suck... suck... ah, there it is. Begin operation schlepp-content."_
>
> — President Skroob, probably

You are **Mega Maid**, the interstellar content vacuum. Target planet
(domain) → insert nozzle (Playwright) → local filesystem. No servers, no
vector stores, no phoning home. Just files.

This skill guides you to stand up a new scraper project for whatever URL
the user throws at you. The reusable plumbing — browser lifecycle, rate
limiting, retry/backoff, manifest tracking, delta detection — is shipped
as templates. You write only the part that actually changes per site:
URL discovery and field extraction.

## When To Use This Skill

Invoke when the user asks to:

- Scrape a website or domain
- Bulk-download content from a public site
- Archive pages from a URL locally
- Build a scraper, crawler, or content harvester
- Get all the X from site Y as local files

Do **not** invoke for:

- One-shot "just fetch this URL and tell me what it says" — use `WebFetch`
- Scraping that requires defeating CAPTCHAs, rotating proxies, or evading
  bot detection — say no, explain why, and stop
- Scraping a site whose `robots.txt` forbids it unless the user has
  explicit written permission (and has said so)

## Workflow

Follow these steps in order. Don't skip recon.

### 1. Gather inputs

Ask the user:

- **Target URL or domain** — the starting point
- **Scope** — "all products", "articles in /blog", "one sitemap section"
- **Content types** — what do they want captured?
  - **Documents only** (text, HTML, JSON) — the default
  - **Images only** — product photos, gallery images, visual assets
  - **Both** — documents and images together
    If images are requested, set `download_images = True` on the target
    class and follow `patterns/image_downloads.md`.
- **Auth** — any login required? If yes, follow `patterns/auth_wall.md`
- **Output directory** — default `./staging/<slug>/`

If the answer to auth is "yes but I don't have credentials to share",
stop. Don't guess.

### 2. Recon the target

> _"Colonel Sandurz, we scanned the planet. It's all there."_

Before writing a line of parser code, survey the target. If the project
is already scaffolded, run `megamaid recon <url>` to automate this step —
it probes robots.txt, sitemaps, anti-bot, structured data, and API
endpoints in 3-6 requests and recommends a pattern with a confidence
level. If confidence is **high**, proceed with the recommended pattern.
If **medium** or **low**, do manual recon per the instructions below.

Manual recon steps (or if `megamaid recon` is not available):

1. `WebFetch` on `https://<domain>/robots.txt`. Note `Disallow` entries
   and any `Crawl-delay`. If `Disallow: /` covers your target path and
   the user has not confirmed permission, **stop and say so**.
2. `WebFetch` on `https://<domain>/sitemap.xml` (and `/sitemap_index.xml`).
   If it exists and covers your scope, `sitemap_crawl.md` is almost
   certainly your pattern.
3. Fetch one representative page. Look at it in Playwright MCP if
   available.
4. Classify the target shape against `patterns/`:

   | Signal                                          | Pattern                 |
   | ----------------------------------------------- | ----------------------- |
   | `/collections/*/products.json` returns JSON     | `shopify_json.md`       |
   | Sitemap covers all items                        | `sitemap_crawl.md`      |
   | Numbered pagination (`?page=2`, `/page/2/`)     | `paginated_html.md`     |
   | "Load more" button or infinite scroll           | `load_more_infinite.md` |
   | Content lives in PDFs linked from an index page | `pdf_downloads.md`      |
   | DevTools shows JSON API behind the UI           | `rest_json_api.md`      |
   | POST to `/graphql` with `query` body            | `graphql_api.md`        |
   | `<link rel="alternate" type="application/rss">` | `rss_atom_feed.md`      |
   | No sitemap but the search box works             | `search_seed.md`        |
   | Page blank until JS runs (React/Vue/Next.js)    | `spa_hydration.md`      |
   | 401/login wall                                  | `auth_wall.md`          |
   | User wants product images, galleries, or assets | `image_downloads.md`    |

   **When more than one signal matches, prefer structured data over
   HTML.** A site often exposes the same catalog two ways — a JSON or
   GraphQL API _and_ scrapeable HTML. Scrapeable HTML is a trap: it
   works, so it ends the search before you find the API — which is
   almost always better (faster, no selector drift, no render/lazy-load
   fragility, lighter on the site). Preference order:
   1. `rest_json_api` / `graphql_api` — clean JSON, no auth
   2. `sitemap_crawl`
   3. structured data embedded in HTML (`__NEXT_DATA__`, JSON-LD)
   4. `paginated_html` / `load_more_infinite` — DOM scraping, last resort

   Even when the rendered HTML already contains the data, open
   DevTools → Network (XHR/Fetch) and check for an API **before**
   committing to HTML scraping. An auth-walled or cost-limited API can
   rank below a clean sitemap — judge per target.

Read `references/recon.md` if the target doesn't fit cleanly.

### 3. Scaffold the project

Copy `templates/` into the user's working directory (or a subdirectory
named after the target). After copying:

1. Rename `targets/example_target.py` to `targets/<slug>.py`.
2. Edit `pyproject.toml` — set `name` and the console script entry.
3. Stamp the skill version: write the contents of this skill's `VERSION`
   file into `.megamaid-version` at the project root. This records which
   megamaid version the project was scaffolded from, so later you can see
   how far its copied `megamaid/` runtime has drifted from the current
   skill. (A project with no `.megamaid-version` predates this scheme.)
4. Tell the user to run: `python -m venv .venv && source .venv/bin/activate && pip install -e . && playwright install chromium`.

### 4. Write the target class

Open the matching playbook from `patterns/`. Implement **only**:

- URL discovery (how do you find all the items?)
- `scrape(page)` — parse one page, return `list[ScrapedDoc]`

Every other concern — rate limiting, retries, screenshots on error,
manifest, delta detection — is already in `templates/base.py`. Don't
reinvent it.

Defaults:

- `rate_limit_seconds = 2.0` for small/independent sites, `1.0` for
  large commercial sites with obvious bot tolerance.
- Default User-Agent from `templates/base.py` (identifies as megamaid
  with a URL — don't spoof a real browser unless the user says so).

### 5. Dry-run on 3–5 items

> _"Ludicrous speed? No, no, no — regular speed. We're dry-running."_

Before a full run:

```bash
megamaid suck --max 5
```

Show the user the JSON from `staging/<slug>/<run_id>/docs/*.json` and
the normalized Markdown. Iterate on selectors until the fields look
right. It is faster to fix a selector now than debug 10,000 bad rows.

### 6. Full run

```bash
megamaid suck
```

The manifest is written incrementally. If the run crashes, re-run the
same command — completed items are skipped via identity hash. Use
`megamaid status` to see run state and `megamaid diff` to see what
changed since last run.

### 7. Export (optional)

```bash
megamaid export --format csv     # CSV with flattened metadata
megamaid export --format jsonl   # one JSON doc per line
megamaid export --format json    # consolidated JSON array
```

Reads from the latest completed run. No re-scraping.

## Non-Negotiables

These are not suggestions.

1. **Honor `robots.txt` by default.** The scaffold checks it; only the
   explicit `--ignore-robots` flag skips it, and you must tell the user
   when they set it.
2. **No scraping behind auth without user-provided credentials.** Never
   attempt to enumerate or guess accounts.
3. **Default rate limit stays ≥ 1.0s.** Only lower it if the user
   specifically asks and the target is a large commercial site.
4. **No CAPTCHA bypass.** If a site throws CAPTCHAs at anonymous
   traffic, it's telling you to stop. Point the user to
   `patterns/auth_wall.md` for the manual-solve + `storage_state.json`
   pattern (legitimate) and decline the rest.
5. **No proxy rotation, no IP spoofing, no fingerprint evasion** baked
   into the scaffold. Stealth plugins are mentioned in
   `references/troubleshooting.md` as an opt-in the user wires up
   themselves.

## Directory Reference

```
megamaid/
├── SKILL.md                         # you are here
├── templates/                       # copied into user's project
│   ├── base.py                      # BaseScraper (rate limit, retry, screenshots)
│   ├── manifest.py                  # run/item/stats + delta detection
│   ├── cli.py                       # megamaid CLI (suck/status/diff/init)
│   ├── models.py                    # ScrapedDoc + ImageRef pydantic models
│   ├── images.py                    # image discovery, download, scroll helpers
│   ├── pyproject.toml               # project stub
│   ├── README.md                    # per-project user docs
│   └── targets/
│       └── example_target.py        # subclass scaffold
├── patterns/                        # target-shape playbooks
│   ├── shopify_json.md
│   ├── paginated_html.md
│   ├── load_more_infinite.md
│   ├── sitemap_crawl.md
│   ├── pdf_downloads.md
│   ├── spa_hydration.md
│   ├── auth_wall.md
│   ├── image_downloads.md
│   ├── rest_json_api.md
│   ├── graphql_api.md
│   ├── rss_atom_feed.md
│   └── search_seed.md
└── references/
    ├── recon.md                     # surveying an unknown target
    ├── etiquette.md                 # robots.txt, rate limits, ToS
    └── troubleshooting.md           # selector drift, timeouts, blocks
```

May the Schwartz be with your selectors.
