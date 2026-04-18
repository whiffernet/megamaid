# megamaid

> _"Suck... suck... suck... suck... ah, there it is. Begin operation schlepp-content."_
>
> — President Skroob, probably

**megamaid** comes in two flavors:

- **[Claude Code skill](#installation--as-a-claude-code-skill)** — Claude reads the pattern playbooks and writes a bespoke scraper for your target. Interactive, code-generating, one site at a time.
- **[MCP server](#installation--as-an-mcp-server-megamaid-mcp)** — exposes `megamaid_recon` and `megamaid_run` as callable tools for agents, n8n workflows, and scripts. No Claude session required.

Both ship from the same repo. Both produce the same output: self-contained Python projects that vacuum content into local files — raw HTML/JSON, normalized Markdown, and optionally images — with rate limiting, retry logic, and crash-resumable manifest tracking.

No databases. No vector stores. No phoning home. Just files.

## What it does

> _"Colonel Sandurz, we scanned the planet. It's all there."_

1. **Recons the target** — run `megamaid recon <url>` to probe robots.txt, sitemaps, anti-bot systems, and API markers in 3–6 requests and get a recommended pattern with confidence level.
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
| Crash-resumable manifest                |    ✅    |   ✅   |          |
| Content-aware HTML → Markdown output    |    ✅    |        |    ✅    |
| Headless browser rendering built-in     |    ✅    |        |    ✅    |
| Image download with resolution dedup    |    ✅    |   ✅   |          |
| robots.txt honored by default           |    ✅    |   ✅   |    ✅    |
| Rate limiting + retry backoff           |    ✅    |   ✅   |    ✅    |
| CLI for operations (run/status/diff)    |    ✅    |   ✅   |          |
| MCP server (agents, n8n, scripts)       |    ✅    |        |          |
| Local-first output (no DB or cloud)     |    ✅    |   ✅   |    ✅    |

Scrapy wins on ecosystem depth (middlewares, pipelines, distributed crawling via Scrapyd). Crawl4AI wins on LLM-native extraction and speed. megamaid wins on "I want a working scraper for this one site by the end of the afternoon, and I want it to still work next month."

## Examples

See [`EXAMPLES.md`](EXAMPLES.md) for end-to-end walkthroughs: downloading product images (Lego at Walmart), archiving PDFs (FDA drug labels), following an RSS feed to a growing local archive, plus three MCP examples — agent sub-tool, n8n scheduled monitoring, and a plain Python cron script.

## Troubleshooting

> _"The radar's been jammed."_ _"Jammed? With what?"_ _"Raspberry jam, sir."_

Most scraper bugs are the equivalent of raspberry jam. See `references/troubleshooting.md` for selector drift, timeouts, blocks, and the stealth workaround for anti-bot CDNs.

## Installation — as a Claude Code skill

> _"One command. Even I can do it, and I'm half-dog."_
>
> — Barf

```bash
git clone git@github.com:whiffernet/megamaid.git ~/.claude/skills/megamaid
```

Verify it registered:

```bash
ls ~/.claude/skills/megamaid/SKILL.md
```

The skill is auto-discovered via `SKILL.md`. Ask Claude to "scrape a website" or "build a scraper for X" and it will invoke megamaid.

**Requires:** Python 3.11+ on the host. Everything else installs automatically when a project is scaffolded.

## Installation — as an MCP server (megamaid-mcp)

> _"They said it couldn't be done. I said I hadn't tried yet."_
>
> — Lone Starr, probably

The MCP server lets agents, n8n workflows, and scripts call megamaid without opening a Claude session. Full instructions: [`mcp/README.md`](mcp/README.md).

**Quick start:**

1. Add to your `docker-compose.yml` (replace `1000:1000` with your `id -u`:`id -g`):

```yaml
megamaid:
  image: ghcr.io/whiffernet/megamaid:latest
  container_name: megamaid-mcp
  user: "1000:1000"
  ports:
    - "127.0.0.1:8305:8000"
  volumes:
    - "${MEGAMAID_PROJECTS_DIR:-$HOME}:/projects:rw"
  environment:
    - MCP_BEARER_TOKEN=${MCP_BEARER_TOKEN}
    - MEGAMAID_PROJECTS_DIR_INTERNAL=/projects
  restart: unless-stopped
```

2. Register with Claude Code — add to `~/.claude/mcp.json`:

```json
{
  "megamaid": {
    "type": "http",
    "url": "http://localhost:8305",
    "headers": { "Authorization": "Bearer ${MCP_BEARER_TOKEN}" }
  }
}
```

3. Start it:

```bash
docker compose up -d megamaid
```

The four `megamaid_*` tools will appear in Claude's tool list on next launch.

## License

MIT

---

_May the Schwartz be with your selectors._
