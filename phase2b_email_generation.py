"""Phase 2B: provider-agnostic email generation from verified research profiles."""

import asyncio
import functools
import re
import traceback
from pathlib import Path

import config
from config import client
from phase2a_alumni_research import get_safe_hooks
from utils.retry import async_retry_with_backoff, describe_exception, retry_with_backoff
from utils.run_context import log_event, log_raw_llm_response

_EMPTY_MARKERS = {"", "blank", "none", "n/a", "-", "unknown"}

_PLACEHOLDER_RE = re.compile(
    r"\["
    r"(?:Your Name|Full Name|Sender Name|Company|Batch|"
    r"Year|Position|Role|Title|City|School|Institute|"
    r"[A-Z][A-Z_ ]{2,})"
    r"\]",
    re.IGNORECASE,
)

_STUDENT_LANGUAGE_PATTERNS = [
    r"currently pursuing",
    r"current student",
    r"I am a student at",
    r"I'm a student at",
    r"currently enrolled",
    r"pursuing my PGP",
    r"pursuing my MBA",
]
_STUDENT_LANGUAGE_RE = re.compile("|".join(_STUDENT_LANGUAGE_PATTERNS), re.IGNORECASE)
_EMAIL_OUTPUT_FORMAT = """
Output your response in this exact format:

<EMAIL>
SUBJECT: [subject line here]
BODY: [full email body here]
</EMAIL>

Do not add any text outside the <EMAIL> tags.
""".strip()

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_PERSONALIZED_PROMPT_PATH = _PROMPTS_DIR / "email_personalized.txt"
_BASE_PROMPT_PATH = _PROMPTS_DIR / "email_base_template.txt"
_VALID_CONFIDENCE_LEVELS = {"very_high", "high", "medium", "low", "unconfirmed"}
_EMAIL_REPAIR_SYSTEM_PROMPT = (
    "Rewrite the provided draft into exactly one <EMAIL>...</EMAIL> block. "
    "Preserve the meaning, but output only:\n"
    "<EMAIL>\nSUBJECT: ...\nBODY: ...\n</EMAIL>"
)


def _normalize_confidence_level(conf_level: str | None) -> str:
    normalized = (conf_level or "").strip().lower()
    normalized = normalized.replace("-", " ").replace("_", " ")
    normalized = " ".join(normalized.split())
    if normalized == "very high":
        return "very_high"
    if normalized in {"high", "medium", "low", "unconfirmed"}:
        return normalized
    return "unconfirmed"


@functools.lru_cache(maxsize=1)
def _load_personalized_prompt() -> str:
    try:
        return _PERSONALIZED_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Prompt file not found: {_PERSONALIZED_PROMPT_PATH}. "
            f"Ensure the prompts/ directory contains email_personalized.txt."
        )


@functools.lru_cache(maxsize=1)
def _load_base_prompt() -> str:
    try:
        return _BASE_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Prompt file not found: {_BASE_PROMPT_PATH}. "
            f"Ensure the prompts/ directory contains email_base_template.txt."
        )


class EmailFenceError(ValueError):
    """Raised when the model response is missing the required EMAIL fence."""

    def __init__(self, raw_response: str):
        super().__init__("Missing required <EMAIL> fence in email response.")
        self.raw_response = raw_response


def _resolve_email_temperature() -> float:
    if config.MODEL_PROVIDER == "kimi_direct":
        return 1.0
    return config.EMAIL_TEMPERATURE


@retry_with_backoff(max_attempts=3, base_delay=1.0, error_type="api")
def _request_email_completion(messages: list[dict[str, str]]):
    kwargs = config._sanitize_kwargs(
        config.MODEL_PROVIDER,
        {
            "model": config.LLM_MODEL,
            "messages": messages,
            "temperature": _resolve_email_temperature(),
            "max_tokens": config.EMAIL_MAX_TOKENS,
        },
    )
    return client.chat.completions.create(**kwargs)


def _repair_email_format(text: str) -> tuple[str | None, str | None]:
    if not text.strip():
        return None, None

    response = _request_email_completion(
        [
            {"role": "system", "content": _EMAIL_REPAIR_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
    )
    repaired_text = response.choices[0].message.content.strip()
    return parse_email_response(repaired_text)

def _clean_company_value(value: str) -> str:
    text = (value or "").strip()
    if text.lower() in _EMPTY_MARKERS:
        return ""
    return text


def _validate_email_client_config() -> None:
    if not config.LLM_API_KEY:
        raise ValueError(f"{config.LLM_API_KEY_ENV_VAR} is missing from the environment.")
    if not config.LLM_MODEL:
        raise ValueError("LLM model is not configured.")
    if not config.LLM_BASE_URL:
        raise ValueError("LLM base URL is not configured.")


def _has_placeholders(text: str) -> bool:
    """Return True if the text contains bracket-style LLM placeholder patterns."""
    return bool(_PLACEHOLDER_RE.search(text))


def _has_student_language(text: str) -> bool:
    """Return True if the text contains 'current student' / 'currently pursuing' variants."""
    return bool(_STUDENT_LANGUAGE_RE.search(text))


def _log_llm_exception(prefix: str, error: Exception) -> None:
    print(f"{prefix}: {error!r}")

    status_code = getattr(error, "status_code", None)
    if status_code is not None:
        print(f"[AI Gen] Status code: {status_code}")

    response = getattr(error, "response", None)
    if response is not None:
        response_status = getattr(response, "status_code", None)
        if response_status is not None and response_status != status_code:
            print(f"[AI Gen] Response status code: {response_status}")

        body_logged = False
        try:
            response_text = response.text
        except Exception:
            response_text = None
        if response_text:
            print("[AI Gen] Response body (truncated):")
            print(response_text[:500])
            body_logged = True

        if not body_logged:
            try:
                response_json = response.json()
            except Exception:
                response_json = None
            if response_json is not None:
                print("[AI Gen] Response JSON (truncated):")
                print(str(response_json)[:500])
                body_logged = True

    error_body = getattr(error, "body", None)
    if error_body:
        print("[AI Gen] Error body (truncated):")
        print(str(error_body)[:500])

    print(traceback.format_exc())


def generate_email_from_profile(name: str, profile: dict, *, enrichment_source: str = "llm_research") -> tuple:
    """
    Generate a personalized Subject + Body using a verified research profile.
    Returns (subject, body) tuple.
    """
    batch = profile.get("batch_verified", "")
    conf_level = _normalize_confidence_level(profile.get("confidence_level", "unknown"))
    company = _clean_company_value(profile.get("company", ""))
    if conf_level in ("very_high", "high"):
        if not company:
            print(f"[AI Gen] Missing verified company for {name}; using base template.")
            return generate_email_base_template(name, batch)
    elif conf_level == "medium":
        pass
    else:
        print(f"[AI Gen] Confidence too low ({conf_level}) for {name}; using base template")
        return generate_email_base_template(name, batch)

    try:
        _validate_email_client_config()
        user_prompt = _build_prompt_from_profile(name, profile)

        print(
            f"[AI Gen] Calling provider={config.MODEL_PROVIDER} "
            f"model={config.LLM_MODEL} base_url={config.LLM_BASE_URL}"
        )
        response = _request_email_completion(
            [
                {"role": "system", "content": _load_personalized_prompt()},
                {"role": "user", "content": user_prompt},
            ]
        )

        text = response.choices[0].message.content.strip()
        usage = getattr(response, "usage", None)
        tokens_used = getattr(usage, "total_tokens", None)
        if tokens_used is None and isinstance(usage, dict):
            tokens_used = usage.get("total_tokens")
        log_raw_llm_response(
            phase="2B",
            alumni_name=name,
            api_called="Gemini Email",
            tokens_used=tokens_used,
            raw_response=text,
        )
        subject, body = parse_email_response(text)
        if subject is None or body is None:
            subject, body = _repair_email_format(text)
        if subject is None or body is None:
            print(f"[AI Gen] parse_email_response returned None for {name}")
            if enrichment_source == "llm_research" and conf_level == "unconfirmed":
                return generate_email_base_template(name, batch)
            log_event(
                phase="2B",
                alumni_name=name,
                api_called="Gemini Email",
                error_type=config.STATUS_FAILED_PARSE,
                raw_response_snippet=text,
            )
            raise EmailFenceError(text)
        combined = f"{subject}\n{body}"
        if _has_placeholders(combined):
            print(f"[AI Gen] Placeholder detected in email for {name}: {body[:200]}")
            log_event(
                phase="2B",
                alumni_name=name,
                api_called="Gemini Email",
                error_type="PLACEHOLDER_DETECTED",
                raw_response_snippet=combined[:300],
            )
            return None, None
        if _has_student_language(combined):
            print(f"[AI Gen] Student language detected in email for {name}: {body[:200]}")
            log_event(
                phase="2B",
                alumni_name=name,
                api_called="Gemini Email",
                error_type="STUDENT_LANGUAGE_DETECTED",
                raw_response_snippet=combined[:300],
            )
            return None, None
        return subject, body

    except EmailFenceError:
        raise

    except Exception as e:
        status_code, snippet = describe_exception(e, limit=500)
        log_event(
            phase="2B",
            alumni_name=name,
            api_called="Gemini Email",
            http_status=status_code,
            error_type=type(e).__name__,
            raw_response_snippet=snippet,
        )
        _log_llm_exception(f"[Email Gen Error] {name}", e)
        return None, None


def generate_email_base_template(name: str, batch: str = "") -> tuple:
    """
    Generate a generic alumni connection email — used when research yields
    Low/UNCONFIRMED confidence or when no research was possible.
    """
    user_prompt = f"""Recipient: {name}
Batch: {batch if batch else 'Fellow IIM Udaipur alumnus'}
Confidence: Low — no verified career information available.

Write a warm, genuine alumni connection email. Do NOT reference any career details,
company names, or specific achievements. Focus ONLY on the shared IIM Udaipur connection.

{_EMAIL_OUTPUT_FORMAT}
"""

    try:
        _validate_email_client_config()
        response = _request_email_completion(
            [
                {"role": "system", "content": _load_base_prompt()},
                {"role": "user", "content": user_prompt},
            ]
        )

        text = response.choices[0].message.content.strip()
        usage = getattr(response, "usage", None)
        tokens_used = getattr(usage, "total_tokens", None)
        if tokens_used is None and isinstance(usage, dict):
            tokens_used = usage.get("total_tokens")
        log_raw_llm_response(
            phase="2B",
            alumni_name=name,
            api_called="Gemini Base Template",
            tokens_used=tokens_used,
            raw_response=text,
        )
        subject, body = parse_email_response(text)
        if subject is None or body is None:
            subject, body = _repair_email_format(text)
        if subject is None or body is None:
            print(f"[AI Gen] parse_email_response returned None for base template ({name})")
            log_event(
                phase="2B",
                alumni_name=name,
                api_called="Gemini Base Template",
                error_type=config.STATUS_FAILED_PARSE,
                raw_response_snippet=text,
            )
            raise EmailFenceError(text)
        combined = f"{subject}\n{body}"
        if _has_placeholders(combined):
            print(f"[AI Gen] Placeholder detected in base template for {name}: {body[:200]}")
            log_event(
                phase="2B",
                alumni_name=name,
                api_called="Gemini Base Template",
                error_type="PLACEHOLDER_DETECTED",
                raw_response_snippet=combined[:300],
            )
            return None, None
        if _has_student_language(combined):
            print(f"[AI Gen] Student language detected in base template for {name}: {body[:200]}")
            log_event(
                phase="2B",
                alumni_name=name,
                api_called="Gemini Base Template",
                error_type="STUDENT_LANGUAGE_DETECTED",
                raw_response_snippet=combined[:300],
            )
            return None, None
        return subject, body

    except EmailFenceError:
        raise

    except Exception as e:
        status_code, snippet = describe_exception(e, limit=500)
        log_event(
            phase="2B",
            alumni_name=name,
            api_called="Gemini Base Template",
            http_status=status_code,
            error_type=type(e).__name__,
            raw_response_snippet=snippet,
        )
        _log_llm_exception(f"[Email Gen Error] base template for {name}", e)
        return None, None


def _build_prompt_from_profile(name: str, profile: dict) -> str:
    """Build the email generation prompt from a parsed research profile."""
    confidence = profile.get("confidence", "Unknown")
    conf_level = _normalize_confidence_level(profile.get("confidence_level", "unknown"))
    current_role = profile.get("current_role", "")
    company = _clean_company_value(profile.get("company", ""))
    location = profile.get("location", "")
    domain = profile.get("domain", "")
    batch = profile.get("batch_verified", "")
    flags = profile.get("flags", "")
    outreach_note = profile.get("outreach_note", "")

    if conf_level in ("very_high", "high") and not company:
        raise ValueError("Verified company is required for profile-based email generation.")

    safe_hooks = get_safe_hooks(profile)
    hooks_text = "\n".join(f"  - {hook}" for hook in safe_hooks) if safe_hooks else "  (none available)"

    lines = [
        "--- RESEARCH PROFILE (verified) ---",
        f"Recipient: {name}",
        f"Confidence: {confidence}",
        f"Batch: {batch}" if batch else "",
        f"Current Role: {current_role}" if current_role and conf_level in ("very_high", "high") else "",
        f"Company: {company}" if conf_level in ("very_high", "high") else "",
        f"Location: {location}" if location else "",
        f"Domain: {domain}" if domain and conf_level in ("very_high", "high") else "",
        "",
        "FLAGS (do NOT reference flagged facts in the email):",
        f"  {flags}" if flags else "  None",
        "",
        "SAFE EMAIL HOOKS (use these):",
        hooks_text,
        "",
        f"OUTREACH NOTE: {outreach_note}" if outreach_note else "",
        "",
        "--- INSTRUCTIONS ---",
    ]

    if conf_level in ("very_high", "high"):
        lines.append("Write a personalized email using the career details above.")
        lines.append("Reference specific facts from the profile (role, company, domain).")
    elif conf_level == "medium":
        lines.append("Write a semi-personalized email. You may reference the domain/location but")
        lines.append("do NOT reference specific career details since they are not fully verified.")
        lines.append("Lead with the alumni connection.")
    else:
        lines.append("Write a generic alumni connection email.")
        lines.append("Do NOT reference any career details — they are not verified.")

    lines.extend(["", _EMAIL_OUTPUT_FORMAT])
    return "\n".join(line for line in lines if line != "")


def parse_email_response(text: str) -> tuple:
    """Parse SUBJECT and BODY from the final <EMAIL> block or a clean plain-text fallback."""
    matches = re.findall(r"<EMAIL>(.*?)</EMAIL>", text, re.DOTALL)
    block = matches[-1] if matches else text
    subject_match = re.search(r"^\s*SUBJECT:[ \t]*(.*)$", block, re.IGNORECASE | re.MULTILINE)
    body_match = re.search(r"^\s*BODY:[ \t]*(.+)$", block, re.IGNORECASE | re.MULTILINE | re.DOTALL)
    if not subject_match:
        return None, None

    subject = subject_match.group(1).strip().strip('"').strip("'").strip("*")
    if body_match:
        body = body_match.group(1).strip().strip('"').strip("'")
    else:
        # Some model responses omit the BODY label but still place the email body
        # directly after the SUBJECT line inside the EMAIL fence.
        lines = block.splitlines()
        subject_line_index = None
        for index, line in enumerate(lines):
            if re.match(r"^\s*SUBJECT:", line, re.IGNORECASE):
                subject_line_index = index
                break

        if subject_line_index is None:
            return None, None

        remaining_lines = lines[subject_line_index + 1 :]
        body = "\n".join(remaining_lines).strip().strip('"').strip("'")

    if not subject or not body:
        return None, None

    return subject, body


# ---------------------------------------------------------------------------
# Async variants (used by async orchestrator)
# ---------------------------------------------------------------------------


@async_retry_with_backoff(max_attempts=3, base_delay=1.0, error_type="api")
async def _request_email_completion_async(messages: list[dict[str, str]]):
    kwargs = config._sanitize_kwargs(
        config.MODEL_PROVIDER,
        {
            "model": config.LLM_MODEL,
            "messages": messages,
            "temperature": _resolve_email_temperature(),
            "max_tokens": config.EMAIL_MAX_TOKENS,
        },
    )
    return await asyncio.to_thread(client.chat.completions.create, **kwargs)


async def _repair_email_format_async(text: str) -> tuple[str | None, str | None]:
    if not text.strip():
        return None, None

    response = await _request_email_completion_async(
        [
            {"role": "system", "content": _EMAIL_REPAIR_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
    )
    repaired_text = response.choices[0].message.content.strip()
    return parse_email_response(repaired_text)


async def generate_email_from_profile_async(
    name: str,
    profile: dict,
    *,
    enrichment_source: str = "llm_research",
    gemini_sem=None,
) -> tuple:
    """Async version of ``generate_email_from_profile``."""
    batch = profile.get("batch_verified", "")
    conf_level = _normalize_confidence_level(profile.get("confidence_level", "unknown"))
    company = _clean_company_value(profile.get("company", ""))
    if conf_level in ("very_high", "high"):
        if not company:
            print(f"[AI Gen] Missing verified company for {name}; using base template.")
            return await generate_email_base_template_async(name, batch, gemini_sem=gemini_sem)
    elif conf_level == "medium":
        pass
    else:
        print(f"[AI Gen] Confidence too low ({conf_level}) for {name}; using base template")
        return await generate_email_base_template_async(name, batch, gemini_sem=gemini_sem)

    try:
        _validate_email_client_config()
        user_prompt = _build_prompt_from_profile(name, profile)

        if gemini_sem:
            async with gemini_sem:
                response = await _request_email_completion_async(
                    [
                        {"role": "system", "content": _load_personalized_prompt()},
                        {"role": "user", "content": user_prompt},
                    ]
                )
        else:
            response = await _request_email_completion_async(
                [
                    {"role": "system", "content": _load_personalized_prompt()},
                    {"role": "user", "content": user_prompt},
                ]
            )

        text = response.choices[0].message.content.strip()
        usage = getattr(response, "usage", None)
        tokens_used = getattr(usage, "total_tokens", None)
        if tokens_used is None and isinstance(usage, dict):
            tokens_used = usage.get("total_tokens")
        log_raw_llm_response(
            phase="2B",
            alumni_name=name,
            api_called="Gemini Email",
            tokens_used=tokens_used,
            raw_response=text,
        )
        subject, body = parse_email_response(text)
        if subject is None or body is None:
            subject, body = await _repair_email_format_async(text)
        if subject is None or body is None:
            if enrichment_source == "llm_research" and conf_level == "unconfirmed":
                return await generate_email_base_template_async(name, batch, gemini_sem=gemini_sem)
            log_event(
                phase="2B",
                alumni_name=name,
                api_called="Gemini Email",
                error_type=config.STATUS_FAILED_PARSE,
                raw_response_snippet=text,
            )
            raise EmailFenceError(text)
        combined = f"{subject}\n{body}"
        if _has_placeholders(combined):
            print(f"[AI Gen] Placeholder detected in email for {name}: {body[:200]}")
            log_event(
                phase="2B",
                alumni_name=name,
                api_called="Gemini Email",
                error_type="PLACEHOLDER_DETECTED",
                raw_response_snippet=combined[:300],
            )
            return None, None
        if _has_student_language(combined):
            print(f"[AI Gen] Student language detected in email for {name}: {body[:200]}")
            log_event(
                phase="2B",
                alumni_name=name,
                api_called="Gemini Email",
                error_type="STUDENT_LANGUAGE_DETECTED",
                raw_response_snippet=combined[:300],
            )
            return None, None
        return subject, body

    except EmailFenceError:
        raise

    except Exception as e:
        status_code, snippet = describe_exception(e, limit=500)
        log_event(
            phase="2B",
            alumni_name=name,
            api_called="Gemini Email",
            http_status=status_code,
            error_type=type(e).__name__,
            raw_response_snippet=snippet,
        )
        _log_llm_exception(f"[Email Gen Error] {name}", e)
        return None, None


async def generate_email_base_template_async(name: str, batch: str = "", *, gemini_sem=None) -> tuple:
    """Async version of ``generate_email_base_template``."""
    user_prompt = f"""Recipient: {name}
Batch: {batch if batch else 'Fellow IIM Udaipur alumnus'}
Confidence: Low — no verified career information available.

Write a warm, genuine alumni connection email. Do NOT reference any career details,
company names, or specific achievements. Focus ONLY on the shared IIM Udaipur connection.

{_EMAIL_OUTPUT_FORMAT}
"""

    try:
        _validate_email_client_config()
        if gemini_sem:
            async with gemini_sem:
                response = await _request_email_completion_async(
                    [
                        {"role": "system", "content": _load_base_prompt()},
                        {"role": "user", "content": user_prompt},
                    ]
                )
        else:
            response = await _request_email_completion_async(
                [
                    {"role": "system", "content": _load_base_prompt()},
                    {"role": "user", "content": user_prompt},
                ]
            )

        text = response.choices[0].message.content.strip()
        usage = getattr(response, "usage", None)
        tokens_used = getattr(usage, "total_tokens", None)
        if tokens_used is None and isinstance(usage, dict):
            tokens_used = usage.get("total_tokens")
        log_raw_llm_response(
            phase="2B",
            alumni_name=name,
            api_called="Gemini Base Template",
            tokens_used=tokens_used,
            raw_response=text,
        )
        subject, body = parse_email_response(text)
        if subject is None or body is None:
            subject, body = await _repair_email_format_async(text)
        if subject is None or body is None:
            log_event(
                phase="2B",
                alumni_name=name,
                api_called="Gemini Base Template",
                error_type=config.STATUS_FAILED_PARSE,
                raw_response_snippet=text,
            )
            raise EmailFenceError(text)
        combined = f"{subject}\n{body}"
        if _has_placeholders(combined):
            print(f"[AI Gen] Placeholder detected in base template for {name}: {body[:200]}")
            log_event(
                phase="2B",
                alumni_name=name,
                api_called="Gemini Base Template",
                error_type="PLACEHOLDER_DETECTED",
                raw_response_snippet=combined[:300],
            )
            return None, None
        if _has_student_language(combined):
            print(f"[AI Gen] Student language detected in base template for {name}: {body[:200]}")
            log_event(
                phase="2B",
                alumni_name=name,
                api_called="Gemini Base Template",
                error_type="STUDENT_LANGUAGE_DETECTED",
                raw_response_snippet=combined[:300],
            )
            return None, None
        return subject, body

    except EmailFenceError:
        raise

    except Exception as e:
        status_code, snippet = describe_exception(e, limit=500)
        log_event(
            phase="2B",
            alumni_name=name,
            api_called="Gemini Base Template",
            http_status=status_code,
            error_type=type(e).__name__,
            raw_response_snippet=snippet,
        )
        _log_llm_exception(f"[Email Gen Error] base template for {name}", e)
        return None, None


if __name__ == "__main__":
    print("Testing email generation...")
    subject, body = generate_email_base_template("Test User", "PGP '23")
    print(f"\nSUBJECT: {subject}")
    print(f"\nBODY:\n{body}")
