"""
Phase 2A: LLM-powered alumni research agent.
Uses Qwen 3 Max (via OpenRouter) with web search to research each alumni
following the Alum Search Skill protocol. Outputs structured, verified profiles.
"""

import json
import re
from pathlib import Path

from openai import OpenAI

import config
from phase2a_enrichment import enrich_person

_SKILL_PATH = Path(__file__).parent / "prompts" / "alum_search_skill.txt"
with open(_SKILL_PATH, encoding="utf-8") as f:
    _SKILL_PROMPT = f.read()

_SYSTEM_INSTRUCTION_PREFIX = (
    "Output ONLY the valid XML block wrapped in <PROFILE> tags."
)

_PROFILE_OUTPUT_FORMAT = """
At the very end of your response, after all reasoning and research notes, output one structured block in this exact format — and nothing else after it:

<PROFILE>
CONFIDENCE: Very High | High | Medium | Low | Unconfirmed
SOURCES_USED: [comma-separated list]
LINKEDIN_URL: [url or leave blank]
BATCH: [graduation year or leave blank]
ROLE: [current job title or leave blank]
COMPANY: [current employer or leave blank]
LOCATION: [city, country or leave blank]
DOMAIN: [industry domain or leave blank]
CAREER_HISTORY: [one-line summary or leave blank]
FLAGS: [pipe-separated concerns or None]
OUTREACH_NOTE: [one sentence for email writer or leave blank]
HOOK1: [specific verifiable hook or leave blank]
HOOK2: [specific verifiable hook or leave blank]
</PROFILE>

Rules:
- The <PROFILE> block must appear exactly once, at the very end.
- Every key must be present even if the value is blank.
- Each key occupies exactly one line — no multi-line values.
- If you are unsure about COMPANY, leave it blank. Do not guess.
- Do not add any text after </PROFILE>.
""".strip()

_EMPTY_MARKERS = {"blank", "none", "n/a", "-", "unknown"}
_CONFIDENCE_ORDER = ["very_high", "high", "medium", "low", "unconfirmed"]

_client = OpenAI(
    base_url=config.OPENROUTER_BASE_URL,
    api_key=config.OPENROUTER_API_KEY,
)


def research_alumni(
    name: str,
    batch: str = "",
    last_known_role: str = "",
    location: str = "",
    profile_url: str = "",
) -> dict:
    """
    Send one alumni entry to the LLM research agent.
    Returns a parsed profile dict with a `raw_profile` key on success.
    Returns None on total failure or unusable parser output.
    """
    enrichment_text, _ = enrich_person(name, last_known_role, profile_url)
    user_msg = _build_user_message(
        name,
        batch,
        last_known_role,
        location,
        profile_url,
        enrichment_text,
    )

    try:
        response = _client.chat.completions.create(
            model=config.OPENROUTER_MODEL,
            messages=[
                {"role": "system", "content": f"{_SYSTEM_INSTRUCTION_PREFIX}\n\n{_SKILL_PROMPT}"},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=2048,
            temperature=1.0,
        )

        raw = response.choices[0].message.content.strip()
        profile = _parse_profile(raw, name=name)
        if not profile:
            print(f"  [Research] Parser failed for {name}. Raw LLM response ({len(raw)} chars):")
            print("  --- RAW RESPONSE START ---")
            print(raw)
            print("  --- RAW RESPONSE END ---")
            return None
        profile["raw_profile"] = raw
        return profile

    except Exception as e:
        print(f"  [Research] API error for {name}: {e}")
        return None


def _build_user_message(name, batch, role, location, profile_url, enrichment_text):
    """Build the user prompt with all available alumni data."""
    lines = [
        "Research the following alumni and produce a structured profile.",
        "Use the provided web-search context to verify their current status.",
        "",
        f"Name: {name}",
        f"Batch: {batch}" if batch else "Batch: Unknown",
        f"Last known role (from AlmaConnect): {role}" if role else "Last known role: Unknown",
        f"Last known location: {location}" if location else "Last known location: Unknown",
        f"AlmaConnect profile: {profile_url}" if profile_url else "AlmaConnect profile: Not available",
        "",
        "Institution: IIM Udaipur",
        "",
        "--- WEB SEARCH CONTEXT ---",
        enrichment_text if enrichment_text else "(No web search context available)",
        "",
        "Follow every step in your instructions.",
        "",
        _PROFILE_OUTPUT_FORMAT,
    ]
    return "\n".join(lines)


def _sanitize_value(value: str) -> str:
    text = (value or "").strip()
    if text.lower() in _EMPTY_MARKERS:
        return ""
    return text


def _downgrade_confidence_level(level: str) -> str:
    if level not in _CONFIDENCE_ORDER:
        return level

    index = _CONFIDENCE_ORDER.index(level)
    if index >= len(_CONFIDENCE_ORDER) - 1:
        return _CONFIDENCE_ORDER[-1]
    return _CONFIDENCE_ORDER[index + 1]


def _extract_profile_block(raw: str) -> dict | None:
    matches = re.findall(r"<PROFILE>(.*?)</PROFILE>", raw, re.DOTALL)
    if not matches:
        return None

    block_text = matches[-1]
    block = {}
    for line in block_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        block[key.strip()] = value.strip()
    return block


def _parse_profile(raw: str, name: str = "") -> dict | None:
    """Parse the final <PROFILE> block into a normalized profile dict."""
    block = _extract_profile_block(raw)
    if block:
        p = _parse_profile_block(block)
    else:
        if "<PROFILE>" in raw and "</PROFILE>" not in raw:
            recovered_block = _extract_profile_block(f"{raw}</PROFILE>")
            if recovered_block:
                recovered_name = name or "unknown alumnus"
                print(f"[Phase2A] WARNING: Recovered truncated PROFILE block for {recovered_name}.")
                p = _parse_profile_block(recovered_block)
            else:
                p = _parse_profile_from_jsonish(raw)
        else:
            p = _parse_profile_from_jsonish(raw)
        if not p:
            print("[Parser] No <PROFILE> block or usable JSON found — treating as unusable")
            return None

    if p["company"] and _classify_confidence(p["confidence"]) in {"low", "unconfirmed"}:
        p["company"] = ""

    confidence_level = _classify_confidence(p["confidence"])
    flags_upper = p["flags"].upper()
    if "VERIFY" in flags_upper or "UNCONFIRMED" in flags_upper:
        confidence_level = _downgrade_confidence_level(confidence_level)

    p["confidence_level"] = confidence_level
    return p


def _parse_profile_block(block: dict) -> dict:
    """Normalize parsed profile key/value pairs into the internal profile shape."""
    hooks = []
    email_hooks = block.get("EMAIL_HOOKS", [])
    if isinstance(email_hooks, list):
        hooks.extend(_sanitize_value(str(value)) for value in email_hooks if _sanitize_value(str(value)))

    for key, value in block.items():
        if key.upper().startswith("HOOK") and _sanitize_value(str(value)):
            hooks.append(_sanitize_value(str(value)))

    alias_map = {key.upper(): value for key, value in block.items()}
    return {
        "confidence": _sanitize_value(str(alias_map.get("CONFIDENCE", alias_map.get("confidence", "")))),
        "sources_used": _sanitize_value(str(alias_map.get("SOURCES_USED", alias_map.get("sources_used", "")))),
        "linkedin_url": _sanitize_value(str(alias_map.get("LINKEDIN_URL", alias_map.get("linkedin_url", "")))),
        "batch_verified": _sanitize_value(str(alias_map.get("BATCH", alias_map.get("batch", "")))),
        "current_role": _sanitize_value(str(alias_map.get("ROLE", alias_map.get("current_role", alias_map.get("role", ""))))),
        "company": _sanitize_value(str(alias_map.get("COMPANY", alias_map.get("company", "")))),
        "location": _sanitize_value(str(alias_map.get("LOCATION", alias_map.get("location", "")))),
        "domain": _sanitize_value(str(alias_map.get("DOMAIN", alias_map.get("domain", "")))),
        "career_history": _sanitize_value(str(alias_map.get("CAREER_HISTORY", alias_map.get("career_history", "")))),
        "flags": _sanitize_value(str(alias_map.get("FLAGS", alias_map.get("flags", "")))),
        "outreach_note": _sanitize_value(str(alias_map.get("OUTREACH_NOTE", alias_map.get("outreach_note", "")))),
        "email_hooks": hooks,
    }


def _parse_profile_from_jsonish(raw: str) -> dict | None:
    """Try to recover a profile from JSON-like content when <PROFILE> tags are missing."""
    for candidate in _extract_json_candidates(raw):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue

        if isinstance(parsed, dict):
            return _parse_profile_block(parsed)

    return None


def _extract_json_candidates(raw: str) -> list[str]:
    """Return possible JSON object strings found in a model response."""
    candidates = []
    candidates.extend(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL))

    start = raw.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(raw)):
            char = raw[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(raw[start:index + 1])
                    break
        start = raw.find("{", start + 1)

    seen = []
    for candidate in candidates:
        stripped = candidate.strip()
        if stripped and stripped not in seen:
            seen.append(stripped)
    return seen


def _classify_confidence(conf_text: str) -> str:
    """Map the free-text confidence to a category."""
    t = conf_text.lower()
    if "very high" in t:
        return "very_high"
    if "high" in t:
        return "high"
    if "medium" in t:
        return "medium"
    if "low" in t:
        return "low"
    if "unconfirmed" in t:
        return "unconfirmed"
    return "unknown"


def is_profile_usable(profile: dict) -> bool:
    """A profile is usable for outreach if confidence >= Medium."""
    return profile["confidence_level"] in ("very_high", "high", "medium")


def get_safe_hooks(profile: dict) -> list:
    """
    Return only email hooks that are safe to use given confidence level.
    - High/Very High: all hooks
    - Medium: campus/batch hooks only (skip career hooks)
    - Low/Unconfirmed: empty — use base template
    """
    level = profile["confidence_level"]
    hooks = profile.get("email_hooks", [])

    if level in ("very_high", "high"):
        return hooks
    if level == "medium":
        safe = []
        for hook in hooks:
            hook_lower = hook.lower()
            if any(word in hook_lower for word in ("campus", "batch", "alumni", "iim", "alma mater")):
                safe.append(hook)
        return safe if safe else hooks[:1]
    return []


if __name__ == "__main__":
    import json

    print("Testing alumni research agent (Qwen 3 Max + web search)...")
    print("Researching: Gaurav Singh, PGDM '15\n")

    result = research_alumni(
        name="Gaurav Singh",
        batch="PGDM '15",
        last_known_role="Operations, Strategy, Business Development",
        location="Bangalore",
        profile_url="https://iimu.almaconnect.com/profiles/gaurav-singh-951",
    )

    if result:
        print("--- PARSED PROFILE ---")
        display = {k: v for k, v in result.items() if k != "raw_profile"}
        print(json.dumps(display, indent=2, ensure_ascii=False))
        print(f"\nUsable for outreach: {is_profile_usable(result)}")
        print(f"Safe hooks: {get_safe_hooks(result)}")
        print(f"\n--- RAW LLM OUTPUT ---\n{result['raw_profile']}")
    else:
        print("Research FAILED.")
