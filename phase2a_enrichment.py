"""
Phase 2A: Tavily enrichment.
Provides lightweight web-search context for downstream LLM research.
"""

import config


def enrich_via_tavily(name, company=""):
    """Search Tavily with key rotation and return combined result snippets."""
    from tavily import TavilyClient

    if not config.TAVILY_API_KEYS:
        return ""

    query = f"{name} LinkedIn IIM Udaipur"
    if company:
        query += f" {company} current role"

    for key in config.TAVILY_API_KEYS:
        try:
            client = TavilyClient(api_key=key)
            response = client.search(query, search_depth="basic", max_results=3)
            results = response.get("results", [])
            if not results:
                continue

            snippets = [r.get("content", "")[:500] for r in results[:3] if r.get("content")]
            combined = "\n".join(snippets)
            if len(combined.strip()) > 30:
                return combined
        except Exception as e:
            print(f"  [Tavily] Key failed, trying next... ({e})")
            continue

    return ""


def enrich_person(name, company="", linkedin_url=""):
    """Return Tavily enrichment text or fall back to the base template path."""
    print(f"  [Enrich] Trying Tavily for {name}...")
    tavily_text = enrich_via_tavily(name, company)
    if tavily_text:
        print(f"  [Enrich] Tavily success for {name}")
        return tavily_text, "tavily"

    print(f"  [Enrich] No enrichment found for {name} — using base template")
    return "", "base_template"


if __name__ == "__main__":
    # Quick test
    text, source = enrich_person(
        "Rahul Sharma",
        company="McKinsey",
        linkedin_url="https://linkedin.com/in/rahulsharma",
    )
    print(f"\nSource: {source}")
    print(f"Text: {text[:300] if text else '(empty)'}")
