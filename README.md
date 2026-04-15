# megamaid

> _"She's gone from suck... to blow!"_
>
> We only do the first part.

**megamaid** is a [Claude Code](https://claude.ai/claude-code) skill that scaffolds polite, resumable web scrapers. Point it at a URL, and it stands up a self-contained Python project that vacuums content into local files — raw HTML/JSON, normalized Markdown, and optionally images — with rate limiting, retry logic, and crash-resumable manifest tracking.

No servers. No vector stores. No phoning home. Just files.

## What it does

1. **Recons the target** — checks `robots.txt`, detects the site's framework (Shopify, Next.js, WordPress, static HTML), and classifies it into a scraping pattern.
2. **Scaffolds a project** — copies a working Python project with `BaseScraper`, manifest tracking, delta detection, and a CLI (`megamaid suck / status / diff / init`).
3. **Writes the target class** — the only bespoke part: URL discovery and field extraction, tailored to the detected pattern.
4. **Dry-runs** — scrapes 3–5 items so you can iterate on selectors before committing to a full run.
5. **Full run** — manifest-tracked, crash-resumable, with identity-hash delta detection on subsequent runs.

## Patterns

Eight target-shape playbooks, each with examples and gotchas:

| Pattern              | When to use                                     |
| -------------------- | ----------------------------------------------- |
| `shopify_json`       | Shopify stores with `/products.json` endpoints  |
| `paginated_html`     | Numbered pagination (`?page=2`, `/page/2/`)     |
| `sitemap_crawl`      | `sitemap.xml` covers your target URLs           |
| `load_more_infinite` | "Load more" buttons or infinite scroll          |
| `pdf_downloads`      | PDFs linked from an index page                  |
| `spa_hydration`      | JS-rendered SPAs (React, Vue, Next.js)          |
| `auth_wall`          | Content behind a login (manual session capture) |
| `image_downloads`    | Product photos, galleries, visual assets        |

## Output

```
staging/<target>/<run_id>/
├── raw/           # original HTML/JSON per item
├── docs/          # normalized ScrapedDoc JSON per item
├── images/        # downloaded images (content-hash filenames, auto-deduped)
├── debug/         # error screenshots
└── manifest.json  # run state, identity hashes, delta detection
```

## Non-negotiables

> _"Evil will always triumph because good is dumb."_ — Dark Helmet.
>
> Prove him wrong. Scrape politely.

- Honors `robots.txt` by default. `--ignore-robots` is opt-in.
- Default rate limit >= 1 second. 2 seconds for small/independent sites.
- No CAPTCHA bypass. No proxy rotation. No fingerprint evasion baked in.
- No scraping behind auth without user-provided credentials.

## Installation

Copy this repo into your Claude Code skills directory:

```bash
git clone git@github.com:whiffernet/megamaid.git ~/.claude/skills/megamaid
```

The skill is auto-discovered by Claude Code via `SKILL.md`. Ask Claude to "scrape a website" or "build a scraper for X" and it will invoke megamaid.

## Requirements

Scaffolded projects need:

- Python 3.11+
- Playwright (`playwright install chromium`)
- Dependencies installed via `pip install -e .` in the scaffolded project

## License

MIT

---

_May the Schwartz be with your selectors._
