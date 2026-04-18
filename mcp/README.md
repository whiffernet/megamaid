# megamaid-mcp

MCP server for megamaid — exposes web scraping as callable tools for agents,
n8n workflows, and scripts. No Claude Code session required.

## Tools

| Tool                               | What it does                                                           |
| ---------------------------------- | ---------------------------------------------------------------------- |
| `megamaid_recon(url)`              | Probe a URL and recommend a scraping pattern (3–6 HTTP requests, ~15s) |
| `megamaid_run(project, ...)`       | Run a scaffolded megamaid project, return stats + optional docs        |
| `megamaid_status(project)`         | Latest run stats from disk — fast, no network                          |
| `megamaid_list_docs(project, ...)` | List scraped docs from a run                                           |

See [`../EXAMPLES.md`](../EXAMPLES.md) for usage examples.

---

## Installation

### Prerequisites

- Docker + Docker Compose
- A megamaid project already scaffolded (e.g. `~/megamaid-walmart`)

### Step 0 — Add two values to your `.env`

```bash
# A random secret — anyone calling the server must present this token
MCP_BEARER_TOKEN=$(openssl rand -hex 32)

# Your Linux user/group ID so the container can read your project files
# Run `id` to find yours: uid=1001(alice) gid=1001(alice) → use 1001:1001
MEGAMAID_UID_GID=1001:1001
```

### Step 1 — Add the service to your docker-compose.yml

```yaml
megamaid:
  image: ghcr.io/whiffernet/megamaid:latest # or build locally (see below)
  container_name: megamaid-mcp
  user: "${MEGAMAID_UID_GID}"
  ports:
    - "127.0.0.1:8305:8000"
  cap_drop:
    - ALL
  security_opt:
    - no-new-privileges:true
  tmpfs:
    - /tmp:noexec,nosuid,size=256m
  volumes:
    - "${MEGAMAID_PROJECTS_DIR:-$HOME}:/projects:rw"
  environment:
    - MCP_BEARER_TOKEN=${MCP_BEARER_TOKEN}
    - MEGAMAID_PROJECTS_DIR_INTERNAL=/projects
    - MEGAMAID_TIMEOUT=300
  mem_limit: 512m
  pids_limit: 100
  restart: unless-stopped
  healthcheck:
    test:
      [
        "CMD",
        "python",
        "-c",
        "import socket; s = socket.create_connection(('localhost', 8000), timeout=5); s.close()",
      ]
    interval: 30s
    timeout: 10s
    retries: 3
    start_period: 15s
```

### Step 2 — Set your projects directory

Add to your `.env` file (or export in your shell):

```bash
# Directory that contains your megamaid projects
# e.g. if your projects are at ~/megamaid-walmart, ~/megamaid-hnrss, etc.
MEGAMAID_PROJECTS_DIR=/home/youruser
```

If omitted, defaults to `$HOME`.

### Step 3 — Start the server

```bash
docker compose up -d megamaid
docker ps --filter name=megamaid-mcp   # should show "(healthy)"
```

### Step 4 — Register with Claude Code

Create or edit `~/.claude/mcp.json`:

```json
{
  "megamaid": {
    "type": "http",
    "url": "http://localhost:8305",
    "headers": {
      "Authorization": "Bearer ${MCP_BEARER_TOKEN}"
    }
  }
}
```

Restart Claude Code. The four `megamaid_*` tools will appear in your tool list.

### Step 5 — Verify

In Claude Code (or any MCP client), call the recon tool:

```
megamaid_recon("https://books.toscrape.com")
```

Expected: `recommended_pattern: paginated_html` with `confidence: low`.

---

## Building locally instead of pulling from GHCR

The Dockerfile is at `mcp/Dockerfile` in the megamaid repo root. Build context
must be the repo root (so the Dockerfile can access `templates/`):

```bash
git clone git@github.com:whiffernet/megamaid.git
cd megamaid
docker build -t megamaid-mcp -f mcp/Dockerfile .
```

Then use `image: megamaid-mcp` instead of `ghcr.io/whiffernet/megamaid:latest`
in your docker-compose.

---

## Calling tools from code (no Claude)

The server uses the MCP streamable-HTTP protocol. Every session needs an
initialization handshake first:

```python
import httpx, json

MCP_URL = "http://localhost:8305/mcp"
TOKEN   = "your-bearer-token"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "Authorization": f"Bearer {TOKEN}",
}

def parse_sse(text):
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:])
    return {}

def tool_call(session_id, name, args):
    r = httpx.post(MCP_URL, headers={**HEADERS, "mcp-session-id": session_id},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": name, "arguments": args}}, timeout=300)
    d = parse_sse(r.text)
    return json.loads(d["result"]["content"][0]["text"])

# Initialize session
r = httpx.post(MCP_URL, headers=HEADERS, json={
    "jsonrpc": "2.0", "id": 0, "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
               "clientInfo": {"name": "my-script", "version": "1.0"}}
})
session_id = r.headers["mcp-session-id"]

# Call a tool
result = tool_call(session_id, "megamaid_recon", {"url": "https://example.com"})
print(result["recommended_pattern"]["pattern"])
```

---

## Tool reference

### megamaid_recon

```
megamaid_recon(url: str) -> dict
```

Probes a URL and returns a full `ReconReport`:

```json
{
  "recommended_pattern": { "pattern": "rss_atom_feed", "confidence": "medium", "score": 60 },
  "alternative_patterns": [...],
  "anti_bot": { "detected": false },
  "recommended_rate_limit": 30.0,
  "rate_limit_reason": "Crawl-delay directive: 30.0s",
  "warnings": [],
  "total_requests": 4
}
```

### megamaid_run

```
megamaid_run(
  project: str,            # "megamaid-walmart" or "/projects/megamaid-walmart"
  max_items: int = None,   # cap on items scraped
  include_docs: bool = False,  # include new+changed docs inline
  summary_only: bool = True,   # truncate content_md to 500 chars
) -> dict
```

Always returns `{ run_id, target, staging_dir, stats }`.
With `include_docs=True`: also returns `new_docs[]` and `changed_docs[]`.

### megamaid_status

```
megamaid_status(project: str) -> dict
```

Returns `{ run_id, target, status, started_at, completed_at, staging_dir, stats }` for the most recent run.

### megamaid_list_docs

```
megamaid_list_docs(
  project: str,
  run_id: str = None,    # defaults to latest
  limit: int = 50,       # max 500
) -> dict
```

Returns `{ run_id, target, docs[], total, returned }`.
Each doc: `{ id, title, source_url, content_md (500 chars), metadata }`.

---

## Configuration

| Environment variable             | Default     | Description                                     |
| -------------------------------- | ----------- | ----------------------------------------------- |
| `MCP_BEARER_TOKEN`               | required    | Bearer token for authentication                 |
| `MEGAMAID_PROJECTS_DIR_INTERNAL` | `/projects` | Mount point inside the container                |
| `MEGAMAID_TIMEOUT`               | `300`       | Subprocess timeout for `megamaid_run` (seconds) |

---

## Port map

Follows the `mcp-*` naming convention in this environment:

| Service            | Port |
| ------------------ | ---- |
| mcp-ynab           | 8301 |
| mcp-apple-calendar | 8302 |
| megamaid-mcp       | 8305 |
