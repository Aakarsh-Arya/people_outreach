"""
Phase 2B: AI Email Generation via the Gemini OpenAI-compatible endpoint.
Takes a verified research profile and generates personalized Subject + Body.
Only references facts confirmed by the research agent; never fabricates.
"""

import os
import re
import traceback

from openai import OpenAI

import config
from phase2a_alumni_research import get_safe_hooks

_EMPTY_MARKERS = {"", "blank", "none", "n/a", "-", "unknown"}
_EMAIL_OUTPUT_FORMAT = """
Output your response in this exact format:

<EMAIL>
SUBJECT: [subject line here]
BODY: [full email body here]
</EMAIL>

Do not add any text outside the <EMAIL> tags.
""".strip()

client = OpenAI(
    base_url=config.OPENROUTER_BASE_URL,
    api_key=config.OPENROUTER_API_KEY,
)

SYSTEM_PROMPT = f"""You are writing a personalized cold outreach email on behalf of {config.SENDER_NAME},
{config.SENDER_CONTEXT}

ANTI-HALLUCINATION RULES (these override everything else):
- You will receive a RESEARCH PROFILE with verified facts and a CONFIDENCE level.
- ONLY reference facts explicitly present in the research profile.
- If a fact has a FLAG or VERIFY warning next to it, do NOT mention it in the email.
- If confidence is "Medium" — use only campus/batch connection hooks. Do NOT reference career details.
- If confidence is "Low" or "UNCONFIRMED" — write a generic alumni connection email. No career references at all.
- NEVER invent details about someone's role, company, achievements, or career history.
- NEVER assume someone still works at a company unless the profile explicitly confirms it as current.
- If you are unsure about any fact, omit it. A vague but honest email is better than a specific but wrong one.

STYLE RULES:
- Write in a warm, peer-to-peer tone — as one alumnus to another.
- Keep the email genuine, concise, and professional.
- No bullet points in the email body.
- No ALL CAPS in the subject line.
- No trigger words: Free, Urgent, Winner, Click here, Limited time.
- End the email with a natural, low-pressure sign-off (e.g., "No pressure to reply if you're having a busy week!").
- Do NOT use any HTML formatting. Plain text only.
"""

_EXPECTED_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
_EXPECTED_MODEL = "gemini-2.5-flash"


def _clean_company_value(value: str) -> str:
    text = (value or "").strip()
    if text.lower() in _EMPTY_MARKERS:
        return ""
    return text


def _validate_email_client_config() -> None:
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if config.OPENROUTER_BASE_URL != _EXPECTED_BASE_URL:
        raise ValueError(
            f"phase2b base_url mismatch: expected {_EXPECTED_BASE_URL!r}, got {config.OPENROUTER_BASE_URL!r}"
        )
    if config.OPENROUTER_MODEL != _EXPECTED_MODEL:
        raise ValueError(
            f"phase2b model mismatch: expected {_EXPECTED_MODEL!r}, got {config.OPENROUTER_MODEL!r}"
        )
    if not gemini_api_key:
        raise ValueError("GEMINI_API_KEY is missing from the environment.")
    if config.OPENROUTER_API_KEY != gemini_api_key:
        raise ValueError("phase2b is not using GEMINI_API_KEY from the environment.")


def _log_raw_llm_response(prefix: str, text: str) -> None:
    print(f"{prefix} ({len(text)} chars):")
    print("--- RAW LLM RESPONSE START ---")
    print(text)
    print("--- RAW LLM RESPONSE END ---")


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
            print("[AI Gen] Response body:")
            print(response_text)
            body_logged = True

        if not body_logged:
            try:
                response_json = response.json()
            except Exception:
                response_json = None
            if response_json is not None:
                print("[AI Gen] Response JSON:")
                print(response_json)
                body_logged = True

    error_body = getattr(error, "body", None)
    if error_body:
        print("[AI Gen] Error body:")
        print(error_body)

    print(traceback.format_exc())


def generate_email_from_profile(name: str, profile: dict) -> tuple:
    """
    Generate a personalized Subject + Body using a verified research profile.
    Returns (subject, body) tuple.
    """
    batch = profile.get("batch_verified", "")
    company = _clean_company_value(profile.get("company", ""))
    if not company:
        print(f"[AI Gen] Missing verified company for {name}; using base template.")
        return generate_email_base_template(name, batch)

    try:
        _validate_email_client_config()
        user_prompt = _build_prompt_from_profile(name, profile)

        print(
            f"[AI Gen] Calling model={config.OPENROUTER_MODEL} "
            f"base_url={config.OPENROUTER_BASE_URL} auth=GEMINI_API_KEY"
        )
        response = client.chat.completions.create(
            model=config.OPENROUTER_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
            max_tokens=4000,
        )

        text = response.choices[0].message.content.strip()
        _log_raw_llm_response(f"[AI Gen] Raw response for {name}", text)
        subject, body = parse_email_response(text)
        if subject is None or body is None:
            print(f"[AI Gen] parse_email_response returned None for {name}")
            return None, None
        return subject, body

    except Exception as e:
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
        response = client.chat.completions.create(
            model=config.OPENROUTER_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
            max_tokens=4000,
        )

        text = response.choices[0].message.content.strip()
        _log_raw_llm_response(f"[AI Gen] Raw base-template response for {name}", text)
        subject, body = parse_email_response(text)
        if subject is None or body is None:
            print(f"[AI Gen] parse_email_response returned None for base template ({name})")
            return None, None
        return subject, body

    except Exception as e:
        _log_llm_exception(f"[Email Gen Error] base template for {name}", e)
        return None, None


def _build_prompt_from_profile(name: str, profile: dict) -> str:
    """Build the email generation prompt from a parsed research profile."""
    confidence = profile.get("confidence", "Unknown")
    conf_level = profile.get("confidence_level", "unknown")
    current_role = profile.get("current_role", "")
    company = _clean_company_value(profile.get("company", ""))
    location = profile.get("location", "")
    domain = profile.get("domain", "")
    batch = profile.get("batch_verified", "")
    flags = profile.get("flags", "")
    outreach_note = profile.get("outreach_note", "")

    if not company:
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
    """Parse SUBJECT and BODY from the final <EMAIL> block only."""
    matches = re.findall(r"<EMAIL>(.*?)</EMAIL>", text, re.DOTALL)
    if not matches:
        return None, None

    block = matches[-1]
    subject_match = re.search(r"^\s*SUBJECT:[ \t]*(.*)$", block, re.IGNORECASE | re.MULTILINE)
    body_match = re.search(r"^\s*BODY:[ \t]*(.+)$", block, re.IGNORECASE | re.MULTILINE | re.DOTALL)
    if not subject_match or not body_match:
        return None, None

    subject = subject_match.group(1).strip().strip('"').strip("'").strip("*")
    body = body_match.group(1).strip().strip('"').strip("'")
    if not subject or not body:
        return None, None

    return subject, body


def generate_email(name, role="", company="", enrichment_text="", is_fallback=False):
    """
    Legacy wrapper — used by old orchestrator.
    New code should use generate_email_from_profile() instead.
    """
    if is_fallback or (not role and not company and not enrichment_text):
        return generate_email_base_template(name)

    profile = {
        "confidence": "Medium — legacy enrichment, single source",
        "confidence_level": "medium",
        "current_role": role,
        "company": company,
        "location": "",
        "domain": "",
        "batch_verified": "",
        "flags": "",
        "outreach_note": "",
        "email_hooks": [f"Works as {role} at {company}" if role and company else ""],
    }
    return generate_email_from_profile(name, profile)


if __name__ == "__main__":
    print("Testing email generation with Qwen 3 Max via OpenRouter...")
    subject, body = generate_email(
        name="Test User",
        role="Software Engineer",
        company="Google",
        enrichment_text="Works on cloud infrastructure. IIM Udaipur MBA 2022.",
    )
    print(f"\nSUBJECT: {subject}")
    print(f"\nBODY:\n{body}")
