# megamaid — Examples

> _"Now I will show you what it does. Pay attention, because I'm only going to do this once... and then sell it to you."_
>
> — Yogurt, probably

Three examples — images, PDFs, and text — each showing what you say to Claude and what comes out the other side.

---

## Example 1: Product images

**What you say to Claude:**

> "Scrape all the Lego product images from Walmart — I want the highest resolution of every photo."

**What megamaid does:**

Claude runs `megamaid recon https://www.walmart.com`, detects PerimeterX anti-bot and pulls `__NEXT_DATA__` SSR state from the page — Walmart bakes the full product payload into its server-rendered HTML, so no API reverse-engineering needed. megamaid uses stealth httpx headers (matching Walmart's expected `Sec-Fetch-*` profile + cookie warmup) to bypass PerimeterX, paginates through the Lego category, extracts the self-hosted image CDN URLs, and downloads the largest available resolution per unique image (resolution-aware dedup skips smaller variants of photos it's already seen).

The project scaffolds in `~/megamaid-walmart-lego/`:

```
megamaid suck          # scrapes everything
megamaid suck --max 5  # dry-run: 5 products to verify selectors
```

**What you get:**

```
staging/walmart_lego/20260417T182244Z/
├── docs/           # 847 ScrapedDoc JSON files, one per product
│   ├── lego-technic-42196-ferrari-499p-hybrid-hypercar.json
│   └── ...
├── images/         # 3,200+ images, content-hash filenames, auto-deduped
│   ├── a8f3c2d1.jpg   (2400×2400 px)
│   └── ...
└── manifest.json   # identity hashes, delta detection for re-runs
```

On a second run, only new or changed listings download — unchanged items are skipped by identity hash. Total: **~480 MB**, **3,200+ images**, **847 products** across sets, minifigures, and accessories.

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

## MCP Examples

> _"We're surrounded by assholes!"_ _"What? You went to MCP speed?!"_
>
> — Dark Helmet, discovering how much faster things get when you stop opening Claude Code every time

The MCP server (`megamaid-mcp`) exposes the same scraping power as a callable tool — no conversation required. These examples show what it looks like from a Claude agent, an automation workflow, and a direct API call.

### MCP Example 1: Agent sub-tool

An agent (Ernestine, ePortfolio, or any LangGraph node) calls `megamaid_run` mid-task to pull fresh product data without spinning up a separate Claude session.

**What the agent does:**

```python
# Inside a LangGraph tool node — no Claude Code session required
result = await mcp_client.call_tool(
    "megamaid_run",
    {
        "project": "megamaid-walmart-lego",
        "max_items": 20,
        "include_docs": True,
        "summary_only": True
    }
)

# result["new_docs"] contains the 20 most recently changed Lego listings
for doc in result["new_docs"]:
    print(doc["title"], doc["metadata"].get("price"))
# → LEGO Technic Ferrari 499P Hybrid Hypercar   $449.99
# → LEGO Icons Eiffel Tower                     $629.99
# → LEGO City Police Station                    $199.99
```

**What you get back:**

```json
{
  "run_id": "20260418T120000Z",
  "target": "walmart_lego",
  "stats": { "total": 847, "new": 3, "changed": 17, "unchanged": 827 },
  "new_docs": [
    {
      "title": "LEGO Technic Ferrari 499P Hybrid Hypercar",
      "source_url": "https://www.walmart.com/ip/...",
      "content_md": "Experience the thrill of Le Mans racing...",
      "metadata": { "price": "$449.99", "brand": "LEGO" }
    }
  ]
}
```

---

### MCP Example 2: Scheduled workflow (site monitoring)

A scheduled workflow calls `megamaid_recon` weekly against a list of competitor sites. When a site changes its anti-bot system or opens a new sitemap, a Slack alert fires — no Claude session, no human needed.

**Workflow:**

```
Schedule trigger (Monday 9am)
  ↓
HTTP Request node
  POST http://localhost:8305/mcp
  tool: megamaid_recon
  url:  https://www.competitor.com
  ↓
IF node: recommended_pattern changed since last week?
  ↓ yes
Slack node: "competitor.com switched from paginated_html → rest_json_api"
```

**What the recon tool returns:**

```json
{
  "recommended_pattern": { "pattern": "rest_json_api", "confidence": "high" },
  "anti_bot": { "primary_system": "cloudflare", "severity": "monitoring" },
  "recommended_rate_limit": 2.0,
  "warnings": []
}
```

Store last week's `recommended_pattern.pattern` in the workflow's static data. Compare on each run. If it changed, something significant happened on the target site worth investigating.

---

### MCP Example 3: Direct API call (no Claude required)

Any script, cron job, or service that can make HTTP requests can call the MCP server directly. Here's a Python script that runs a project and writes a daily digest to a file — no LLM, no conversation.

```python
import httpx, json

MCP_URL = "http://localhost:8305/mcp"
TOKEN   = "your-bearer-token"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "Authorization": f"Bearer {TOKEN}",
}

def mcp(session_id, tool, args):
    r = httpx.post(MCP_URL, headers={**HEADERS, "mcp-session-id": session_id},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": tool, "arguments": args}}, timeout=300)
    for line in r.text.splitlines():
        if line.startswith("data:"):
            d = json.loads(line[5:])
            return json.loads(d["result"]["content"][0]["text"])

# 1. Initialize session
r = httpx.post(MCP_URL, headers=HEADERS, json={"jsonrpc":"2.0","id":0,
    "method":"initialize","params":{"protocolVersion":"2024-11-05",
    "capabilities":{},"clientInfo":{"name":"cron","version":"1.0"}}})
sid = r.headers["mcp-session-id"]

# 2. Run the scraper
result = mcp(sid, "megamaid_run", {
    "project": "megamaid-hnrss",
    "include_docs": True,
    "summary_only": True
})

# 3. Write digest
with open("/tmp/hn-digest.md", "w") as f:
    f.write(f"# HN Digest — {result['run_id']}\n\n")
    for doc in result.get("new_docs", []):
        f.write(f"- [{doc['title']}]({doc['source_url']})\n")

print(f"Wrote {len(result.get('new_docs',[]))} new stories")
```

Run this from cron. No LLM. No conversation. Just files.

---

_May the Schwartz be with your selectors._
