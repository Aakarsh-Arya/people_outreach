"""Phase 2A: provider-agnostic alumni research agent with Tavily enrichment."""

import asyncio
import functools
import json
import re
from pathlib import Path

import config
from config import client
from phase2a_enrichment import AllTavilyKeysExhaustedError, enrich_person, enrich_person_async
from utils.gemini_native import call_gemini_native, call_gemini_native_async
from utils.retry import async_retry_with_backoff, describe_exception, retry_with_backoff, with_retry, with_retry_async
from utils.run_context import log_event, log_raw_llm_response

_SKILL_PATH = Path(__file__).parent / "prompts" / "alum_search_skill.txt"
_KIMI_WEB_SEARCH_TOOL = [{"type": "builtin_function", "function": {"name": "$web_search"}}]


@functools.lru_cache(maxsize=1)
def _load_skill_prompt() -> str:
    try:
        return _SKILL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Prompt file not found: {_SKILL_PATH}. "
            f"Ensure the prompts/ directory contains alum_search_skill.txt."
        )

_SYSTEM_INSTRUCTION_PREFIX = (
    "Return ONLY one complete <PROFILE>...</PROFILE> block. "
    "Do not include reasoning, notes, analysis, markdown, or any text before or after the block. "
    "You MUST include the closing </PROFILE> tag."
)

_PROFILE_OUTPUT_FORMAT = """
Output exactly one structured block in this exact format:

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
- Do not include any reasoning or research notes before the block.
""".strip()

_EMPTY_MARKERS = {"blank", "none", "n/a", "-", "unknown"}
_CONFIDENCE_ORDER = ["very_high", "high", "medium", "low", "unconfirmed"]
_UNCONFIRMED_FLAG_MARKERS = {
    "UNCONFIRMED",
    "EDUCATION_MISMATCH",
}
_FORCE_UNCONFIRMED_FLAG_MARKERS = {
    "IDENTITY_UNCONFIRMED",
    "WRONG_PERSON",
}
_DOWNGRADE_FLAG_MARKERS = {
    "BATCH_YEAR_MISMATCH",
}
_VERIFY_COMPANY_FLAG_MARKERS = {
    "VERIFY CURRENT EMPLOYER",
    "VERIFY_CURRENT_EMPLOYER",
}


class ProfileFenceError(ValueError):
    """Raised when the model response is missing the required PROFILE fence."""

    def __init__(self, raw_response: str):
        super().__init__("Missing required <PROFILE> fence in research response.")
        self.raw_response = raw_response


def _serialize_assistant_message(message) -> dict:
    serialized = {
        "role": getattr(message, "role", "assistant"),
        "content": getattr(message, "content", "") or "",
    }

    tool_calls = []
    for tool_call in getattr(message, "tool_calls", []) or []:
        tool_calls.append(
            {
                "id": tool_call.id,
                "type": tool_call.type,
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                },
            }
        )

    if tool_calls:
        serialized["tool_calls"] = tool_calls
    return serialized


def _build_kimi_research_kwargs(messages: list[dict[str, str]]) -> dict:
    return config._sanitize_kwargs(
        config.MODEL_PROVIDER,
        {
            "model": config.LLM_MODEL,
            "messages": messages,
            "max_tokens": config.RESEARCH_MAX_TOKENS,
            "temperature": config.RESEARCH_TEMPERATURE,
            "tools": _KIMI_WEB_SEARCH_TOOL,
        },
    )


def _append_kimi_tool_messages(messages: list[dict], tool_calls) -> None:
    for tool_call in tool_calls or []:
        tool_name = tool_call.function.name
        tool_arguments = json.loads(tool_call.function.arguments)
        if tool_name == "$web_search":
            tool_result = tool_arguments
        else:
            tool_result = {"error": f"Unsupported Kimi tool: {tool_name}"}

        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": tool_name,
                "content": json.dumps(tool_result, ensure_ascii=True),
            }
        )


def _request_kimi_research_completion(messages: list[dict[str, str]]):
    working_messages = [dict(message) for message in messages]

    while True:
        kwargs = _build_kimi_research_kwargs(working_messages)
        response = with_retry(lambda: client.chat.completions.create(**kwargs), max_retries=4, base_delay=2.0)
        choice = response.choices[0]
        if choice.finish_reason != "tool_calls":
            return response

        working_messages.append(_serialize_assistant_message(choice.message))
        _append_kimi_tool_messages(working_messages, choice.message.tool_calls)


async def _request_kimi_research_completion_async(messages: list[dict[str, str]]):
    working_messages = [dict(message) for message in messages]

    while True:
        kwargs = _build_kimi_research_kwargs(working_messages)
        response = await with_retry_async(
            lambda: asyncio.to_thread(client.chat.completions.create, **kwargs),
            max_retries=4,
            base_delay=2.0,
        )
        choice = response.choices[0]
        if choice.finish_reason != "tool_calls":
            return response

        working_messages.append(_serialize_assistant_message(choice.message))
        _append_kimi_tool_messages(working_messages, choice.message.tool_calls)


@retry_with_backoff(max_attempts=3, base_delay=1.0, error_type="api")
def _request_research_completion(messages: list[dict[str, str]]):
    """
    Route research completion to native Gemini client (with search grounding)
    or the standard OpenAI-compat client depending on config.

    Native path: MODEL_PROVIDER=gemini_aistudio + TAVILY_FROZEN + GEMINI_SEARCH_GROUNDING
    All other cases: standard OpenAI-compat client (unchanged behaviour)
    """
    use_native = (
        config.MODEL_PROVIDER == "gemini_aistudio"
        and config.TAVILY_FROZEN
        and config.GEMINI_SEARCH_GROUNDING
    )

    if use_native:
        system_content = next((message["content"] for message in messages if message["role"] == "system"), "")
        user_content = next((message["content"] for message in messages if message["role"] == "user"), "")
        return call_gemini_native(
            api_key=config.LLM_API_KEY,
            model=config.LLM_MODEL,
            system_prompt=system_content,
            user_message=user_content,
            max_tokens=config.RESEARCH_MAX_TOKENS,
            temperature=config.RESEARCH_TEMPERATURE,
            search_grounding=True,
        )

    if config.MODEL_PROVIDER == "kimi_direct":
        return _request_kimi_research_completion(messages)

    kwargs = config._sanitize_kwargs(
        config.MODEL_PROVIDER,
        {
            "model": config.LLM_MODEL,
            "messages": messages,
            "max_tokens": config.RESEARCH_MAX_TOKENS,
            "temperature": config.RESEARCH_TEMPERATURE,
        },
    )
    return client.chat.completions.create(**kwargs)


def research_alumni(
    name: str,
    batch: str = "",
    graduation_year: str = "",
    last_known_role: str = "",
    location: str = "",
    profile_url: str = "",
) -> dict:
    """
    Send one alumni entry to the LLM research agent.
    Returns a parsed profile dict with a `raw_profile` key on success.
    Returns None on total failure or unusable parser output.
    """
    effective_graduation_year = graduation_year or batch
    enrichment_text, _, confirmed_linkedin_url, enrichment_metadata = enrich_person(
        name,
        company=last_known_role,
        graduation_year=effective_graduation_year,
        linkedin_url=profile_url,
    )
    tavily_raw = enrichment_text
    tavily_metadata = json.dumps(enrichment_metadata, ensure_ascii=True)
    # When Tavily is frozen, enrichment_text is always "" — that is expected and correct.
    # Only apply the low-presence guard when Tavily is actually active.
    if not config.TAVILY_FROZEN:
        if not enrichment_text or enrichment_metadata.get("chunk_count", 0) < 2:
            log_event(
                phase="2A",
                alumni_name=name,
                api_called="Tavily Enrichment",
                error_type="LOW_PRESENCE_FALLBACK",
                raw_response_snippet=(
                    f"search_depth={enrichment_metadata.get('search_depth', '')}; "
                    f"chunk_count={enrichment_metadata.get('chunk_count', 0)}"
                ),
            )
            print(f"  [Research] Low web presence for {name}; skipping LLM research.")
            return None

    user_msg = _build_user_message(
        name,
        batch,
        last_known_role,
        location,
        profile_url,
        confirmed_linkedin_url,
        enrichment_text,
    )

    try:
        response = _request_research_completion(
            [
                {"role": "system", "content": f"{_SYSTEM_INSTRUCTION_PREFIX}\n\n{_load_skill_prompt()}"},
                {"role": "user", "content": user_msg},
            ]
        )

        raw = response.choices[0].message.content.strip()
        usage = getattr(response, "usage", None)
        tokens_used = getattr(usage, "total_tokens", None)
        if tokens_used is None and isinstance(usage, dict):
            tokens_used = usage.get("total_tokens")
        log_raw_llm_response(
            phase="2A",
            alumni_name=name,
            api_called=f"{config.MODEL_PROVIDER.upper()} Research ({'native_search' if config.TAVILY_FROZEN else 'tavily'})",
            tokens_used=tokens_used,
            raw_response=raw,
        )
        if "<PROFILE>" not in raw or "</PROFILE>" not in raw:
            log_event(
                phase="2A",
                alumni_name=name,
                api_called="Gemini Research",
                error_type=config.STATUS_FAILED_PARSE,
                raw_response_snippet=raw,
            )
            raise ProfileFenceError(raw)

        profile = _parse_profile(raw, name=name)
        if not profile:
            log_event(
                phase="2A",
                alumni_name=name,
                api_called="Gemini Research",
                error_type=config.STATUS_FAILED_PARSE,
                raw_response_snippet=raw,
            )
            raise ProfileFenceError(raw)

        # --- Batch year validation ---
        if not _validate_batch_match(profile.get("batch_verified", ""), batch):
            print(f"  [Research] BATCH_YEAR_MISMATCH for {name}: profile={profile.get('batch_verified')}, input={batch}")
            profile["confidence"] = "Unconfirmed"
            profile["confidence_level"] = "unconfirmed"
            existing_flags = profile.get("flags", "None")
            profile["flags"] = f"{existing_flags} | BATCH_YEAR_MISMATCH" if existing_flags != "None" else "BATCH_YEAR_MISMATCH"
            profile["company"] = ""
            profile["current_role"] = ""
            profile["location"] = ""

        # --- Education institution check ---
        if not _check_education_match(enrichment_text):
            print(f"  [Research] EDUCATION_MISMATCH for {name}: IIM Udaipur not found in enrichment")
            existing_flags = profile.get("flags", "None")
            profile["flags"] = f"{existing_flags} | EDUCATION_MISMATCH" if existing_flags != "None" else "EDUCATION_MISMATCH"
            profile["confidence_level"] = _downgrade_confidence_level(profile["confidence_level"])

        profile["raw_profile"] = raw
        profile["tavily_raw"] = tavily_raw
        profile["tavily_metadata"] = tavily_metadata
        return profile

    except (ProfileFenceError, AllTavilyKeysExhaustedError):
        raise

    except Exception as e:
        status_code, snippet = describe_exception(e, limit=500)
        log_event(
            phase="2A",
            alumni_name=name,
            api_called="Gemini Research",
            http_status=status_code,
            error_type=type(e).__name__,
            raw_response_snippet=snippet,
        )
        print(f"  [Research] API error for {name}: {e}")
        return None


def parse_profile_response(raw: str, name: str = "") -> dict | None:
    """Parse a stored PROFILE response when resuming 2B from RESEARCH_DONE."""
    if not raw or "<PROFILE>" not in raw or "</PROFILE>" not in raw:
        return None

    profile = _parse_profile(raw, name=name)
    if profile:
        profile["raw_profile"] = raw
    return profile


# ---------------------------------------------------------------------------
# Async variants (used by async orchestrator)
# ---------------------------------------------------------------------------


@async_retry_with_backoff(max_attempts=3, base_delay=1.0, error_type="api")
async def _request_research_completion_async(messages: list[dict[str, str]]):
    """Async version of _request_research_completion. Same routing logic."""
    use_native = (
        config.MODEL_PROVIDER == "gemini_aistudio"
        and config.TAVILY_FROZEN
        and config.GEMINI_SEARCH_GROUNDING
    )

    if use_native:
        system_content = next((message["content"] for message in messages if message["role"] == "system"), "")
        user_content = next((message["content"] for message in messages if message["role"] == "user"), "")
        return await call_gemini_native_async(
            api_key=config.LLM_API_KEY,
            model=config.LLM_MODEL,
            system_prompt=system_content,
            user_message=user_content,
            max_tokens=config.RESEARCH_MAX_TOKENS,
            temperature=config.RESEARCH_TEMPERATURE,
            search_grounding=True,
        )

    if config.MODEL_PROVIDER == "kimi_direct":
        return await _request_kimi_research_completion_async(messages)

    kwargs = config._sanitize_kwargs(
        config.MODEL_PROVIDER,
        {
            "model": config.LLM_MODEL,
            "messages": messages,
            "max_tokens": config.RESEARCH_MAX_TOKENS,
            "temperature": config.RESEARCH_TEMPERATURE,
        },
    )
    return await asyncio.to_thread(client.chat.completions.create, **kwargs)


async def research_alumni_async(
    name: str,
    batch: str = "",
    graduation_year: str = "",
    last_known_role: str = "",
    location: str = "",
    profile_url: str = "",
    *,
    gemini_sem=None,
    research_sem=None,
    tavily_sem=None,
) -> dict:
    """Async version of ``research_alumni``."""
    effective_graduation_year = graduation_year or batch
    enrichment_text, _, confirmed_linkedin_url, enrichment_metadata = await enrich_person_async(
        name,
        company=last_known_role,
        graduation_year=effective_graduation_year,
        linkedin_url=profile_url,
        tavily_sem=tavily_sem,
    )
    tavily_raw = enrichment_text
    tavily_metadata = json.dumps(enrichment_metadata, ensure_ascii=True)
    # When Tavily is frozen, enrichment_text is always "" — that is expected and correct.
    # Only apply the low-presence guard when Tavily is actually active.
    if not config.TAVILY_FROZEN:
        if not enrichment_text or enrichment_metadata.get("chunk_count", 0) < 2:
            log_event(
                phase="2A",
                alumni_name=name,
                api_called="Tavily Enrichment",
                error_type="LOW_PRESENCE_FALLBACK",
                raw_response_snippet=(
                    f"search_depth={enrichment_metadata.get('search_depth', '')}; "
                    f"chunk_count={enrichment_metadata.get('chunk_count', 0)}"
                ),
            )
            print(f"  [Research] Low web presence for {name}; skipping LLM research.")
            return None

    user_msg = _build_user_message(
        name,
        batch,
        last_known_role,
        location,
        profile_url,
        confirmed_linkedin_url,
        enrichment_text,
    )

    try:
        research_gate = research_sem or gemini_sem
        if research_gate:
            async with research_gate:
                response = await _request_research_completion_async(
                    [
                        {"role": "system", "content": f"{_SYSTEM_INSTRUCTION_PREFIX}\n\n{_load_skill_prompt()}"},
                        {"role": "user", "content": user_msg},
                    ]
                )
        else:
            response = await _request_research_completion_async(
                [
                    {"role": "system", "content": f"{_SYSTEM_INSTRUCTION_PREFIX}\n\n{_load_skill_prompt()}"},
                    {"role": "user", "content": user_msg},
                ]
            )

        raw = response.choices[0].message.content.strip()
        usage = getattr(response, "usage", None)
        tokens_used = getattr(usage, "total_tokens", None)
        if tokens_used is None and isinstance(usage, dict):
            tokens_used = usage.get("total_tokens")
        log_raw_llm_response(
            phase="2A",
            alumni_name=name,
            api_called=f"{config.MODEL_PROVIDER.upper()} Research ({'native_search' if config.TAVILY_FROZEN else 'tavily'})",
            tokens_used=tokens_used,
            raw_response=raw,
        )
        if "<PROFILE>" not in raw or "</PROFILE>" not in raw:
            log_event(
                phase="2A",
                alumni_name=name,
                api_called="Gemini Research",
                error_type=config.STATUS_FAILED_PARSE,
                raw_response_snippet=raw,
            )
            raise ProfileFenceError(raw)

        profile = _parse_profile(raw, name=name)
        if not profile:
            log_event(
                phase="2A",
                alumni_name=name,
                api_called="Gemini Research",
                error_type=config.STATUS_FAILED_PARSE,
                raw_response_snippet=raw,
            )
            raise ProfileFenceError(raw)

        # --- Batch year validation ---
        if not _validate_batch_match(profile.get("batch_verified", ""), batch):
            print(f"  [Research] BATCH_YEAR_MISMATCH for {name}: profile={profile.get('batch_verified')}, input={batch}")
            profile["confidence"] = "Unconfirmed"
            profile["confidence_level"] = "unconfirmed"
            existing_flags = profile.get("flags", "None")
            profile["flags"] = f"{existing_flags} | BATCH_YEAR_MISMATCH" if existing_flags != "None" else "BATCH_YEAR_MISMATCH"
            profile["company"] = ""
            profile["current_role"] = ""
            profile["location"] = ""

        # --- Education institution check ---
        if not _check_education_match(enrichment_text):
            print(f"  [Research] EDUCATION_MISMATCH for {name}: IIM Udaipur not found in enrichment")
            existing_flags = profile.get("flags", "None")
            profile["flags"] = f"{existing_flags} | EDUCATION_MISMATCH" if existing_flags != "None" else "EDUCATION_MISMATCH"
            profile["confidence_level"] = _downgrade_confidence_level(profile["confidence_level"])

        profile["raw_profile"] = raw
        profile["tavily_raw"] = tavily_raw
        profile["tavily_metadata"] = tavily_metadata
        return profile

    except (ProfileFenceError, AllTavilyKeysExhaustedError):
        raise

    except Exception as e:
        status_code, snippet = describe_exception(e, limit=500)
        log_event(
            phase="2A",
            alumni_name=name,
            api_called="Gemini Research",
            http_status=status_code,
            error_type=type(e).__name__,
            raw_response_snippet=snippet,
        )
        print(f"  [Research] API error for {name}: {e}")
        return None


def _build_user_message(
    name: str,
    batch: str,
    role: str,
    location: str,
    profile_url: str,
    confirmed_linkedin_url: str,
    enrichment_text: str,
) -> str:
    """
    Build the user message for the research LLM.

    When TAVILY_FROZEN=True (direct LLM native search mode):
        Returns only "{name} {batch_year} passout" — the model does its own search.

    When TAVILY_FROZEN=False (legacy Tavily enrichment mode):
        Returns the full structured prompt with web-search context injected.
    """
    if config.TAVILY_FROZEN:
        # Minimal prompt — LLM uses its native web search from here.
        # Format: "{name} {graduation_year} passout"
        year = (batch or "").strip()
        return f"{name} {year} passout".strip()

    # --- Legacy Tavily path (unchanged) ---
    lines = [
        "You are researching an IIM Udaipur alumni for a personalised outreach email.",
        "If a confirmed LinkedIn URL is provided, treat it as the primary identity anchor.",
        "",
        f"Name: {name}",
        f"Batch: {batch}" if batch else "Batch: Unknown",
        f"Last known role (from AlmaConnect): {role}" if role else "Last known role: Unknown",
        f"Last known location: {location}" if location else "Last known location: Unknown",
        f"AlmaConnect profile: {profile_url}" if profile_url else "AlmaConnect profile: Not available",
        (
            f"Confirmed LinkedIn URL from Tavily: {confirmed_linkedin_url}"
            if confirmed_linkedin_url
            else "Confirmed LinkedIn URL from Tavily: Not available"
        ),
        "",
        "Institution: IIM Udaipur",
        "",
        "--- WEB SEARCH CONTEXT ---",
        enrichment_text if enrichment_text else "(No web search context available)",
        "",
        "Follow every step in your instructions, but return only the final PROFILE block.",
        "Do not include reasoning or any text before the block.",
        "",
        _PROFILE_OUTPUT_FORMAT,
    ]
    return "\n".join(lines)


def _validate_batch_match(profile_batch: str, input_batch: str) -> bool:
    """Return False if the profile batch differs from the input batch by > 3 years."""
    profile_years = re.findall(r"\d{4}", profile_batch or "")
    input_years = re.findall(r"\d{4}", input_batch or "")
    if not profile_years or not input_years:
        return True  # can't compare — assume OK
    return abs(int(profile_years[-1]) - int(input_years[-1])) <= 3


_MIN_ENRICHMENT_LEN_FOR_EDUCATION_CHECK = 200


def _check_education_match(enrichment_text: str) -> bool:
    """Return True if enrichment text mentions IIM Udaipur."""
    if not enrichment_text or len(enrichment_text) < _MIN_ENRICHMENT_LEN_FOR_EDUCATION_CHECK:
        return True  # no data or too thin to contradict
    text_lower = enrichment_text.lower()
    return (
        "iim udaipur" in text_lower
        or "indian institute of management udaipur" in text_lower
        or "iim-u" in text_lower
        or "iimu" in text_lower
    )


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
        block[key.strip().upper()] = value.strip()
    return block


def _parse_profile(raw: str, name: str = "") -> dict | None:
    """Parse the final <PROFILE> block into a normalized profile dict."""
    block = _extract_profile_block(raw)
    if block:
        p = _parse_profile_block(block)
    else:
        print("[Parser] No <PROFILE> block found — treating as unusable")
        return None

    if p["company"] and _classify_confidence(p["confidence"]) in {"low", "unconfirmed"}:
        p["company"] = ""

    confidence_level = _classify_confidence(p["confidence"])
    flags_upper = p["flags"].upper()
    if any(marker in flags_upper for marker in _VERIFY_COMPANY_FLAG_MARKERS):
        p["company"] = ""
        if confidence_level in {"very_high", "high"}:
            confidence_level = "medium"

    if any(marker in flags_upper for marker in _FORCE_UNCONFIRMED_FLAG_MARKERS):
        confidence_level = "unconfirmed"
    elif any(marker in flags_upper for marker in _DOWNGRADE_FLAG_MARKERS):
        confidence_level = _downgrade_confidence_level(confidence_level)
    elif any(marker in flags_upper for marker in _UNCONFIRMED_FLAG_MARKERS):
        confidence_level = _downgrade_confidence_level(confidence_level)

    # Ensure FLAGS is never empty
    if not p["flags"]:
        p["flags"] = "None"

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
        return safe
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
