# megamaid

> _"Suck... suck... suck... suck... ah, there it is. Begin operation schlepp-content."_
>
> — President Skroob, probably

**megamaid** is a [Claude Code](https://claude.ai/claude-code) skill that scaffolds polite, resumable web scrapers. Point it at a URL, and it stands up a self-contained Python project that vacuums content into local files — raw HTML/JSON, normalized Markdown, and optionally images — with rate limiting, retry logic, and crash-resumable manifest tracking.

No servers. No vector stores. No phoning home. Just files.

## What it does

> _"Colonel Sandurz, we scanned the planet. It's all there."_

1. **Recons the target** — checks `robots.txt`, detects the site's framework (Shopify, Next.js, WordPress, static HTML), and classifies it into a scraping pattern.
2. **Scaffolds a project** — copies a working Python project with `BaseScraper`, manifest tracking, delta detection, and a CLI (`megamaid suck / status / diff / init`).
3. **Writes the target class** — the only bespoke part: URL discovery and field extraction, tailored to the detected pattern.
4. **Dry-runs** — scrapes 3–5 items so you can iterate on selectors before committing to a full run.
5. **Full run** — manifest-tracked, crash-resumable, with identity-hash delta detection on subsequent runs.

## Patterns

> _"Use the sitemap, Lone Starr. Use the sitemap."_

Nine target-shape playbooks, each with examples and gotchas:

| Pattern              | When to use                                     |
| -------------------- | ----------------------------------------------- |
| `shopify_json`       | Shopify stores with `/products.json` endpoints  |
| `paginated_html`     | Numbered pagination (`?page=2`, `/page/2/`)     |
| `sitemap_crawl`      | `sitemap.xml` covers your target URLs           |
| `load_more_infinite` | "Load more" buttons or infinite scroll          |
| `pdf_downloads`      | PDFs linked from an index page                  |
| `rest_json_api`      | Site has a JSON API behind the UI (skip HTML)   |
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

## Troubleshooting

> _"The radar's been jammed."_ _"Jammed? With what?"_ _"Raspberry jam, sir."_

Most scraper bugs are the equivalent of raspberry jam. See `references/troubleshooting.md` for selector drift, timeouts, blocks, and the stealth workaround for anti-bot CDNs.

## Installation

Copy this repo into your Claude Code skills directory:

```bash
git clone git@github.com:whiffernet/megamaid.git ~/.claude/skills/megamaid
```

The skill is auto-discovered by Claude Code via `SKILL.md`. Ask Claude to "scrape a website" or "build a scraper for X" and it will invoke megamaid.

## Requirements

> _"Ludicrous speed? No, no, no — regular speed. We're dry-running."_

Scaffolded projects need:

- **Python 3.11+**
- **Chromium browser** — Playwright downloads its own copy, separate from pip:
  ```bash
  pip install -e .               # installs playwright, bs4, pydantic, click, httpx, trafilatura
  playwright install chromium    # downloads the Chromium binary (~150 MB)
  ```

Optional (install only if your target needs them):

- `pypdf` — for the PDF downloads pattern
- `playwright-stealth` — for anti-bot protected sites
- `xvfb` — for stealth mode on headless Linux servers only (Mac/Windows don't need this)

## License

MIT

---

_May the Schwartz be with your selectors._
