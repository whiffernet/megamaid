# megamaid — Examples

> _"Now I will show you what it does. Pay attention, because I'm only going to do this once... and then sell it to you."_
>
> — Yogurt, probably

Three examples — images, PDFs, and text — each showing what you say to Claude and what comes out the other side.

---

## Example 1: Product images

**What you say to Claude:**

> "Scrape all the product images from lululemon.com — I want the highest resolution of every photo."

**What megamaid does:**

Claude runs `megamaid recon https://www.lululemon.com`, sees PerimeterX anti-bot and no usable sitemap, then pivots to the `image_downloads` pattern — stealth pagination through 13 subcategory pages to enumerate every product URL, then extracts the image CDN URLs and downloads the largest available resolution per unique image (resolution-aware dedup skips smaller variants of photos it's already seen).

The project scaffolds in `~/megamaid-lululemon/`:

```
megamaid suck          # scrapes everything
megamaid suck --max 5  # dry-run: 5 products to verify selectors
```

**What you get:**

```
staging/lululemon/20260417T182244Z/
├── docs/           # 409 ScrapedDoc JSON files, one per product
│   ├── womens-define-jacket.json
│   └── ...
├── images/         # 4,431 images, content-hash filenames, auto-deduped
│   ├── a8f3c2d1.jpg   (2400×3000 px)
│   └── ...
└── manifest.json   # identity hashes, delta detection for re-runs
```

On a second run, only new products download — unchanged items are skipped by identity hash. Total: **617 MB**, **4,431 images**, **409 products** across 13 subcategories.

---

## Example 2: PDFs

**What you say to Claude:**

> "Archive all the FDA drug label PDFs for diabetes medications and pull the text out of them."

**What megamaid does:**

Claude runs `megamaid recon https://www.fda.gov`, detects the openFDA REST API at `/api/drug/label.json`, and chooses the `pdf_downloads` pattern — searches the openFDA API for diabetes-related labels, collects PDF download URLs from the results, then streams each PDF and extracts the text with pypdf.

No browser required. The entire run is httpx.

```
megamaid suck --max 5  # dry-run: 5 labels to verify extraction
megamaid suck          # full archive
```

**What you get:**

```
staging/fda_diabetes/20260417T194012Z/
├── raw/
│   ├── metformin-hydrochloride-label.pdf     (312 KB)
│   ├── insulin-glargine-label.pdf             (89 KB)
│   └── ...
├── docs/
│   ├── metformin-hydrochloride-label.json
│   │   {
│   │     "title": "Metformin Hydrochloride Tablets",
│   │     "content_md": "INDICATIONS AND USAGE\nMetformin hydrochloride
│   │                    tablets are indicated as an adjunct to diet and
│   │                    exercise to improve glycemic control...",
│   │     "metadata": {
│   │       "manufacturer": "Aurobindo Pharma",
│   │       "route": "ORAL",
│   │       "ndc": "65862-0189"
│   │     }
│   │   }
│   └── ...
└── manifest.json
```

Each doc has **20,000–48,000 characters** of extracted text ready for search, embedding, or analysis — no manual PDF wrangling.

---

## Example 3: Text / articles

**What you say to Claude:**

> "Follow the Hacker News feed and save every new story as markdown. I want it updated daily."

**What megamaid does:**

Claude runs `megamaid recon https://news.ycombinator.com`, spots the RSS alternate link in the `<head>`, and chooses the `rss_atom_feed` pattern — fetches `https://hnrss.org/newest`, parses the RSS 2.0 feed, and writes each story as a `ScrapedDoc` with the URL, title, summary, and publication timestamp. The manifest's identity-hash delta detection means only stories that weren't in the last run are written on subsequent runs.

```
megamaid suck --max 5  # dry-run: 5 stories

# Add to cron for daily updates:
# 0 8 * * * cd ~/megamaid-hn && source .venv/bin/activate && megamaid suck
```

**What you get:**

```
staging/hnrss/20260417T232911Z/
├── docs/
│   ├── blog-ezyang-com-2026-04-oss-code-review-in-the-era-of-llms.json
│   │   {
│   │     "title": "OSS code review, in the era of LLMs",
│   │     "source_url": "https://blog.ezyang.com/2026/04/oss-code-review-in-the-era-of-llms/",
│   │     "content_md": "There is a new phenomenon I have been noticing...",
│   │     "metadata": { "published": "Fri, 17 Apr 2026 23:20:58 +0000" }
│   │   }
│   ├── arstechnica-com-space-a-private-company-plans-to-bag-an-asteroid.json
│   └── ...
└── manifest.json
    {
      "total": 30,
      "new": 8,       ← only stories not seen in the previous run
      "unchanged": 22
    }
```

Run it daily and you get a growing local archive of everything that surfaces on HN — no API key, no database, no moving parts. Just files.

---

_May the Schwartz be with your selectors._
