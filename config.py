"""
Central configuration for Gmail Automation Pipeline.
Secrets are loaded from .env.local; never hardcode them here.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parent

# Load .env.local from project root.
load_dotenv(BASE_DIR / ".env.local")


def _parse_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except (ValueError, TypeError):
        print(f"WARNING: Invalid {name}='{raw_value}', using default {default}")
        return default


def _parse_float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        return float(raw_value)
    except (ValueError, TypeError):
        print(f"WARNING: Invalid {name}='{raw_value}', using default {default}")
        return default


def _resolve_google_credentials_file() -> str:
    """Resolve the OAuth client file from env, credentials.json, or one client_secret export."""
    configured = os.getenv("GOOGLE_CREDENTIALS_FILE", "").strip()
    if configured:
        configured_path = Path(configured)
        if not configured_path.is_absolute():
            configured_path = BASE_DIR / configured_path
        return str(configured_path)

    default_path = BASE_DIR / "credentials.json"
    if default_path.exists():
        return str(default_path)

    client_secret_files = sorted(BASE_DIR.glob("client_secret_*.json"))
    if len(client_secret_files) == 1:
        return str(client_secret_files[0])

    return str(default_path)


PLACEHOLDER_GOOGLE_SHEET_ID = "PLACEHOLDER_GOOGLE_SHEET_ID"


def require_google_sheet_id() -> str:
    """Return the configured sheet ID or raise a clear setup error."""
    if not GOOGLE_SHEET_ID or GOOGLE_SHEET_ID == PLACEHOLDER_GOOGLE_SHEET_ID:
        raise ValueError(
            "GOOGLE_SHEET_ID is not set. Add the target sheet ID to .env.local before "
            "running Phase 1 or sheet-backed Phase 2."
        )
    return GOOGLE_SHEET_ID


# OpenAI-compatible LLM backend
MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "gemini_aistudio").strip().lower() or "gemini_aistudio"
PROVIDER_MAP = {
    "gemini_aistudio": (
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "gemini-2.5-flash",
        "GEMINI_API_KEY",
    ),
    "openrouter_kimi": (
        "https://openrouter.ai/api/v1",
        "moonshotai/kimi-k2",
        "OPENROUTER_API_KEY",
    ),
    "openrouter_claude": (
        "https://openrouter.ai/api/v1",
        "anthropic/claude-sonnet-4-6",
        "OPENROUTER_API_KEY",
    ),
    "openrouter_gpt": (
        "https://openrouter.ai/api/v1",
        "openai/gpt-5.4",
        "OPENROUTER_API_KEY",
    ),
    "kimi_direct": (
        "https://api.moonshot.ai/v1",
        "kimi-k2-thinking",
        "KIMI_API_KEY",
    ),
}
PROVIDER_DEFAULTS = {
    "gemini_aistudio": {
        "RESEARCH_MAX_TOKENS": 8000,
        "EMAIL_MAX_TOKENS": 3000,
        "RESEARCH_TEMPERATURE": 1.0,
        "EMAIL_TEMPERATURE": 0.7,
    },
    "openrouter_kimi": {
        "RESEARCH_MAX_TOKENS": 6000,
        "EMAIL_MAX_TOKENS": 2000,
        "RESEARCH_TEMPERATURE": 0.7,
        "EMAIL_TEMPERATURE": 0.5,
    },
    "openrouter_claude": {
        "RESEARCH_MAX_TOKENS": 6000,
        "EMAIL_MAX_TOKENS": 2000,
        "RESEARCH_TEMPERATURE": 0.7,
        "EMAIL_TEMPERATURE": 0.5,
    },
    "openrouter_gpt": {
        "RESEARCH_MAX_TOKENS": 8000,
        "EMAIL_MAX_TOKENS": 3000,
        "RESEARCH_TEMPERATURE": 0.7,
        "EMAIL_TEMPERATURE": 0.5,
    },
    "kimi_direct": {
        "RESEARCH_MAX_TOKENS": 6000,
        "EMAIL_MAX_TOKENS": 2000,
        "RESEARCH_TEMPERATURE": 0.6,
        "EMAIL_TEMPERATURE": 0.6,
    },
}

if MODEL_PROVIDER not in PROVIDER_MAP:
    supported = ", ".join(sorted(PROVIDER_MAP))
    raise ValueError(f"Unsupported MODEL_PROVIDER '{MODEL_PROVIDER}'. Supported values: {supported}")

LLM_BASE_URL, LLM_MODEL, LLM_API_KEY_ENV_VAR = PROVIDER_MAP[MODEL_PROVIDER]
LLM_API_KEY = os.getenv(LLM_API_KEY_ENV_VAR, "").strip()
if not LLM_API_KEY or not LLM_API_KEY.strip():
    raise ValueError(
        f"{LLM_API_KEY_ENV_VAR} is missing or empty in .env.local - aborting before pipeline starts."
    )
client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)


def _sanitize_kwargs(provider: str, kwargs: dict) -> dict:
    """Strip provider-unsupported arguments before making an API call."""
    sanitized = {key: value for key, value in kwargs.items() if value is not None}

    if provider == "gemini_aistudio":
        # Only strip extra_body when search grounding is NOT active.
        # When GEMINI_SEARCH_GROUNDING is True, extra_body carries the
        # {"tools": [{"googleSearch": {}}]} payload and must be preserved.
        if not GEMINI_SEARCH_GROUNDING:
            sanitized.pop("extra_body", None)
        sanitized.pop("reasoning_effort", None)
        sanitized.pop("presence_penalty", None)
        sanitized.pop("frequency_penalty", None)

    if provider == "openrouter_claude":
        sanitized.pop("stop", None)

    return sanitized


# Google Sheets
# The spreadsheet ID from the URL: https://docs.google.com/spreadsheets/d/<THIS_PART>/edit
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", PLACEHOLDER_GOOGLE_SHEET_ID)
if (
    not GOOGLE_SHEET_ID
    or not GOOGLE_SHEET_ID.strip()
    or GOOGLE_SHEET_ID == PLACEHOLDER_GOOGLE_SHEET_ID
):
    print("WARNING: GOOGLE_SHEET_ID not set or placeholder - sheet-mode runs will fail")
SHEET_NAME = "Sheet1"
SHEET_NAME_TEMPLATE = os.getenv("SHEET_NAME_TEMPLATE", "cohort_{year}")

# Google People API
# OAuth credentials file downloaded from Google Cloud Console
GOOGLE_CREDENTIALS_FILE = _resolve_google_credentials_file()
GOOGLE_TOKEN_FILE = str(BASE_DIR / "token.json")
PEOPLE_API_SCOPES = [
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/directory.readonly",
]
SHEETS_API_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]
ALL_SCOPES = PEOPLE_API_SCOPES + SHEETS_API_SCOPES

# Tavily (Web Search Fallback with key rotation)
_tavily_raw = os.getenv("TAVILY_API_KEYS", "")
TAVILY_API_KEYS = [k.strip() for k in _tavily_raw.split(",") if k.strip()]
if not TAVILY_API_KEYS:
    print("WARNING: TAVILY_API_KEYS not set - Tavily enrichment will be unavailable")
TAVILY_SEARCH_DEPTH = os.getenv("TAVILY_SEARCH_DEPTH", "advanced").strip() or "advanced"
TAVILY_MAX_RESULTS = _parse_int_env("TAVILY_MAX_RESULTS", 3)
TAVILY_CHUNKS_PER_SOURCE = _parse_int_env("TAVILY_CHUNKS_PER_SOURCE", 3)

# --- Tavily freeze ---
# Set TAVILY_FROZEN=false in .env.local to re-enable Tavily enrichment.
# When True (default), all Tavily calls are bypassed and the LLM does its
# own native web search from the minimal user prompt only.
TAVILY_FROZEN = os.getenv("TAVILY_FROZEN", "true").strip().lower() != "false"
if TAVILY_FROZEN:
    print("INFO: Tavily is FROZEN — enrichment bypassed; LLM uses native search.")

# --- Gemini Google Search Grounding ---
# When True, injects google_search tool into Gemini research calls via extra_body.
# Only meaningful when MODEL_PROVIDER=gemini_aistudio and TAVILY_FROZEN=True.
GEMINI_SEARCH_GROUNDING = os.getenv("GEMINI_SEARCH_GROUNDING", "true").strip().lower() != "false"

# Pipeline Settings
ENRICHMENT_DELAY = 5.0
INPUT_CSV = os.getenv("INPUT_CSV", "alumni_raw.csv")
LOGS_DIR = str(BASE_DIR / "logs")
DEBUG_LOGS_DIR = str(BASE_DIR / "debug_logs")
PROGRESS_FILE = str(BASE_DIR / "progress.json")
COHORTS_DIR = str(BASE_DIR / "cohorts")
COHORTS_MANIFEST_FILE = str(Path(COHORTS_DIR) / "manifest.json")
COHORT_PHASE2_BATCH_SIZE = _parse_int_env("COHORT_PHASE2_BATCH_SIZE", 100)
GEMINI_CONCURRENCY = _parse_int_env("GEMINI_CONCURRENCY", 5)
KIMI_CONCURRENCY = _parse_int_env("KIMI_CONCURRENCY", 5)
TAVILY_CONCURRENCY = _parse_int_env("TAVILY_CONCURRENCY", 3)
RESEARCH_MAX_TOKENS = _parse_int_env(
    "RESEARCH_MAX_TOKENS", PROVIDER_DEFAULTS[MODEL_PROVIDER]["RESEARCH_MAX_TOKENS"]
)
EMAIL_MAX_TOKENS = _parse_int_env(
    "EMAIL_MAX_TOKENS", PROVIDER_DEFAULTS[MODEL_PROVIDER]["EMAIL_MAX_TOKENS"]
)
RESEARCH_TEMPERATURE = _parse_float_env(
    "RESEARCH_TEMPERATURE", PROVIDER_DEFAULTS[MODEL_PROVIDER]["RESEARCH_TEMPERATURE"]
)
EMAIL_TEMPERATURE = _parse_float_env(
    "EMAIL_TEMPERATURE", PROVIDER_DEFAULTS[MODEL_PROVIDER]["EMAIL_TEMPERATURE"]
)

STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_RESEARCH_DONE = "RESEARCH_DONE"
STATUS_EMAIL_DONE = "EMAIL_DONE"
STATUS_SENT = "SENT"
STATUS_FAILED = "FAILED"
STATUS_FAILED_PARSE = "FAILED_PARSE"
EMAIL_SOURCE_MANUAL_VERIFIED = "manual_verified"

COHORT_YEARS = [str(year) for year in range(2013, 2024)]


def cohort_tab_name(year: str | int) -> str:
    return SHEET_NAME_TEMPLATE.format(year=str(year))

COLUMN_NAME_MAP = {
    "Full Name": "Name",
    "full_name": "Name",
    "Member Name": "Name",
    "Batch Year": "Graduation_Year",
    "Graduation Year": "Graduation_Year",
    "batch": "Graduation_Year",
    "Year": "Graduation_Year",
    "Current Company": "AlmaConnect_Company",
    "Current_Company": "AlmaConnect_Company",
    "Company": "AlmaConnect_Company",
    "Profile URL": "LinkedIn_URL",
    "LinkedIn": "LinkedIn_URL",
    "LinkedIn URL": "LinkedIn_URL",
}

# Google Sheet Column Headers (in order)
SHEET_HEADERS = [
    "Name",
    "Email",
    "Email_Source",
    "Confidence_Level",
    "GENDER",
    "BATCH_YEAR",
    "PRIMARY_DOMAIN",
    "CONTEXT",
    "Graduation_Year",
    "AlmaConnect_Company",
    "Verified_Company",
    "LinkedIn_URL",
    "Enrichment_Notes",
    "Tavily_Raw",
    "Tavily_Metadata",
    "Enrichment_Source",
    "Subject",
    "Body",
    "Subject_v1",
    "Body_v1",
    "Subject_v2",
    "Body_v2",
    "Subject_v3",
    "Body_v3",
    "Sent",
    "STATUS",
]
