"""
Phase 2A: Tavily enrichment.
Provides lightweight web-search context for downstream LLM research.
"""

import asyncio
import logging
import re

import config
from utils.retry import async_retry_with_backoff, describe_exception, retry_with_backoff
from utils.run_context import log_event

_CREDIT_EXHAUSTION_KEYWORDS = (
    "insufficient credits",
    "credit",
    "credits exhausted",
    "quota exceeded",
)
_EXHAUSTED_TAVILY_KEYS: set[str] = set()
log = logging.getLogger(__name__)
_CONFIDENCE_RE = re.compile(
    r"^\s*CONFIDENCE:\s*(Very High|High|Medium|Low|Unconfirmed)\b",
    re.IGNORECASE | re.MULTILINE,
)
_CONFIDENCE_MAP = {
    "very high": "very_high",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "unconfirmed": "unconfirmed",
}


# FROZEN — unreachable unless TAVILY_FROZEN=false.
def _clean_tavily_snippet(text: str) -> str:
    """Remove empty N/A lines from Tavily snippets before passing to the LLM."""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if stripped in ("", "N/A", "None"):
            continue
        if stripped.endswith(": N/A") or stripped.endswith(": None"):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def extract_confidence_level(profile_text: str) -> str:
    """Extract and normalize CONFIDENCE tier from a raw profile block."""
    if not profile_text:
        return ""
    match = _CONFIDENCE_RE.search(profile_text)
    if not match:
        return ""
    raw = match.group(1).strip().lower()
    normalized = _CONFIDENCE_MAP.get(raw, "")
    if normalized:
        log.info("Extracted confidence level: %s", normalized)
    return normalized


class TavilyCreditExhaustedError(RuntimeError):
    """Raised when Tavily rejects a key due to exhausted credits."""


class AllTavilyKeysExhaustedError(RuntimeError):
    """Raised when every configured Tavily key has exhausted credits."""


def clear_tavily_exhaustion_state() -> None:
    _EXHAUSTED_TAVILY_KEYS.clear()
    log.info("Tavily exhaustion state cleared for new run")


# FROZEN — unreachable unless TAVILY_FROZEN=false.
def _is_credit_exhaustion_error(error: Exception) -> tuple[bool, int | None, str]:
    status_code, snippet = describe_exception(error, limit=500)
    snippet_lower = snippet.lower()
    is_credit_exhausted = status_code == 429 and any(
        keyword in snippet_lower for keyword in _CREDIT_EXHAUSTION_KEYWORDS
    )
    return is_credit_exhausted, status_code, snippet


# FROZEN — unreachable unless TAVILY_FROZEN=false.
def _build_tavily_query(name: str, graduation_year: str = "", company: str = "") -> str:
    parts = [name.strip(), "IIM Udaipur"]
    if graduation_year:
        parts.append(graduation_year.strip())
    parts.append("LinkedIn")
    if company:
        parts.append(company.strip())
    return " ".join(part for part in parts if part)


# FROZEN — unreachable unless TAVILY_FROZEN=false.
def _extract_linkedin_url(results: list[dict]) -> str:
    for result in results:
        url = (result.get("url", "") or "").strip()
        if re.search(r"linkedin\.com/in/", url, re.IGNORECASE):
            return url
    return ""


# FROZEN — unreachable unless TAVILY_FROZEN=false.
@retry_with_backoff(max_attempts=3, base_delay=1.0, error_type="api")
def _search_tavily(api_key: str, query: str) -> dict:
    from tavily import TavilyClient

    client = TavilyClient(api_key=api_key)
    try:
        return client.search(
            query,
            search_depth=config.TAVILY_SEARCH_DEPTH,
            max_results=config.TAVILY_MAX_RESULTS,
            chunks_per_source=config.TAVILY_CHUNKS_PER_SOURCE,
        )
    except Exception as error:
        is_credit_exhausted, _, snippet = _is_credit_exhaustion_error(error)
        if is_credit_exhausted:
            raise TavilyCreditExhaustedError(snippet) from error
        raise


# FROZEN — unreachable unless TAVILY_FROZEN=false.
def enrich_via_tavily(name, company="", graduation_year=""):
    """Search Tavily with key rotation and return combined result snippets plus metadata."""
    if not config.TAVILY_API_KEYS:
        return "", "", {"chunk_count": 0, "search_depth": config.TAVILY_SEARCH_DEPTH}

    query = _build_tavily_query(name, graduation_year=graduation_year, company=company)
    print(f"  [Tavily] Query: {query}")

    for key in config.TAVILY_API_KEYS:
        if key in _EXHAUSTED_TAVILY_KEYS:
            continue

        try:
            response = _search_tavily(key, query)
            results = response.get("results", [])
            if not results:
                continue

            snippet_records = []
            for result in results[: config.TAVILY_MAX_RESULTS]:
                content = (result.get("content", "") or "").strip()
                if not content:
                    continue
                snippet_records.append(
                    {
                        "title": (result.get("title", "") or "").strip(),
                        "url": (result.get("url", "") or "").strip(),
                        "content": _clean_tavily_snippet(content)[:1000],
                    }
                )

            linkedin_url = _extract_linkedin_url(results)
            combined = "\n\n".join(
                "\n".join(
                    part
                    for part in [
                        f"TITLE: {record['title']}" if record["title"] else "",
                        f"URL: {record['url']}" if record["url"] else "",
                        f"SNIPPET: {record['content']}",
                    ]
                    if part
                )
                for record in snippet_records
            )
            if len(combined.strip()) > 30:
                metadata = {
                    "chunk_count": len(snippet_records),
                    "search_depth": config.TAVILY_SEARCH_DEPTH,
                    "linkedin_url_found": bool(linkedin_url),
                    "query": query,
                    "source_urls": [record["url"] for record in snippet_records if record["url"]],
                    "raw_results": snippet_records,
                }
                log_event(
                    phase="2A",
                    api_called="Tavily",
                    error_type="INFO",
                    raw_response_snippet=(
                        f"search_depth={config.TAVILY_SEARCH_DEPTH}; "
                        f"chunks={metadata['chunk_count']}; "
                        f"linkedin_url_found={metadata['linkedin_url_found']}; query={query}"
                    ),
                )
                return combined, linkedin_url, metadata
        except TavilyCreditExhaustedError as error:
            _EXHAUSTED_TAVILY_KEYS.add(key)
            log_event(
                phase="2A",
                api_called="Tavily",
                http_status=429,
                error_type="TAVILY_CREDITS_EXHAUSTED",
                raw_response_snippet=str(error),
            )
            print("  [Tavily] Key credits exhausted, rotating to next key...")
            continue
        except Exception as error:
            status_code, snippet = describe_exception(error, limit=500)
            log_event(
                phase="2A",
                api_called="Tavily",
                http_status=status_code,
                error_type=type(error).__name__,
                raw_response_snippet=snippet,
            )
            print(f"  [Tavily] Non-credit error from current key; not rotating. ({error})")
            raise

    if config.TAVILY_API_KEYS and len(_EXHAUSTED_TAVILY_KEYS) == len(config.TAVILY_API_KEYS):
        log_event(
            phase="2A",
            api_called="Tavily",
            http_status=429,
            error_type="ALL_TAVILY_KEYS_EXHAUSTED",
            raw_response_snippet="All configured Tavily keys reported credit exhaustion.",
        )
        raise AllTavilyKeysExhaustedError("All configured Tavily keys have exhausted credits.")

    return "", "", {"chunk_count": 0, "search_depth": config.TAVILY_SEARCH_DEPTH}


def enrich_person(name, company="", graduation_year="", linkedin_url=""):
    """Return Tavily enrichment text, source, extracted LinkedIn URL, and metadata."""
    # --- Tavily freeze guard ---
    if config.TAVILY_FROZEN:
        print(f"  [Enrich] Tavily FROZEN — skipping enrichment for {name}")
        return "", "frozen", linkedin_url or "", {"chunk_count": 0, "frozen": True}
    # --- end freeze guard ---
    # FROZEN — unreachable unless TAVILY_FROZEN=false.
    print(f"  [Enrich] Trying Tavily for {name}...")
    tavily_text, extracted_linkedin_url, metadata = enrich_via_tavily(
        name,
        company=company,
        graduation_year=graduation_year,
    )
    resolved_linkedin_url = extracted_linkedin_url or linkedin_url or ""
    if tavily_text:
        print(f"  [Enrich] Tavily success for {name}")
        return tavily_text, "tavily", resolved_linkedin_url, metadata

    print(f"  [Enrich] No enrichment found for {name} — using base template")
    return "", "base_template", resolved_linkedin_url, metadata


# ---------------------------------------------------------------------------
# Async variants (used by async orchestrator)
# ---------------------------------------------------------------------------


# FROZEN — unreachable unless TAVILY_FROZEN=false.
@async_retry_with_backoff(max_attempts=3, base_delay=1.0, error_type="api")
async def _search_tavily_async(api_key: str, query: str) -> dict:
    """Run Tavily search in a thread so the event loop stays free."""
    from tavily import TavilyClient

    tc = TavilyClient(api_key=api_key)
    try:
        return await asyncio.to_thread(
            tc.search,
            query,
            search_depth=config.TAVILY_SEARCH_DEPTH,
            max_results=config.TAVILY_MAX_RESULTS,
            chunks_per_source=config.TAVILY_CHUNKS_PER_SOURCE,
        )
    except Exception as error:
        is_credit_exhausted, _, snippet = _is_credit_exhaustion_error(error)
        if is_credit_exhausted:
            raise TavilyCreditExhaustedError(snippet) from error
        raise


# FROZEN — unreachable unless TAVILY_FROZEN=false.
async def enrich_via_tavily_async(name, company="", graduation_year="", *, tavily_sem=None):
    """Async key-rotation Tavily search.  Mirrors ``enrich_via_tavily``."""
    if not config.TAVILY_API_KEYS:
        return "", "", {"chunk_count": 0, "search_depth": config.TAVILY_SEARCH_DEPTH}

    query = _build_tavily_query(name, graduation_year=graduation_year, company=company)
    print(f"  [Tavily] Query: {query}")

    for key in config.TAVILY_API_KEYS:
        if key in _EXHAUSTED_TAVILY_KEYS:
            continue

        try:
            if tavily_sem:
                async with tavily_sem:
                    response = await _search_tavily_async(key, query)
            else:
                response = await _search_tavily_async(key, query)

            results = response.get("results", [])
            if not results:
                continue

            snippet_records = []
            for result in results[: config.TAVILY_MAX_RESULTS]:
                content = (result.get("content", "") or "").strip()
                if not content:
                    continue
                snippet_records.append(
                    {
                        "title": (result.get("title", "") or "").strip(),
                        "url": (result.get("url", "") or "").strip(),
                        "content": _clean_tavily_snippet(content)[:1000],
                    }
                )

            linkedin_url = _extract_linkedin_url(results)
            combined = "\n\n".join(
                "\n".join(
                    part
                    for part in [
                        f"TITLE: {record['title']}" if record["title"] else "",
                        f"URL: {record['url']}" if record["url"] else "",
                        f"SNIPPET: {record['content']}",
                    ]
                    if part
                )
                for record in snippet_records
            )
            if len(combined.strip()) > 30:
                metadata = {
                    "chunk_count": len(snippet_records),
                    "search_depth": config.TAVILY_SEARCH_DEPTH,
                    "linkedin_url_found": bool(linkedin_url),
                    "query": query,
                    "source_urls": [record["url"] for record in snippet_records if record["url"]],
                    "raw_results": snippet_records,
                }
                log_event(
                    phase="2A",
                    api_called="Tavily",
                    error_type="INFO",
                    raw_response_snippet=(
                        f"search_depth={config.TAVILY_SEARCH_DEPTH}; "
                        f"chunks={metadata['chunk_count']}; "
                        f"linkedin_url_found={metadata['linkedin_url_found']}; query={query}"
                    ),
                )
                return combined, linkedin_url, metadata
        except TavilyCreditExhaustedError as error:
            _EXHAUSTED_TAVILY_KEYS.add(key)
            log_event(
                phase="2A",
                api_called="Tavily",
                http_status=429,
                error_type="TAVILY_CREDITS_EXHAUSTED",
                raw_response_snippet=str(error),
            )
            print("  [Tavily] Key credits exhausted, rotating to next key...")
            continue
        except Exception as error:
            status_code, snippet = describe_exception(error, limit=500)
            log_event(
                phase="2A",
                api_called="Tavily",
                http_status=status_code,
                error_type=type(error).__name__,
                raw_response_snippet=snippet,
            )
            print(f"  [Tavily] Non-credit error from current key; not rotating. ({error})")
            raise

    if config.TAVILY_API_KEYS and len(_EXHAUSTED_TAVILY_KEYS) == len(config.TAVILY_API_KEYS):
        log_event(
            phase="2A",
            api_called="Tavily",
            http_status=429,
            error_type="ALL_TAVILY_KEYS_EXHAUSTED",
            raw_response_snippet="All configured Tavily keys reported credit exhaustion.",
        )
        raise AllTavilyKeysExhaustedError("All configured Tavily keys have exhausted credits.")

    return "", "", {"chunk_count": 0, "search_depth": config.TAVILY_SEARCH_DEPTH}


async def enrich_person_async(name, company="", graduation_year="", linkedin_url="", *, tavily_sem=None):
    """Async version of ``enrich_person``."""
    # --- Tavily freeze guard ---
    if config.TAVILY_FROZEN:
        print(f"  [Enrich] Tavily FROZEN — skipping enrichment for {name}")
        return "", "frozen", linkedin_url or "", {"chunk_count": 0, "frozen": True}
    # --- end freeze guard ---
    # FROZEN — unreachable unless TAVILY_FROZEN=false.
    print(f"  [Enrich] Trying Tavily for {name}...")
    tavily_text, extracted_linkedin_url, metadata = await enrich_via_tavily_async(
        name,
        company=company,
        graduation_year=graduation_year,
        tavily_sem=tavily_sem,
    )
    resolved_linkedin_url = extracted_linkedin_url or linkedin_url or ""
    if tavily_text:
        print(f"  [Enrich] Tavily success for {name}")
        return tavily_text, "tavily", resolved_linkedin_url, metadata

    print(f"  [Enrich] No enrichment found for {name} — using base template")
    return "", "base_template", resolved_linkedin_url, metadata


if __name__ == "__main__":
    # Quick test
    text, source, linkedin_url, metadata = enrich_person(
        "Rahul Sharma",
        company="McKinsey",
        graduation_year="2022",
        linkedin_url="https://linkedin.com/in/rahulsharma",
    )
    print(f"\nSource: {source}")
    print(f"LinkedIn URL: {linkedin_url or '(none)'}")
    print(f"Metadata: {metadata}")
    print(f"Text: {text[:300] if text else '(empty)'}")
