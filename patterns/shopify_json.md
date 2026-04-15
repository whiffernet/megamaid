# Pattern: Shopify JSON API

> _"We found it, sir — the planet is running Shopify. All the hose work is already done."_

## When to use

The target is a Shopify store. Detection signals:

- `<meta name="shopify-checkout-api-token">` in page source
- `cdn.shopify.com` asset URLs
- `/collections/<handle>` and `/products/<handle>` paths
- **The smoking gun:** `https://<domain>/collections/<handle>/products.json` returns a JSON list (up to 250 items per request).

Try it first: `curl https://<domain>/collections/all/products.json?limit=250`.
If you get JSON, skip HTML scraping entirely.

## URL discovery

```python
PRODUCTS_API = "https://<domain>/collections/all/products.json?limit=250&page={page}"
```

Loop `page=1,2,3,...` until the `products` array comes back empty.

## Parsing

Each product has: `id`, `handle`, `title`, `vendor`, `product_type`,
`tags`, `body_html`, `variants[]`, `images[]`, `created_at`, `updated_at`.

`body_html` is HTML embedded in the JSON — parse it with BeautifulSoup.

```python
import json
from bs4 import BeautifulSoup
from playwright.async_api import Page
from megamaid.base import BaseScraper
from megamaid.models import ScrapedDoc, slug_from_url


class MyShopifyTarget(BaseScraper):
    target_name = "example_shop"
    base_url = "https://example-shop.myshopify.com"
    rate_limit_seconds = 1.5

    async def scrape(self, page: Page, max_items=None):
        docs = []
        for page_num in range(1, 100):
            url = f"{self.base_url}/collections/all/products.json?limit=250&page={page_num}"
            await self._navigate(page, url)
            body = await page.inner_text("body")
            products = json.loads(body).get("products", [])
            if not products:
                break
            for p in products:
                docs.append(self._parse(p))
                if max_items and len(docs) >= max_items:
                    return docs
        return docs

    def _parse(self, product: dict) -> ScrapedDoc:
        url = f"{self.base_url}/products/{product['handle']}"
        text = BeautifulSoup(product.get("body_html", ""), "html.parser").get_text("\n")
        return ScrapedDoc(
            id=slug_from_url(url),
            source_url=url,
            title=product["title"],
            content_md=text,
            metadata={
                "vendor": product.get("vendor"),
                "product_type": product.get("product_type"),
                "tags": product.get("tags", []),
                "created_at": product.get("created_at"),
                "updated_at": product.get("updated_at"),
                "variants": [
                    {"price": v.get("price"), "sku": v.get("sku")}
                    for v in product.get("variants", [])
                ],
            },
        )
```

## Gotchas

- `products.json` returns up to 250 per page; pass `?limit=250&page=N`.
- Not every product is a "real" product — stores often have gift cards,
  merch, pre-orders. Filter by `product_type` or `tags`.
- If you only want a specific collection, use
  `/collections/<handle>/products.json` instead of `/collections/all`.
- Some stores hide `products.json` behind a password gate. If you get a
  login page, this pattern doesn't apply.

## Public test target

There are many public Shopify demo stores; any Shopify store with an
open `products.json` endpoint will exercise this path. Confirm with
`curl` before coding.
