"""Clean structural extraction using Scrapling (read-only, no browser launch).

Runtime purity (CLAUDE.md §0)
------------------------------
Scrapling is a runtime-only dep in the [web] extra. This module is safe to
import in CI without Scrapling installed — all Scrapling imports are *lazy*
(inside functions, guarded by try/except ImportError). When Scrapling is absent
the functions return a graceful "scraping unavailable" dict.

Security (CLAUDE.md §3 + ADR-0008)
------------------------------------
- Only the lightweight `Fetcher` (HTTP-only, no browser) is used here. It
  makes no outbound connections beyond the target URL, respects robots.txt
  implicitly via HTTP semantics, and launches no browser process.
- No credentials, cookies, or page HTML are logged. Only URL host, HTTP status
  code, and byte/element counts are emitted at DEBUG level.
- Form submission and state-changing POST requests are NOT in this module; they
  belong to the browser-use agent path.

Routing heuristic
-----------------
`web/scrape.py` is the *cheap, read-only* path: fetching a URL and returning
structured text/links/metadata without launching a browser. Use this when:
  - The task is clearly "read this URL" or "get the text from X".
  - No JavaScript rendering or interaction is needed.
  - No authentication or multi-step navigation is required.

For anything interactive or multi-step, the caller should route to
`web/agent.py` (browser-use).
"""

from __future__ import annotations

import logging
from typing import TypedDict
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Typed result structures
# ---------------------------------------------------------------------------


class ScrapeResult(TypedDict):
    """Structured extraction result returned to the brain."""

    url: str
    """Canonical URL that was fetched."""

    title: str
    """Page <title> text, or empty string if unavailable."""

    text: str
    """Visible body text, whitespace-normalised, truncated to _MAX_TEXT_CHARS."""

    links: list[str]
    """Absolute href values of <a> elements found on the page."""

    error: str
    """Non-empty when extraction failed; all other fields may be empty/default."""


class ScrapeUnavailableResult(TypedDict):
    """Returned when Scrapling is not installed."""

    url: str
    error: str


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_TEXT_CHARS: int = 8_000
"""Maximum characters of body text returned to the brain.

Keeps the result token-friendly while preserving most of the relevant content.
The brain will summarise or quote from this rather than receiving raw HTML.
"""

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _host(url: str) -> str:
    """Return just the hostname for metadata-safe logging."""
    try:
        return urlparse(url).netloc or url[:80]
    except Exception:
        return url[:80]


def _scrapling_available() -> bool:
    """Return True only if Scrapling can be imported successfully."""
    try:
        import scrapling  # noqa: F401

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_page(url: str, *, selector: str | None = None) -> ScrapeResult:
    """Fetch *url* with the lightweight Scrapling HTTP fetcher and extract content.

    Parameters
    ----------
    url:
        Fully-qualified HTTP/HTTPS URL to fetch.
    selector:
        Optional CSS selector. When provided, only elements matching this
        selector are included in the ``text`` field (useful for targeted
        extraction, e.g. ``"article"`` or ``"#main-content"``). When absent,
        the full body text is extracted.

    Returns
    -------
    ScrapeResult
        Typed dict with ``url``, ``title``, ``text``, ``links``, and
        ``error`` fields. ``error`` is non-empty on failure.

    Notes
    -----
    - Uses Scrapling's ``Fetcher`` (curl_cffi-based HTTP, no browser process).
    - Scrapling is imported lazily — safe to call from CI without the [web]
      extra installed (returns a graceful error dict instead of crashing).
    - Only HTTP status, byte length, and element count are logged. Never logs
      page text, credentials, or PII.
    """
    empty: ScrapeResult = {
        "url": url,
        "title": "",
        "text": "",
        "links": [],
        "error": "",
    }

    # --- Lazy import guard ---------------------------------------------------
    try:
        from scrapling.fetchers import Fetcher  # type: ignore[import-not-found]
    except ImportError:
        logger.debug("Scrapling not installed — scraping unavailable for %s", _host(url))
        empty["error"] = (
            "Scrapling is not installed. Install the [web] extra: "
            "pip install 'friday[web]'"
        )
        return empty

    # --- Fetch ---------------------------------------------------------------
    try:
        fetcher = Fetcher(auto_match=False)
        response = fetcher.get(url, stealthy_headers=True, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Scrapling fetch failed for host=%s: %s", _host(url), type(exc).__name__)
        empty["error"] = f"Fetch error: {type(exc).__name__}: {exc}"
        return empty

    logger.debug(
        "Scrapling fetched host=%s status=%s bytes=%d",
        _host(url),
        getattr(response, "status", "?"),
        len(getattr(response, "content", b"")),
    )

    # --- Extract content -----------------------------------------------------
    try:
        # Title
        title_els = response.css("title")
        title: str = title_els[0].text if title_els else ""

        # Text: targeted or full body
        if selector:
            target_els = response.css(selector)
            raw_text = " ".join(el.text for el in target_els if el.text)
        else:
            body_els = response.css("body")
            raw_text = body_els[0].text if body_els else ""

        # Normalise whitespace and cap length
        text = " ".join(raw_text.split())[:_MAX_TEXT_CHARS]

        # Links — absolute hrefs only
        link_els = response.css("a[href]")
        links: list[str] = []
        base_scheme_host = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        for el in link_els:
            href: str = el.attrib.get("href", "")
            if href.startswith("http://") or href.startswith("https://"):
                links.append(href)
            elif href.startswith("/"):
                links.append(base_scheme_host + href)
            # Fragment-only (#anchor) and relative paths are skipped.

        logger.debug(
            "Scrapling extracted host=%s title_len=%d text_len=%d links=%d",
            _host(url),
            len(title),
            len(text),
            len(links),
        )

        return {
            "url": url,
            "title": title,
            "text": text,
            "links": links[:200],  # cap link list
            "error": "",
        }

    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "Scrapling extraction error for host=%s: %s", _host(url), type(exc).__name__
        )
        empty["error"] = f"Extraction error: {type(exc).__name__}: {exc}"
        return empty


def scrape_summary(url: str, *, selector: str | None = None) -> str:
    """Convenience wrapper: fetch *url* and return a brain-ready text summary.

    Returns a plain string so the caller can feed it directly into tool-call
    result routing without unpacking a dict.
    """
    result = fetch_page(url, selector=selector)
    if result["error"]:
        return f"(scrape error) {result['error']}"

    parts: list[str] = []
    if result["title"]:
        parts.append(f"Title: {result['title']}")
    if result["text"]:
        parts.append(f"Text: {result['text']}")
    if result["links"]:
        parts.append(f"Links ({len(result['links'])}): " + ", ".join(result["links"][:10]))
    return "\n".join(parts) if parts else "(no content extracted)"
