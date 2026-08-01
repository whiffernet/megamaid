# megamaid

![Megamaid — orbital vacuum platform, hyperrealistic sci-fi render](assets/megamaid.jpg)

> _"Suck... suck... suck... suck... ah, there it is. Begin operation schlepp-content."_
>
> — President Skroob, probably

**megamaid** comes in two flavors:

- **[Claude Code skill](#installation)** — Claude reads the pattern playbooks and writes a bespoke scraper for your target. Interactive, code-generating, one site at a time.
- **[MCP server](#installation)** — exposes `megamaid_recon` and `megamaid_run` as callable tools for agents, automation workflows, and scripts. No Claude session required.

Both ship from the same repo. Both produce the same output: self-contained Python projects that vacuum content into local files — raw HTML/JSON, normalized Markdown, and optionally images — with rate limiting, retry logic, and crash-resumable manifest tracking.

No databases. No vector stores. No phoning home. Just files.

## What it does

> _"Colonel Sandurz, we scanned the planet. It's all there."_

1. **Recons the target** — `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/launch.py" --cli recon <url>` probes robots.txt, sitemaps, anti-bot systems, and API markers in 3–6 requests and returns a recommended pattern with a confidence level. (A plugin install puts nothing on PATH — see [Running megamaid commands](#running-megamaid-commands).)
2. **Scaffolds a project** — copies a working Python project with `BaseScraper`, manifest tracking, delta detection, and a CLI (`megamaid recon / suck / status / diff / init`).
3. **Writes the target class** — the only bespoke part: URL discovery and field extraction, tailored to the detected pattern.
4. **Dry-runs** — scrapes 3–5 items so you can iterate on selectors before committing to a full run.
5. **Full run** — manifest-tracked, crash-resumable, with identity-hash delta detection on subsequent runs.

## Patterns

> _"Use the sitemap, Lone Starr. Use the sitemap."_

Twelve target-shape playbooks, each with examples and gotchas:

| Pattern              | When to use                                     |
| -------------------- | ----------------------------------------------- |
| `shopify_json`       | Shopify stores with `/products.json` endpoints  |
| `paginated_html`     | Numbered pagination (`?page=2`, `/page/2/`)     |
| `sitemap_crawl`      | `sitemap.xml` covers your target URLs           |
| `load_more_infinite` | "Load more" buttons or infinite scroll          |
| `pdf_downloads`      | PDFs linked from an index page                  |
| `rest_json_api`      | Site has a JSON API behind the UI (skip HTML)   |
| `graphql_api`        | POST-to-`/graphql` with query bodies            |
| `rss_atom_feed`      | Site publishes RSS 2.0 or Atom 1.0 feeds        |
| `search_seed`        | No sitemap, but the search box returns results  |
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

## Compared to other free scrapers

> _"We're not just doing the job. We're doing it better."_
>
> — Lone Starr, once, probably

Measured against the two biggest general-purpose open-source scraping frameworks — [Scrapy](https://github.com/scrapy/scrapy) (the Python classic, ~55k stars) and [Crawl4AI](https://github.com/unclecode/crawl4ai) (the LLM-era newcomer, ~58k stars) — megamaid is narrower in scope but opinionated about the workflow around a scrape, not just the fetch itself.

| Feature                                 | megamaid | Scrapy | Crawl4AI |
| --------------------------------------- | :------: | :----: | :------: |
| Zero-code target scaffold (AI-written)  |    ✅    |        |          |
| Pre-built pattern playbooks             |    ✅    |        |          |
| Automated site recon (`megamaid recon`) |    ✅    |        |          |
| Identity-hash delta detection           |    ✅    |        |          |
| MCP server (agents, workflows, scripts) |    ✅    |        |          |
| Content-aware HTML → Markdown output    |    ✅    |        |    ✅    |
| Headless browser rendering built-in     |    ✅    |        |    ✅    |
| Crash-resumable manifest                |    ✅    |   ✅   |          |
| Image download with resolution dedup    |    ✅    |   ✅   |          |
| CLI for operations (run/status/diff)    |    ✅    |   ✅   |          |
| robots.txt honored by default           |    ✅    |   ✅   |    ✅    |
| Rate limiting + retry backoff           |    ✅    |   ✅   |    ✅    |
| Local-first output (no DB or cloud)     |    ✅    |   ✅   |    ✅    |

Scrapy wins on ecosystem depth (middlewares, pipelines, distributed crawling via Scrapyd). Crawl4AI wins on LLM-native extraction and speed. megamaid wins on "I want a working scraper for this one site by the end of the afternoon, and I want it to still work next month."

## Examples

See [`EXAMPLES.md`](EXAMPLES.md) for end-to-end walkthroughs: downloading product images (Lego at Walmart), archiving PDFs (FDA drug labels), following an RSS feed to a growing local archive, plus three MCP examples — agent sub-tool, scheduled workflow monitoring, and a plain Python cron script.

## Troubleshooting

> _"The radar's been jammed."_ _"Jammed? With what?"_ _"Raspberry jam, sir."_

Most scraper bugs are the equivalent of raspberry jam. See `skills/megamaid/references/troubleshooting.md` for selector drift, timeouts, blocks, and the stealth workaround for anti-bot CDNs.

## Installation

> _"One command. Even I can do it, and I'm half-dog."_
>
> — Barf

```bash
claude plugin marketplace add whiffernet/megamaid && claude plugin install megamaid@whiffernet
```

That is the whole install. It registers the skill, the `/megamaid-doctor` command, and the
`megamaid` MCP server. Ask Claude to "scrape a website" and the skill takes over.

**Requires:** Python 3.11+ and git. No Docker, no `uv`, no tokens, no config files to edit.

On the MCP server's first start, a small virtualenv is built under
`~/.local/state/megamaid/` (9–14 seconds depending on pip cache warmth; roughly 70 packages,
no browser download). It is stamped with the plugin version, so `claude plugin update megamaid`
refreshes it automatically on the next start.

### Running megamaid commands

A plugin install puts nothing on your PATH. There are two command forms, and they are not
interchangeable:

**URL-scoped** (`recon`, `map`, `init`) — no project needed, run from anywhere via the
launcher:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/launch.py" --cli recon https://example.com
```

**Project-scoped** (`suck`, `status`, `diff`, `export`) — run inside a scraped project
directory, where `targets/` and `.venv/` live:

```bash
.venv/bin/megamaid suck
```

If you want a short name for the launcher form, symlink it — optional, and required by
nothing:

```bash
ln -s ~/.local/state/megamaid/venv/bin/megamaid ~/.local/bin/megamaid
```

### Something wrong?

Run `/megamaid-doctor` in Claude Code. It reads `~/.local/state/megamaid/launch.log` and
explains the last failure — including the case where Claude Code killed a slow first build,
which leaves nothing on screen but "Failed to connect".

### Using the MCP server without Claude Code

The server is a normal console script, so any MCP client can spawn it over stdio:

```bash
pipx install "git+https://github.com/whiffernet/megamaid@v0.9.0#egg=megamaid[mcp]"
megamaid-mcp
```

## License

MIT

---

_May the Schwartz be with your selectors._
