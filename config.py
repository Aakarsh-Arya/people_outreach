"""
Central configuration for Gmail Automation Pipeline.
Secrets are loaded from .env.local; never hardcode them here.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Load .env.local from project root.
load_dotenv(BASE_DIR / ".env.local")


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


# OpenAI-compatible LLM backend (Google AI Studio)
OPENROUTER_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENROUTER_MODEL = "gemini-2.5-flash"
OPENROUTER_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Google Sheets
# The spreadsheet ID from the URL: https://docs.google.com/spreadsheets/d/<THIS_PART>/edit
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", PLACEHOLDER_GOOGLE_SHEET_ID)
SHEET_NAME = "Sheet1"

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

# Email Sender Info
SENDER_NAME = "Aakarsh Arya"
SENDER_CONTEXT = (
    "an MBA graduate from IIM Udaipur (Class of 2025). "
    "He is reaching out to fellow IIM Udaipur alumni for professional networking."
)

# Pipeline Settings
PEOPLE_API_DELAY = 0.3
ENRICHMENT_DELAY = 5.0
BATCH_SIZE = 5
INPUT_CSV = os.getenv("INPUT_CSV", "alumni_raw.csv")

# Google Sheet Column Headers (in order)
SHEET_HEADERS = [
    "Name",
    "Email",
    "Email_Source",
    "Confidence_Level",
    "Graduation_Year",
    "AlmaConnect_Company",
    "Verified_Company",
    "LinkedIn_URL",
    "Enrichment_Notes",
    "Enrichment_Source",
    "Subject",
    "Body",
    "Sent",
]
