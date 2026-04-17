# Pattern: GraphQL API

> _"I don't know what I want — just give me exactly what I want, and nothing else."_
>
> — Dark Helmet, on GraphQL field selection

## When to use

The site has a GraphQL endpoint. GraphQL is the opposite philosophy
of REST: **one endpoint, one HTTP verb (POST), query strings describe
the shape of the response.** Modern frontends (especially e-commerce
and SaaS) have quietly replaced REST with GraphQL for everything.

Detection:

- DevTools → Network → XHR/Fetch, watch for `POST` to `/graphql`,
  `/api/graphql`, `/gql`, `/v1/graphql`, `api.<domain>/graphql`
- Request body is JSON with a `query` field (and often `variables`,
  `operationName`)
- Response is JSON `{ "data": { ... } }` shape
- Shopify Storefront API: `<domain>/api/<version>/graphql.json`
- Hasura / Apollo / Relay-based sites — all use GraphQL under the hood
- `<script>` tag exposing `window.__APOLLO_STATE__` — Apollo client cache

If you see a POST to `/graphql` with a JSON body containing `query`,
you're in.

## Key difference from `rest_json_api`

`rest_json_api` loops over URLs. GraphQL loops over **queries**. You
don't iterate page numbers — you iterate cursors inside the query
response. The HTTP method is always POST, the URL is always the same,
and the response shape mirrors your query.

## Discovering the schema

GraphQL APIs often support **introspection**, which lets you query
the schema itself:

```python
INTROSPECTION_QUERY = """
{
  __schema {
    queryType { name }
    types {
      name
      kind
      fields {
        name
        type { name kind ofType { name kind } }
      }
    }
  }
}
"""
```

POST that as `{"query": INTROSPECTION_QUERY}` to the endpoint. If
introspection is enabled you get the full schema back. If it's
disabled (common in production) you'll need to reverse-engineer
queries from DevTools — copy the exact query the frontend is
sending and replay it.

## Relay-style cursor pagination

By convention, list-returning GraphQL fields use the Relay
specification: `edges`, `pageInfo`, `endCursor`, `hasNextPage`.

```graphql
query Products($after: String, $first: Int!) {
  products(first: $first, after: $after) {
    edges {
      node {
        id
        title
        handle
      }
    }
    pageInfo {
      endCursor
      hasNextPage
    }
  }
}
```

Paginate by passing the previous response's `endCursor` as `after`
until `hasNextPage` becomes `false`.

## Example

```python
import asyncio

import httpx
from playwright.async_api import Browser

from megamaid.base import BaseScraper
from megamaid.models import ScrapedDoc, slug_from_url


PRODUCTS_QUERY = """
query Products($after: String, $first: Int!) {
  products(first: $first, after: $after) {
    edges {
      cursor
      node {
        id
        title
        handle
        description
        tags
      }
    }
    pageInfo {
      endCursor
      hasNextPage
    }
  }
}
"""


class MyGraphQLTarget(BaseScraper):
    target_name = "example_graphql"
    base_url = "https://example.com"
    graphql_url = "https://example.com/api/graphql"
    rate_limit_seconds = 1.0

    async def scrape(self, page, max_items=None):
        raise NotImplementedError("Use run() directly for GraphQL targets")

    async def run(self, browser: Browser, max_items=None):
        async with httpx.AsyncClient(
            headers={
                "User-Agent": self.user_agent,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        ) as client:
            return await self._scrape_graphql(client, max_items)

    async def _scrape_graphql(self, client, max_items):
        docs = []
        cursor = None
        while True:
            data = await self._post_graphql(
                client,
                PRODUCTS_QUERY,
                variables={"after": cursor, "first": 50},
            )
            if not data:
                break
            products = data["data"]["products"]
            for edge in products["edges"]:
                docs.append(self._parse(edge["node"]))
                if max_items and len(docs) >= max_items:
                    return docs
            page_info = products["pageInfo"]
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info["endCursor"]
        return docs

    async def _post_graphql(self, client, query: str, variables: dict | None = None):
        """POST a GraphQL query with rate limiting and retry on 429."""
        await self._rate_limit()
        body = {"query": query}
        if variables is not None:
            body["variables"] = variables
        resp = await client.post(self.graphql_url, json=body, timeout=30.0)
        if resp.status_code == 429:
            retry_after = min(int(resp.headers.get("Retry-After", 5)), 30)
            await asyncio.sleep(retry_after)
            return await self._post_graphql(client, query, variables)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            # GraphQL returns 200 OK with errors in the body
            raise RuntimeError(f"GraphQL errors: {payload['errors']}")
        return payload

    def _parse(self, node: dict) -> ScrapedDoc:
        url = f"{self.base_url}/products/{node['handle']}"
        return ScrapedDoc(
            id=slug_from_url(url),
            source_url=url,
            title=node["title"],
            content_md=node.get("description", ""),
            metadata={
                "graphql_id": node["id"],
                "tags": node.get("tags", []),
            },
        )
```

## Authentication

Most public GraphQL APIs are either keyless (with strict rate limits)
or require a bearer token:

- **Bearer token**: `Authorization: Bearer <token>`
- **Shopify Storefront API**: `X-Shopify-Storefront-Access-Token: <token>`
- **Apollo / Hasura**: custom headers per deployment — check DevTools

Never hardcode tokens. Use `os.environ["API_TOKEN"]`.

## Gotchas

- **GraphQL always returns HTTP 200 on errors.** Failed queries land in
  `payload["errors"]`, not `resp.raise_for_status()`. Always check for
  `errors` in the response body.
- **Introspection is often disabled.** Don't assume you can query the
  schema. Copy the exact query from DevTools if you need a starting
  point.
- **Query complexity limits.** Some APIs (GitHub) score queries by
  depth + breadth and reject "too complex" requests. Reduce the fields
  you ask for or paginate more aggressively.
- **Cost-based rate limits.** GitHub's GraphQL API has a "cost" budget
  that's independent of request count. A single 100-item page may cost
  more than ten 10-item pages. Check response `extensions.cost` if
  present.
- **Same query, different shape.** Two nearly-identical queries can
  return slightly different field structures based on fragments,
  aliases, or directives (`@include`, `@skip`). When in doubt, replay
  the exact query the frontend sends.
- **Persisted queries.** Modern Apollo/Relay apps sometimes send only a
  hash (`extensions.persistedQuery.sha256Hash`) instead of the full
  query. The server then looks up the query by hash. If you hit a
  `PersistedQueryNotFound` error, send the full `query` field the
  first time to teach the server.
- **Batching.** Some clients batch multiple queries in one POST
  (`[{query: "..."}, {query: "..."}]`). You don't need to do this —
  separate calls work fine.
- **CORS doesn't matter.** GraphQL servers (like REST) enforce CORS
  only against browsers. httpx ignores it entirely.
- **`variables` must be JSON-valid.** Don't embed user data with naive
  string formatting — pass it through `variables` to avoid injection
  and quoting hell.

## Public test targets

- `https://countries.trevorblades.com/` — public countries/currencies
  GraphQL API, no auth, supports introspection
- `https://spacex-production.up.railway.app/` — SpaceX launches
  (unofficial community mirror), no auth
- `https://api.github.com/graphql` — GitHub GraphQL API v4, requires
  a personal access token, cost-based rate limits
- `https://graphqlzero.almansi.me/api` — JSONPlaceholder-style fake
  data over GraphQL, no auth, ideal for pagination testing
