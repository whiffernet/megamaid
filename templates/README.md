# megamaid-scraper

> _"Suck... suck... suck... suck... ah, there it is."_
>
> — President Skroob, probably

Scaffolded by the [`megamaid`](https://github.com/whiffernet/megamaid) Claude Code skill. This is a standalone Python project — it does not need the skill to run, only to regenerate targets.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

## Layout

```
./
├── pyproject.toml
├── README.md
├── megamaid/          # reusable runtime (don't edit unless you know why)
│   ├── base.py        # BaseScraper: rate limit, retry, screenshot-on-error
│   ├── cli.py         # suck / status / diff / init
│   ├── manifest.py    # run tracking, delta detection
│   └── models.py      # ScrapedDoc
├── targets/           # one file per target — edit these
│   └── <target>.py
└── staging/           # scrape output lives here (gitignore this)
    └── <target>/<run_id>/
        ├── raw/           # original HTML/JSON per item
        ├── docs/          # normalized ScrapedDoc JSON per item
        ├── debug/         # error screenshots
        └── manifest.json  # run state + identity hashes
```

## Usage

```bash
megamaid suck --max 5          # dry-run: 5 items
megamaid suck                  # full run, resumable on crash
megamaid status                # show latest run summary
megamaid diff                  # new / changed / unchanged vs. last run
megamaid export --format csv   # export as CSV, JSONL, or JSON
megamaid map https://example.com  # discover all URLs on a domain
megamaid suck --ignore-robots  # only if you have written permission
```

## Writing a target

1. Copy `targets/example_target.py` to `targets/<your_target>.py`.
2. Set `target_name`, `base_url`, and (optionally) `rate_limit_seconds`.
3. Implement `scrape(page, max_items)`:
   - Discover URLs (sitemap, pagination, JSON API, etc.)
   - For each URL, `await self._navigate(page, url)`
   - Parse the page into one or more `ScrapedDoc`
   - Return the list

That's it. The base class handles rate limiting, retries, and screenshot-on-error.

## Etiquette

- Respect `robots.txt` — the CLI checks it by default. `--ignore-robots` is opt-in and requires you to have permission from the site owner.
- Keep the rate limit at 1s or higher. Smaller/independent sites deserve 2s+.
- Don't run multiple scrapers at once against the same site.
- If the site offers an API, data dump, or bulk download: use that instead.

## License

Yours — this is your project now. `megamaid/` is MIT-licensed if you want to share it.
