#!/usr/bin/env python3
"""
Web Research Module -- search and fetch for MikeyV agents.

Ported from OpenClaw's web search architecture. Three search backends
with automatic fallback, plus URL content extraction.

Search backends (in priority order):
  1. SearXNG  -- self-hosted, JSON API, no CAPTCHA, no rate limits
  2. DDG HTML -- keyless, HTML parsing, CAPTCHA risk
  3. Brave    -- API key required, most reliable

Usage as library:
    from web_research import search, fetch_url
    results = search("xAI post-training team")
    content = fetch_url("https://example.com/page")

Usage as CLI:
    python3 web_research.py search "query here"
    python3 web_research.py fetch "https://example.com"

Author: MikeyV-Cinco
Based on: OpenClaw extensions/duckduckgo, extensions/searxng, extensions/web-readability
"""

import json
import os
import re
import sys
import time
import subprocess
from html import unescape
from urllib.parse import urlparse, unquote, urlencode, quote_plus

# Optional imports -- degrade gracefully
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SEARXNG_BASE_URL = os.environ.get("SEARXNG_BASE_URL", "")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
DEFAULT_TIMEOUT = 20
DEFAULT_COUNT = 10
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# SearXNG backend
# ---------------------------------------------------------------------------

def search_searxng(query, count=DEFAULT_COUNT, categories=None, language=None,
                   base_url=None, timeout=DEFAULT_TIMEOUT):
    """Search via self-hosted SearXNG instance. Returns JSON API results."""
    base = base_url or SEARXNG_BASE_URL
    if not base:
        raise RuntimeError("SearXNG not configured (set SEARXNG_BASE_URL)")

    url = base.rstrip("/") + "/search"
    params = {"q": query, "format": "json"}
    if categories:
        params["categories"] = categories
    if language:
        params["language"] = language

    if HAS_REQUESTS:
        r = requests.get(url, params=params, timeout=timeout,
                         headers={"Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
    else:
        full_url = url + "?" + urlencode(params)
        result = subprocess.run(
            ["curl", "-sL", "-H", "Accept: application/json",
             "--max-time", str(timeout), full_url],
            capture_output=True, text=True, timeout=timeout + 5)
        data = json.loads(result.stdout)

    raw_results = data.get("results", [])
    results = []
    for item in raw_results[:count]:
        if isinstance(item, dict) and item.get("url") and item.get("title"):
            results.append({
                "title": item["title"],
                "url": item["url"],
                "snippet": item.get("content", ""),
            })
    return {"provider": "searxng", "query": query, "count": len(results),
            "results": results}


# ---------------------------------------------------------------------------
# DuckDuckGo HTML backend (ported from OpenClaw ddg-client.ts)
# ---------------------------------------------------------------------------

DDG_HTML_ENDPOINT = "https://html.duckduckgo.com/html"


def _decode_ddg_url(raw_url):
    """Decode DDG's redirect wrapper URLs."""
    try:
        if raw_url.startswith("//"):
            raw_url = "https:" + raw_url
        parsed = urlparse(raw_url)
        from urllib.parse import parse_qs
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return qs["uddg"][0]
    except Exception:
        pass
    return raw_url


def _strip_html(html_text):
    """Remove HTML tags and normalize whitespace."""
    text = re.sub(r'<[^>]+>', ' ', html_text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _is_bot_challenge(html):
    """Detect DDG bot-detection pages."""
    if re.search(r'class="[^"]*\bresult__a\b[^"]*"', html, re.I):
        return False
    return bool(re.search(
        r'g-recaptcha|are you a human|id="challenge-form"|name="challenge"',
        html, re.I))


def _parse_ddg_html(html, count):
    """Parse DuckDuckGo HTML search results page."""
    results = []

    # Find all result links with class result__a
    result_pattern = re.compile(
        r'<a\b(?=[^>]*\bclass="[^"]*\bresult__a\b[^"]*")([^>]*)>([\s\S]*?)</a>',
        re.I)
    snippet_pattern = re.compile(
        r'<a\b(?=[^>]*\bclass="[^"]*\bresult__snippet\b[^"]*")[^>]*>([\s\S]*?)</a>',
        re.I)
    href_pattern = re.compile(r'\bhref="([^"]*)"', re.I)

    matches = list(result_pattern.finditer(html))
    for i, match in enumerate(matches):
        attrs = match.group(1)
        raw_title = match.group(2)
        href_m = href_pattern.search(attrs)
        raw_url = href_m.group(1) if href_m else ""

        # Scope snippet search between this result and the next
        match_end = match.end()
        next_start = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        trailing = html[match_end:next_start]
        snippet_m = snippet_pattern.search(trailing)
        raw_snippet = snippet_m.group(1) if snippet_m else ""

        title = unescape(_strip_html(raw_title))
        url = _decode_ddg_url(unescape(raw_url))
        snippet = unescape(_strip_html(raw_snippet))

        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= count:
            break

    return results


def search_ddg(query, count=DEFAULT_COUNT, region=None, timeout=DEFAULT_TIMEOUT):
    """Search via DuckDuckGo HTML endpoint. No API key required."""
    params = {"q": query}
    if region:
        params["kl"] = region
    params["kp"] = "-2"  # safe search off

    url = DDG_HTML_ENDPOINT + "?" + urlencode(params)
    headers = {"User-Agent": USER_AGENT}

    if HAS_REQUESTS:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        html = r.text
    else:
        result = subprocess.run(
            ["curl", "-sL", "-H", f"User-Agent: {USER_AGENT}",
             "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 5)
        html = result.stdout

    if _is_bot_challenge(html):
        raise RuntimeError("DuckDuckGo returned a bot-detection challenge (CAPTCHA)")

    results = _parse_ddg_html(html, count)
    return {"provider": "duckduckgo", "query": query, "count": len(results),
            "results": results}


# ---------------------------------------------------------------------------
# Brave Search backend
# ---------------------------------------------------------------------------

BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
BRAVE_LLM_CONTEXT_ENDPOINT = "https://api.search.brave.com/res/v1/llm/context"


def _brave_request(url, api_key, timeout):
    """Make authenticated Brave API request."""
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
    }
    if HAS_REQUESTS:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    else:
        header_args = []
        for k, v in headers.items():
            header_args.extend(["-H", f"{k}: {v}"])
        result = subprocess.run(
            ["curl", "-sL", "--compressed", "--max-time", str(timeout),
             url] + header_args,
            capture_output=True, text=True, timeout=timeout + 5)
        return json.loads(result.stdout)


def search_brave(query, count=DEFAULT_COUNT, api_key=None, timeout=DEFAULT_TIMEOUT,
                 country=None, freshness=None, mode="web"):
    """Search via Brave Search API. Supports web and llm-context modes.

    mode="web": standard search results (title, url, snippet)
    mode="llm-context": pre-extracted page content optimized for LLM grounding
    """
    key = api_key or BRAVE_API_KEY
    if not key:
        raise RuntimeError("Brave API key not configured (set BRAVE_API_KEY)")

    if mode == "llm-context":
        endpoint = BRAVE_LLM_CONTEXT_ENDPOINT
        params = {"q": query}
    else:
        endpoint = BRAVE_SEARCH_ENDPOINT
        params = {"q": query, "count": str(min(count, 10))}

    if country:
        params["country"] = country
    if freshness and mode == "web":
        params["freshness"] = freshness

    url = endpoint + "?" + urlencode(params)
    data = _brave_request(url, key, timeout)

    if mode == "llm-context":
        # LLM context returns grounding chunks with full text snippets
        raw = data.get("grounding", {}).get("generic", [])
        results = []
        for item in raw:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippets": [s for s in item.get("snippets", []) if s],
            })
        return {"provider": "brave-llm", "query": query, "count": len(results),
                "results": results}
    else:
        results = []
        for item in data.get("web", {}).get("results", [])[:count]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            })
        return {"provider": "brave", "query": query, "count": len(results),
                "results": results}


# ---------------------------------------------------------------------------
# Gemini with Google Search grounding
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODEL = "gemini-2.5-flash"


def search_gemini(query, api_key=None, model=None, timeout=DEFAULT_TIMEOUT):
    """Search via Gemini with Google Search grounding.

    Returns AI-synthesized answer with citations from Google Search.
    """
    key = api_key or GEMINI_API_KEY
    if not key:
        raise RuntimeError("Gemini API key not configured (set GEMINI_API_KEY)")

    mdl = model or GEMINI_MODEL
    url = f"{GEMINI_API_BASE}/models/{mdl}:generateContent"
    body = json.dumps({
        "contents": [{"parts": [{"text": query}]}],
        "tools": [{"google_search": {}}],
    })

    if HAS_REQUESTS:
        r = requests.post(url, headers={
            "Content-Type": "application/json",
            "x-goog-api-key": key,
        }, data=body, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    else:
        result = subprocess.run(
            ["curl", "-sL", "--max-time", str(timeout),
             "-H", "Content-Type: application/json",
             "-H", f"x-goog-api-key: {key}",
             "-d", body, url],
            capture_output=True, text=True, timeout=timeout + 5)
        data = json.loads(result.stdout)

    if data.get("error"):
        err = data["error"]
        raise RuntimeError(f"Gemini error ({err.get('code')}): {err.get('message')}")

    candidate = (data.get("candidates") or [{}])[0]
    parts = candidate.get("content", {}).get("parts", [])
    content = "\n".join(p.get("text", "") for p in parts if p.get("text"))

    chunks = candidate.get("groundingMetadata", {}).get("groundingChunks", [])
    citations = []
    for chunk in chunks:
        web = chunk.get("web", {})
        if web.get("uri"):
            citations.append({
                "url": web["uri"],
                "title": web.get("title", ""),
            })

    return {"provider": "gemini", "query": query, "content": content,
            "citations": citations}


# ---------------------------------------------------------------------------
# Unified search with fallback
# ---------------------------------------------------------------------------

def search(query, count=DEFAULT_COUNT, providers=None, timeout=DEFAULT_TIMEOUT):
    """
    Search with automatic fallback across providers.

    providers: list of provider names to try, in order.
               Default: ["searxng", "ddg", "brave"] (skips unavailable ones)
    """
    if providers is None:
        providers = []
        if SEARXNG_BASE_URL:
            providers.append("searxng")
        providers.append("ddg")
        if BRAVE_API_KEY:
            providers.append("brave")
        if GEMINI_API_KEY:
            providers.append("gemini")

    errors = []
    for provider in providers:
        try:
            if provider == "searxng":
                return search_searxng(query, count=count, timeout=timeout)
            elif provider == "ddg":
                return search_ddg(query, count=count, timeout=timeout)
            elif provider == "brave":
                return search_brave(query, count=count, timeout=timeout)
            elif provider == "brave-llm":
                return search_brave(query, mode="llm-context", timeout=timeout)
            elif provider == "gemini":
                return search_gemini(query, timeout=timeout)
            else:
                errors.append(f"Unknown provider: {provider}")
        except Exception as e:
            errors.append(f"{provider}: {e}")

    return {"provider": "none", "query": query, "count": 0, "results": [],
            "errors": errors}


# ---------------------------------------------------------------------------
# URL content extraction
# ---------------------------------------------------------------------------

def fetch_url(url, timeout=DEFAULT_TIMEOUT, max_chars=100000):
    """
    Fetch a URL and extract readable text content.

    Returns dict with: url, title, text, content_length, method
    """
    headers = {"User-Agent": USER_AGENT}

    if HAS_REQUESTS:
        r = requests.get(url, headers=headers, timeout=timeout,
                         allow_redirects=True)
        r.raise_for_status()
        html = r.text[:max_chars * 10]  # cap raw HTML
        content_type = r.headers.get("Content-Type", "")
    else:
        result = subprocess.run(
            ["curl", "-sL", "-H", f"User-Agent: {USER_AGENT}",
             "--max-time", str(timeout), "-D", "-", url],
            capture_output=True, text=True, timeout=timeout + 5)
        html = result.stdout
        content_type = ""

    # If not HTML, return raw text
    if content_type and "text/html" not in content_type.lower():
        text = html[:max_chars]
        return {"url": url, "title": "", "text": text,
                "content_length": len(text), "method": "raw"}

    # Try BeautifulSoup extraction
    if HAS_BS4:
        return _extract_with_bs4(url, html, max_chars)

    # Fallback: regex strip tags
    return _extract_with_regex(url, html, max_chars)


def _extract_with_bs4(url, html, max_chars):
    """Extract readable content using BeautifulSoup."""
    soup = BeautifulSoup(html, "lxml")

    # Get title
    title = ""
    title_tag = soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)

    # Remove script, style, nav, header, footer, aside
    for tag in soup.find_all(["script", "style", "nav", "header", "footer",
                               "aside", "noscript", "iframe"]):
        tag.decompose()

    # Try article or main content first
    main = soup.find("article") or soup.find("main") or soup.find("body")
    if main:
        text = main.get_text(separator="\n", strip=True)
    else:
        text = soup.get_text(separator="\n", strip=True)

    # Clean up excessive newlines
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text[:max_chars]

    return {"url": url, "title": title, "text": text,
            "content_length": len(text), "method": "bs4"}


def _extract_with_regex(url, html, max_chars):
    """Fallback extraction using regex (no BS4)."""
    # Get title
    title_m = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
    title = unescape(_strip_html(title_m.group(1))) if title_m else ""

    # Remove script and style blocks
    text = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', html, flags=re.I)
    text = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', text, flags=re.I)
    text = _strip_html(text)
    text = unescape(text)
    text = re.sub(r'\s{3,}', '\n\n', text)
    text = text[:max_chars]

    return {"url": url, "title": title, "text": text,
            "content_length": len(text), "method": "regex"}


# ---------------------------------------------------------------------------
# Semantic Scholar API (already proven working)
# ---------------------------------------------------------------------------

SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1"


def search_papers(query, limit=5, fields=None):
    """Search Semantic Scholar for papers."""
    if fields is None:
        fields = "title,authors,year,citationCount,externalIds,url"
    params = {"query": query, "limit": str(limit), "fields": fields}
    url = SEMANTIC_SCHOLAR_API + "/paper/search?" + urlencode(params)

    if HAS_REQUESTS:
        r = requests.get(url, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        return r.json()
    else:
        result = subprocess.run(
            ["curl", "-sL", "--max-time", str(DEFAULT_TIMEOUT), url],
            capture_output=True, text=True, timeout=DEFAULT_TIMEOUT + 5)
        return json.loads(result.stdout)


def get_author(author_id, fields=None):
    """Get Semantic Scholar author profile."""
    if fields is None:
        fields = "name,affiliations,homepage,paperCount,citationCount,hIndex"
    url = (SEMANTIC_SCHOLAR_API + f"/author/{author_id}"
           + "?" + urlencode({"fields": fields}))

    if HAS_REQUESTS:
        r = requests.get(url, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        return r.json()
    else:
        result = subprocess.run(
            ["curl", "-sL", "--max-time", str(DEFAULT_TIMEOUT), url],
            capture_output=True, text=True, timeout=DEFAULT_TIMEOUT + 5)
        return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 web_research.py search 'query'")
        print("  python3 web_research.py fetch 'https://url'")
        print("  python3 web_research.py papers 'query'")
        print("  python3 web_research.py status")
        sys.exit(1)

    cmd = sys.argv[1]
    arg = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""

    if cmd == "search":
        result = search(arg)
        print(json.dumps(result, indent=2))

    elif cmd == "fetch":
        result = fetch_url(arg)
        print(f"Title: {result['title']}")
        print(f"Method: {result['method']}")
        print(f"Length: {result['content_length']} chars")
        print("---")
        print(result['text'][:5000])

    elif cmd == "papers":
        result = search_papers(arg)
        print(json.dumps(result, indent=2))

    elif cmd == "status":
        print(f"requests: {'yes' if HAS_REQUESTS else 'no'}")
        print(f"bs4: {'yes' if HAS_BS4 else 'no'}")
        print(f"searxng: {'configured' if SEARXNG_BASE_URL else 'not configured'}")
        print(f"brave: {'configured' if BRAVE_API_KEY else 'not configured'}")
        print(f"gemini: {'configured' if GEMINI_API_KEY else 'not configured'}")
        providers = []
        if SEARXNG_BASE_URL:
            providers.append("searxng")
        providers.append("ddg")
        if BRAVE_API_KEY:
            providers.append("brave")
        if GEMINI_API_KEY:
            providers.append("gemini")
        print(f"search chain: {' -> '.join(providers)}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
