"""Automated site recon for pattern recommendation.

Probes a target URL with 3-6 HTTP requests (no Playwright) and produces
a structured report with a recommended scraping pattern, anti-bot
assessment, and rate-limit suggestion.

Usage from the CLI::

    megamaid recon https://example.com
    megamaid recon --format json https://example.com
    megamaid recon --output report.json https://example.com

The scoring engine uses weighted signals from 54 tested sites (34 retail,
20 document-heavy) to rank the 10 megamaid pattern playbooks.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx

from .base import DEFAULT_USER_AGENT

logger = logging.getLogger(__name__)

# Sitemap XML namespace
_SM_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
_IMG_NS = {"image": "http://www.google.com/schemas/sitemap-image/1.1"}

# All 10 megamaid patterns
_PATTERNS = [
    "shopify_json",
    "sitemap_crawl",
    "paginated_html",
    "load_more_infinite",
    "pdf_downloads",
    "rest_json_api",
    "spa_hydration",
    "auth_wall",
    "image_downloads",
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """Result from a single recon probe.

    Attributes:
        name: Probe identifier (e.g. "robots_txt", "sitemap").
        status: One of "pass", "fail", "info", "warn", "skip".
        summary: One-line human-readable description.
        details: Probe-specific structured data.
        requests_made: Number of HTTP requests this probe consumed.
    """

    name: str
    status: str
    summary: str
    details: dict = field(default_factory=dict)
    requests_made: int = 0


@dataclass
class PatternScore:
    """Weighted score for a single scraping pattern.

    Attributes:
        pattern: Pattern name (e.g. "shopify_json").
        score: Numeric score 0-100.
        confidence: "high" (>=70), "medium" (40-69), "low" (<40).
        reasons: Human-readable explanations of contributing signals.
    """

    pattern: str
    score: float
    confidence: str
    reasons: list[str] = field(default_factory=list)


@dataclass
class ReconReport:
    """Complete recon output.

    Attributes:
        url: Original target URL.
        domain: Extracted domain name.
        timestamp: ISO 8601 timestamp of the recon run.
        probes: List of individual probe results.
        recommended_pattern: Top-scoring pattern.
        alternative_patterns: Other patterns scoring above 20.
        recommended_rate_limit: Suggested rate_limit_seconds.
        rate_limit_reason: Human explanation for the rate limit.
        total_requests: Total HTTP requests made during recon.
        warnings: Showstopper warnings (e.g. robots.txt blocks).
    """

    url: str
    domain: str
    timestamp: str
    probes: list[ProbeResult]
    recommended_pattern: PatternScore
    alternative_patterns: list[PatternScore]
    recommended_rate_limit: float
    rate_limit_reason: str
    total_requests: int
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


async def probe_robots(client: httpx.AsyncClient, base_url: str) -> ProbeResult:
    """Fetch and parse robots.txt for the target domain.

    Args:
        client: Shared httpx async client.
        base_url: Target base URL (scheme + host).

    Returns:
        ProbeResult with allowed/disallowed paths, Crawl-delay, and
        Sitemap directives in details.
    """
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    robots_url = f"{origin}/robots.txt"

    try:
        resp = await client.get(robots_url, timeout=10.0)
    except Exception as exc:
        return ProbeResult(
            name="robots_txt",
            status="skip",
            summary=f"Could not fetch robots.txt: {exc}",
            requests_made=1,
        )

    if resp.status_code >= 400:
        return ProbeResult(
            name="robots_txt",
            status="info",
            summary=f"No robots.txt (HTTP {resp.status_code})",
            requests_made=1,
        )

    # Check if we got HTML instead of text (login page masquerading)
    ct = resp.headers.get("content-type", "")
    if "text/html" in ct and "text/plain" not in ct:
        return ProbeResult(
            name="robots_txt",
            status="warn",
            summary="robots.txt returned HTML (possible login redirect)",
            details={"content_type": ct},
            requests_made=1,
        )

    raw = resp.text
    disallowed: list[str] = []
    sitemap_urls: list[str] = []
    crawl_delay: float | None = None

    for line in raw.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("disallow:"):
            path = line.split(":", 1)[1].strip()
            if path:
                disallowed.append(path)
        elif stripped.startswith("sitemap:"):
            url = line.split(":", 1)[1].strip()
            if url.startswith("//"):
                url = f"{parsed.scheme}:{url}"
            elif url.startswith("/"):
                url = origin + url
            if url.startswith("http"):
                sitemap_urls.append(url)
        elif stripped.startswith("crawl-delay:"):
            try:
                crawl_delay = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass

    summary_parts = []
    if disallowed:
        summary_parts.append(f"{len(disallowed)} disallowed paths")
    if crawl_delay:
        summary_parts.append(f"Crawl-delay: {crawl_delay}s")
    if sitemap_urls:
        summary_parts.append(f"{len(sitemap_urls)} sitemap(s) listed")
    summary = ", ".join(summary_parts) if summary_parts else "No restrictions"

    return ProbeResult(
        name="robots_txt",
        status="pass",
        summary=summary,
        details={
            "disallowed_paths": disallowed[:20],
            "sitemap_urls": sitemap_urls,
            "crawl_delay": crawl_delay,
        },
        requests_made=1,
    )


async def probe_sitemap(
    client: httpx.AsyncClient,
    base_url: str,
    known_sitemap_urls: list[str] | None = None,
) -> ProbeResult:
    """Check for sitemap and analyze its structure.

    Fetches the sitemap (from robots.txt Sitemap directives or standard
    paths) and samples URL patterns, image tags, and lastmod presence.
    Only fetches 1-2 sitemaps to stay within the request budget.

    Args:
        client: Shared httpx async client.
        base_url: Target base URL.
        known_sitemap_urls: Sitemap URLs found in robots.txt.

    Returns:
        ProbeResult with sitemap metadata in details.
    """
    candidates = list(known_sitemap_urls or [])
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        url = origin + path
        if url not in candidates:
            candidates.append(url)

    requests_made = 0
    for sitemap_url in candidates[:2]:
        try:
            resp = await client.get(sitemap_url, timeout=15.0)
            requests_made += 1
        except Exception:
            continue

        if resp.status_code >= 400:
            continue

        text = _sitemap_text(resp, sitemap_url)
        if not text:
            continue
        try:
            root = ET.fromstring(text.encode("utf-8", errors="replace"))
        except ET.ParseError:
            continue

        tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

        if tag == "sitemapindex":
            children = root.findall("sm:sitemap/sm:loc", _SM_NS)
            child_count = len(children)
            urls: list[str] = []
            child_text = ""
            # Fetch first child to sample URLs
            if children and children[0].text:
                try:
                    child_resp = await client.get(children[0].text, timeout=15.0)
                    requests_made += 1
                    child_text = _sitemap_text(child_resp, children[0].text)
                    child_root = ET.fromstring(
                        child_text.encode("utf-8", errors="replace")
                    )
                    urls = [
                        loc.text
                        for loc in child_root.findall("sm:url/sm:loc", _SM_NS)
                        if loc.text
                    ]
                except Exception:
                    pass

            has_image = "<image:image" in text or "<image:image" in child_text
            has_lastmod = "<lastmod>" in text or "<lastmod>" in child_text

            return ProbeResult(
                name="sitemap",
                status="pass",
                summary=f"Sitemap index ({child_count} children, ~{len(urls)} URLs sampled)",
                details={
                    "exists": True,
                    "is_index": True,
                    "child_sitemap_count": child_count,
                    "url_count": len(urls),
                    "sample_urls": urls[:10],
                    "url_patterns": _count_path_prefixes(urls),
                    "has_image_tags": has_image,
                    "has_lastmod": has_lastmod,
                },
                requests_made=requests_made,
            )

        elif tag == "urlset":
            urls = [
                loc.text for loc in root.findall("sm:url/sm:loc", _SM_NS) if loc.text
            ]
            has_image = "<image:image" in text
            has_lastmod = "<lastmod>" in text

            return ProbeResult(
                name="sitemap",
                status="pass",
                summary=f"Sitemap ({len(urls)} URLs)",
                details={
                    "exists": True,
                    "is_index": False,
                    "child_sitemap_count": 0,
                    "url_count": len(urls),
                    "sample_urls": urls[:10],
                    "url_patterns": _count_path_prefixes(urls),
                    "has_image_tags": has_image,
                    "has_lastmod": has_lastmod,
                },
                requests_made=requests_made,
            )

    return ProbeResult(
        name="sitemap",
        status="fail",
        summary="No sitemap found",
        details={"exists": False},
        requests_made=requests_made,
    )


def probe_anti_bot(resp: httpx.Response) -> ProbeResult:
    """Detect anti-bot systems from homepage response headers and body.

    Does NOT make additional requests — analyzes the response that was
    already fetched. Zero request cost.

    Args:
        resp: The homepage HTTP response.

    Returns:
        ProbeResult with detected system and severity in details.
    """
    headers = {k.lower(): v for k, v in resp.headers.items()}
    cookies = headers.get("set-cookie", "")
    body = resp.text[:50_000]  # only scan first 50KB

    detections: list[dict] = []

    # Cloudflare
    if headers.get("server", "").lower() == "cloudflare" or "cf-ray" in headers:
        severity = "monitoring"
        signals = []
        if "cf-ray" in headers:
            signals.append("cf-ray header")
        if "server: cloudflare" in str(headers):
            signals.append("server: cloudflare")
        if "challenges.cloudflare.com" in body or "cf-mitigated" in headers:
            severity = "blocking"
            signals.append("challenge page detected")
        detections.append(
            {"system": "cloudflare", "severity": severity, "signals": signals}
        )

    # Akamai
    if "x-akamai-transformed" in headers or "akamai-grn" in headers:
        signals = [k for k in ("x-akamai-transformed", "akamai-grn") if k in headers]
        if "_abck" in cookies:
            signals.append("_abck cookie")
        detections.append(
            {"system": "akamai", "severity": "monitoring", "signals": signals}
        )

    # PerimeterX
    px_headers = [k for k in headers if k.startswith("x-px")]
    if px_headers or "_pxhd" in cookies:
        signals = px_headers + (["_pxhd cookie"] if "_pxhd" in cookies else [])
        if "client.perimeterx.net" in body:
            signals.append("client.perimeterx.net in body")
        detections.append(
            {"system": "perimeterx", "severity": "blocking", "signals": signals}
        )

    # DataDome
    if "x-datadome" in headers or "datadome" in cookies:
        signals = []
        if "x-datadome" in headers:
            signals.append("x-datadome header")
        if "datadome" in cookies:
            signals.append("datadome cookie")
        detections.append(
            {"system": "datadome", "severity": "blocking", "signals": signals}
        )

    # Kasada
    kp_headers = [k for k in headers if k.startswith("x-kpsdk")]
    if kp_headers:
        detections.append(
            {"system": "kasada", "severity": "blocking", "signals": kp_headers}
        )

    # Imperva/Incapsula
    if "x-iinfo" in headers or "incap_ses_" in cookies:
        signals = []
        if "x-iinfo" in headers:
            signals.append("x-iinfo header")
        if "incap_ses_" in cookies:
            signals.append("incap_ses_ cookie")
        detections.append(
            {"system": "imperva", "severity": "monitoring", "signals": signals}
        )

    if not detections:
        return ProbeResult(
            name="anti_bot",
            status="pass",
            summary="No anti-bot detected",
            details={"detected": False},
        )

    top = detections[0]
    others = [d["system"] for d in detections[1:]]
    summary = f"{top['system'].title()} ({top['severity']})"
    if others:
        summary += f" + {', '.join(others)}"

    return ProbeResult(
        name="anti_bot",
        status="warn" if top["severity"] == "blocking" else "info",
        summary=summary,
        details={
            "detected": True,
            "primary_system": top["system"],
            "severity": top["severity"],
            "all_detections": detections,
        },
    )


def probe_structured_data(html: str) -> ProbeResult:
    """Check for JSON-LD and other structured data in the homepage HTML.

    Zero request cost — analyzes already-fetched HTML.

    Args:
        html: Raw HTML content of the homepage.

    Returns:
        ProbeResult with JSON-LD types and other structured data markers.
    """
    jsonld_types: list[str] = []
    for match in re.finditer(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.S | re.I,
    ):
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict):
                if "@graph" in data:
                    for item in data["@graph"]:
                        if isinstance(item, dict) and "@type" in item:
                            jsonld_types.append(str(item["@type"]))
                elif "@type" in data:
                    jsonld_types.append(str(data["@type"]))
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "@type" in item:
                        jsonld_types.append(str(item["@type"]))
        except (json.JSONDecodeError, TypeError):
            continue

    has_og = 'property="og:' in html or "property='og:" in html
    has_microdata = 'itemtype="http' in html

    if not jsonld_types and not has_og and not has_microdata:
        return ProbeResult(
            name="structured_data",
            status="info",
            summary="No structured data found",
            details={
                "has_jsonld": False,
                "has_opengraph": has_og,
                "has_microdata": has_microdata,
            },
        )

    parts = []
    if jsonld_types:
        parts.append(f"JSON-LD: {', '.join(set(jsonld_types))}")
    if has_og:
        parts.append("OpenGraph")
    if has_microdata:
        parts.append("Microdata")

    return ProbeResult(
        name="structured_data",
        status="pass",
        summary="; ".join(parts),
        details={
            "has_jsonld": bool(jsonld_types),
            "jsonld_types": list(set(jsonld_types)),
            "has_opengraph": has_og,
            "has_microdata": has_microdata,
        },
    )


async def probe_api_endpoints(
    client: httpx.AsyncClient,
    base_url: str,
    html: str,
) -> ProbeResult:
    """Detect common API patterns and framework data sources.

    Checks HTML for framework markers (Shopify, Next.js, Nuxt, WordPress,
    Squarespace), then makes up to 2 confirmation requests.

    Args:
        client: Shared httpx async client.
        base_url: Target base URL.
        html: Homepage HTML content.

    Returns:
        ProbeResult with detected framework and confirmed endpoints.
    """
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    requests_made = 0

    framework: str | None = None
    signals: list[str] = []
    endpoints: list[dict] = []
    has_next_data = False
    has_nuxt_data = False

    # Check HTML markers
    if "__NEXT_DATA__" in html:
        has_next_data = True
        signals.append("__NEXT_DATA__ script tag")
        if not framework:
            framework = "nextjs"

    if "__NUXT__" in html or "__nuxt" in html:
        has_nuxt_data = True
        signals.append("__NUXT__ data")
        if not framework:
            framework = "nuxt"

    if "cdn.shopify.com" in html or "shopify-checkout-api-token" in html:
        framework = "shopify"
        signals.append("Shopify CDN/meta tag")

    if "wp-content" in html or "wp-json" in html:
        if not framework:
            framework = "wordpress"
        signals.append("WordPress markers (wp-content/wp-json)")

    if "squarespace.com" in html or "sqs-" in html:
        if not framework:
            framework = "squarespace"
        signals.append("Squarespace markers")

    # Confirmation requests (up to 2)
    confirm_urls: list[str] = []
    if framework == "shopify":
        confirm_urls.append(f"{origin}/collections/all/products.json?limit=1")
    if framework == "wordpress" or "wp-json" in html:
        confirm_urls.append(f"{origin}/wp-json/wp/v2/posts?per_page=1")
    if framework == "squarespace":
        confirm_urls.append(f"{base_url.rstrip('/')}?format=json-pretty")

    for curl in confirm_urls[:2]:
        try:
            cresp = await client.get(curl, timeout=10.0)
            requests_made += 1
            ct = cresp.headers.get("content-type", "")
            endpoints.append(
                {
                    "url": curl,
                    "status": cresp.status_code,
                    "content_type": ct.split(";")[0].strip(),
                    "is_json": "json" in ct,
                }
            )
        except Exception:
            pass

    if not framework and not signals:
        return ProbeResult(
            name="api_endpoints",
            status="info",
            summary="No API endpoints or frameworks detected",
            details={"framework": None},
            requests_made=requests_made,
        )

    confirmed = [e for e in endpoints if e["is_json"] and e["status"] == 200]
    summary = f"{framework.title() if framework else 'Unknown'}"
    if confirmed:
        summary += f" (confirmed: {confirmed[0]['url'].split('?')[0]})"

    return ProbeResult(
        name="api_endpoints",
        status="pass",
        summary=summary,
        details={
            "framework": framework,
            "signals": signals,
            "endpoints": endpoints,
            "has_next_data": has_next_data,
            "has_nuxt_data": has_nuxt_data,
        },
        requests_made=requests_made,
    )


def probe_rendering(html: str) -> ProbeResult:
    """Assess whether the page requires JavaScript for content.

    Analyzes static HTML for content signals: text density, presence of
    SPA shell patterns, script count. Zero request cost.

    Args:
        html: Homepage HTML content.

    Returns:
        ProbeResult with text ratio and JS requirement assessment.
    """
    # Strip tags to get text
    text_only = re.sub(r"<[^>]+>", " ", html)
    text_only = re.sub(r"\s+", " ", text_only).strip()

    html_bytes = len(html.encode("utf-8", errors="replace"))
    text_bytes = len(text_only.encode("utf-8", errors="replace"))
    text_ratio = text_bytes / html_bytes if html_bytes > 0 else 0

    # SPA shell detection: <div id="root"></div> or <div id="app"></div>
    # with very little text content
    has_root_div = bool(
        re.search(r'<div\s+id="(root|app|__next)"[^>]*>\s*</div>', html, re.I)
    )
    script_count = html.lower().count("<script")
    has_noscript = "<noscript" in html.lower()
    needs_js = has_root_div and text_ratio < 0.05

    summary = "JS not required" if not needs_js else "Likely needs JavaScript"
    if needs_js:
        summary += f" (text ratio: {text_ratio:.2f}, root div detected)"

    return ProbeResult(
        name="rendering",
        status="info" if needs_js else "pass",
        summary=summary,
        details={
            "static_html_bytes": html_bytes,
            "text_content_bytes": text_bytes,
            "text_ratio": round(text_ratio, 3),
            "has_root_div_only": has_root_div,
            "needs_js": needs_js,
            "script_count": script_count,
            "noscript_present": has_noscript,
        },
    )


# ---------------------------------------------------------------------------
# Pattern scoring
# ---------------------------------------------------------------------------


def score_patterns(probes: list[ProbeResult]) -> list[PatternScore]:
    """Score all patterns against probe results using weighted signals.

    Args:
        probes: List of completed probe results.

    Returns:
        All patterns sorted by score descending, with confidence levels.
    """
    scores: dict[str, float] = {p: 0.0 for p in _PATTERNS}
    reasons: dict[str, list[str]] = {p: [] for p in _PATTERNS}

    probe_map = {p.name: p for p in probes}

    # --- API endpoints ---
    api = probe_map.get("api_endpoints")
    if api and api.details:
        fw = api.details.get("framework")
        confirmed = [
            e
            for e in api.details.get("endpoints", [])
            if e.get("is_json") and e.get("status") == 200
        ]

        if fw == "shopify":
            scores["shopify_json"] += 50
            reasons["shopify_json"].append("Shopify markers found (+50)")
            if confirmed:
                scores["shopify_json"] += 40
                reasons["shopify_json"].append("/products.json confirmed (+40)")
        if fw == "wordpress":
            scores["rest_json_api"] += 20
            reasons["rest_json_api"].append("WordPress markers (+20)")
            if confirmed:
                scores["rest_json_api"] += 40
                reasons["rest_json_api"].append("/wp-json/ confirmed (+40)")
        if fw == "squarespace":
            scores["rest_json_api"] += 30
            reasons["rest_json_api"].append("Squarespace detected (+30)")
        if api.details.get("has_next_data"):
            scores["spa_hydration"] += 30
            reasons["spa_hydration"].append("__NEXT_DATA__ found (+30)")
            scores["rest_json_api"] += 20
            reasons["rest_json_api"].append("__NEXT_DATA__ (extractable SSR data, +20)")
        if api.details.get("has_nuxt_data"):
            scores["spa_hydration"] += 30
            reasons["spa_hydration"].append("__NUXT__ found (+30)")
        if confirmed and fw not in ("shopify", "wordpress"):
            scores["rest_json_api"] += 30
            reasons["rest_json_api"].append("JSON API endpoint confirmed (+30)")

    # --- Sitemap ---
    sm = probe_map.get("sitemap")
    if sm and sm.details:
        if sm.details.get("exists"):
            url_count = sm.details.get("url_count", 0)
            scores["sitemap_crawl"] += 40
            reasons["sitemap_crawl"].append(
                f"Sitemap found with {url_count} URLs (+40)"
            )
            if url_count > 100:
                scores["sitemap_crawl"] += 20
                reasons["sitemap_crawl"].append(
                    f"Large sitemap ({url_count}+ URLs, +20)"
                )
            if sm.details.get("has_image_tags"):
                scores["image_downloads"] += 15
                reasons["image_downloads"].append("Sitemap has image:image tags (+15)")
            if sm.details.get("has_lastmod"):
                scores["sitemap_crawl"] += 10
                reasons["sitemap_crawl"].append("Sitemap has lastmod dates (+10)")
            # Check for PDF URL patterns
            patterns = sm.details.get("url_patterns", {})
            pdf_urls = sum(v for k, v in patterns.items() if ".pdf" in k.lower())
            if pdf_urls > url_count * 0.3 and url_count > 0:
                scores["pdf_downloads"] += 50
                reasons["pdf_downloads"].append(
                    f"PDF URLs dominate sitemap ({pdf_urls}/{url_count}, +50)"
                )
        else:
            scores["sitemap_crawl"] -= 30
            reasons["sitemap_crawl"].append("No sitemap found (-30)")

    # --- Rendering ---
    rend = probe_map.get("rendering")
    if rend and rend.details:
        if rend.details.get("needs_js"):
            scores["spa_hydration"] += 25
            reasons["spa_hydration"].append("Page needs JS rendering (+25)")
            scores["paginated_html"] -= 20
            reasons["paginated_html"].append(
                "Needs JS, static scraping unreliable (-20)"
            )
            scores["sitemap_crawl"] -= 10
            reasons["sitemap_crawl"].append("Needs JS for page content (-10)")
        elif rend.details.get("text_ratio", 0) > 0.1:
            scores["paginated_html"] += 15
            reasons["paginated_html"].append("Content-rich static HTML (+15)")
            scores["sitemap_crawl"] += 10
            reasons["sitemap_crawl"].append("Content accessible without JS (+10)")

    # --- Structured data ---
    sd = probe_map.get("structured_data")
    if sd and sd.details:
        types = sd.details.get("jsonld_types", [])
        if "Product" in types:
            scores["shopify_json"] += 10
            reasons["shopify_json"].append("JSON-LD Product type (+10)")
        if any(t in types for t in ("Article", "NewsArticle", "BlogPosting")):
            scores["paginated_html"] += 10
            reasons["paginated_html"].append("JSON-LD Article type (+10)")
        if any(t in types for t in ("Recipe",)):
            scores["paginated_html"] += 10
            reasons["paginated_html"].append("JSON-LD Recipe type (+10)")
        if types:
            scores["rest_json_api"] += 5
            reasons["rest_json_api"].append("Structured data present (+5)")

    # --- Anti-bot ---
    ab = probe_map.get("anti_bot")
    if ab and ab.details:
        if ab.details.get("severity") == "blocking":
            scores["spa_hydration"] += 10
            reasons["spa_hydration"].append(
                "Anti-bot blocking — browser may help (+10)"
            )
            scores["rest_json_api"] -= 15
            reasons["rest_json_api"].append(
                "Anti-bot blocking — API likely blocked too (-15)"
            )

    # --- Auth detection (from homepage response) ---
    # If robots probe found auth redirect signals, boost auth_wall
    robots = probe_map.get("robots_txt")
    if robots and robots.status == "warn" and "login" in robots.summary.lower():
        scores["auth_wall"] += 80
        reasons["auth_wall"].append("Auth redirect detected (+80)")

    # --- Baselines ---
    scores["paginated_html"] += 10
    reasons["paginated_html"].append("Common fallback pattern (+10)")

    # Build sorted results
    results = []
    for pattern in _PATTERNS:
        s = max(scores[pattern], 0)
        conf = "high" if s >= 70 else "medium" if s >= 40 else "low"
        r = [x for x in reasons[pattern] if x]
        results.append(
            PatternScore(pattern=pattern, score=s, confidence=conf, reasons=r)
        )

    results.sort(key=lambda x: x.score, reverse=True)
    return results


def recommend_rate_limit(probes: list[ProbeResult]) -> tuple[float, str]:
    """Recommend a rate_limit_seconds value based on probe results.

    Args:
        probes: List of completed probe results.

    Returns:
        Tuple of (rate_limit_seconds, human_reason).
    """
    probe_map = {p.name: p for p in probes}

    # Crawl-delay from robots.txt
    robots = probe_map.get("robots_txt")
    if robots and robots.details:
        cd = robots.details.get("crawl_delay")
        if cd and cd > 0:
            return (max(cd, 1.0), f"Crawl-delay directive: {cd}s")

    # Anti-bot severity
    ab = probe_map.get("anti_bot")
    if ab and ab.details:
        if ab.details.get("severity") == "blocking":
            return (
                3.0,
                f"Anti-bot ({ab.details.get('primary_system', 'unknown')}) is blocking",
            )
        if ab.details.get("severity") == "monitoring":
            return (
                2.0,
                f"Anti-bot ({ab.details.get('primary_system', 'unknown')}) is monitoring",
            )

    # Shopify / large commercial
    api = probe_map.get("api_endpoints")
    if api and api.details and api.details.get("framework") == "shopify":
        return (1.0, "Shopify store (commercial, bot-tolerant)")

    return (2.0, "Default (unfamiliar site)")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def run_recon(
    url: str,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 10.0,
    inter_probe_delay: float = 1.0,
) -> ReconReport:
    """Execute all recon probes against a URL and produce a report.

    Orchestrates probes sequentially with inter_probe_delay between
    each request. Total requests will not exceed 8. Uses httpx only.

    Args:
        url: Target URL to recon.
        user_agent: User-Agent header for all requests.
        timeout: Per-request timeout in seconds.
        inter_probe_delay: Minimum seconds between probe requests.

    Returns:
        A ReconReport with all probe results and recommendations.
    """
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    domain = parsed.netloc

    probes: list[ProbeResult] = []
    warnings: list[str] = []

    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent},
        follow_redirects=True,
        timeout=timeout,
    ) as client:
        # 1. robots.txt
        robots_result = await probe_robots(client, base_url)
        probes.append(robots_result)
        await asyncio.sleep(inter_probe_delay)

        # Check for disallow on the target path
        if robots_result.details:
            disallowed = robots_result.details.get("disallowed_paths", [])
            target_path = parsed.path or "/"
            for dp in disallowed:
                if dp == "/" or target_path.startswith(dp):
                    warnings.append(
                        f"robots.txt disallows {dp} — use --ignore-robots only with permission"
                    )
                    break

        # 2. Sitemap
        known_sitemaps = (
            robots_result.details.get("sitemap_urls", [])
            if robots_result.details
            else []
        )
        sitemap_result = await probe_sitemap(client, base_url, known_sitemaps)
        probes.append(sitemap_result)
        await asyncio.sleep(inter_probe_delay)

        # 3. Homepage fetch (shared by multiple probes)
        homepage_html = ""
        homepage_resp = None
        try:
            homepage_resp = await client.get(url, timeout=timeout)
            homepage_html = homepage_resp.text[:200_000]

            # Check for auth redirect
            if homepage_resp.status_code in (302, 303, 307) or (
                homepage_resp.history
                and any(
                    any(kw in str(r.url) for kw in ("login", "signin", "auth", "sso"))
                    for r in homepage_resp.history
                )
            ):
                warnings.append(
                    "Site redirects to login — may require auth_wall pattern"
                )
        except Exception as exc:
            warnings.append(f"Could not fetch homepage: {exc}")

        # 4-6. Zero-cost probes on homepage response
        if homepage_resp:
            probes.append(probe_anti_bot(homepage_resp))
        else:
            probes.append(
                ProbeResult(
                    name="anti_bot", status="skip", summary="No homepage response"
                )
            )

        probes.append(probe_structured_data(homepage_html))
        probes.append(probe_rendering(homepage_html))

        # 7. API endpoint probes (may make 0-2 requests)
        await asyncio.sleep(inter_probe_delay)
        api_result = await probe_api_endpoints(client, base_url, homepage_html)
        probes.append(api_result)

    # Score patterns
    scored = score_patterns(probes)
    recommended = scored[0]
    alternatives = [s for s in scored[1:] if s.score > 20]

    # Rate limit
    rate, rate_reason = recommend_rate_limit(probes)

    total_requests = sum(p.requests_made for p in probes) + 1  # +1 for homepage

    return ReconReport(
        url=url,
        domain=domain,
        timestamp=datetime.now(timezone.utc).isoformat(),
        probes=probes,
        recommended_pattern=recommended,
        alternative_patterns=alternatives,
        recommended_rate_limit=rate,
        rate_limit_reason=rate_reason,
        total_requests=total_requests,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------


_STATUS_ICONS = {
    "pass": "PASS",
    "fail": "FAIL",
    "info": "INFO",
    "warn": "WARN",
    "skip": "SKIP",
}


def format_text_report(report: ReconReport) -> str:
    """Format a ReconReport as human-readable text.

    Args:
        report: Completed recon report.

    Returns:
        Multi-line string suitable for terminal output.
    """
    lines: list[str] = []
    lines.append(f"\n  Recon: {report.domain}")
    lines.append("  " + "=" * (len(report.domain) + 8))
    lines.append("")

    for probe in report.probes:
        icon = _STATUS_ICONS.get(probe.status, "????")
        pad = "." * max(2, 24 - len(probe.name))
        lines.append(f"  {probe.name} {pad} {icon}")
        if probe.summary:
            lines.append(f"    {probe.summary}")
        lines.append("")

    lines.append("  ---")
    lines.append("")

    rec = report.recommended_pattern
    lines.append(f"  Recommended: {rec.pattern} (confidence: {rec.confidence})")
    for r in rec.reasons:
        lines.append(f"    {r}")
    lines.append("")

    if report.alternative_patterns:
        lines.append("  Alternatives:")
        for alt in report.alternative_patterns[:3]:
            lines.append(
                f"    {alt.pattern} ({alt.confidence}, score: {alt.score:.0f})"
            )
        lines.append("")

    lines.append(f"  Rate limit: {report.recommended_rate_limit}s")
    lines.append(f"    {report.rate_limit_reason}")
    lines.append("")
    lines.append(f"  Total requests: {report.total_requests}")

    if report.warnings:
        lines.append("")
        lines.append("  Warnings:")
        for w in report.warnings:
            lines.append(f"    ! {w}")

    lines.append("")
    return "\n".join(lines)


def format_json_report(report: ReconReport) -> str:
    """Format a ReconReport as JSON.

    Args:
        report: Completed recon report.

    Returns:
        Pretty-printed JSON string.
    """
    return json.dumps(asdict(report), indent=2, default=str)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sitemap_text(resp: httpx.Response, url: str) -> str:
    """Return decoded sitemap body, transparently decompressing .gz payloads.

    httpx auto-decompresses Content-Encoding: gzip, but sitemaps served as
    .gz files arrive as raw gzip bytes (Content-Type: application/x-gzip
    or application/octet-stream). Detect by URL suffix or magic bytes.

    Args:
        resp: httpx response from a sitemap fetch.
        url: The fetched URL (used to detect .gz suffix).

    Returns:
        UTF-8 decoded XML text capped at 500KB, or "" on failure.
    """
    raw = resp.content
    is_gz = url.lower().endswith(".gz") or raw[:2] == b"\x1f\x8b"
    if is_gz:
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError):
            return ""
    try:
        return raw[:500_000].decode("utf-8", errors="replace")
    except Exception:
        return ""


def _count_path_prefixes(urls: list[str], depth: int = 2) -> dict[str, int]:
    """Count URL path prefixes at a given depth.

    Args:
        urls: List of absolute URLs.
        depth: Number of path segments to use as the prefix.

    Returns:
        Dict mapping prefix → count, sorted by count descending.
    """
    counts: dict[str, int] = {}
    for url in urls:
        path = urlparse(url).path.strip("/")
        parts = path.split("/")
        prefix = (
            "/" + "/".join(parts[:depth]) + "/" if len(parts) >= depth else "/" + path
        )
        counts[prefix] = counts.get(prefix, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10])
