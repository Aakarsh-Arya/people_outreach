"""
Google API authentication helper.
Handles OAuth2 flow for People API and Sheets API.
"""

import os

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import config


def get_credentials():
    """Get or refresh OAuth2 credentials. Runs browser auth flow on first use."""
    creds = None

    if os.path.exists(config.GOOGLE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(
            config.GOOGLE_TOKEN_FILE, config.ALL_SCOPES
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                if os.path.exists(config.GOOGLE_TOKEN_FILE):
                    os.remove(config.GOOGLE_TOKEN_FILE)
                print(f"[Auth] Cached token invalid or revoked, starting re-auth flow: {exc}")
                creds = None
        else:
            if not os.path.exists(config.GOOGLE_CREDENTIALS_FILE):
                raise FileNotFoundError(
                    "Missing Google OAuth client file. Set GOOGLE_CREDENTIALS_FILE in .env.local, "
                    "place credentials.json in the project root, or keep a single "
                    "client_secret_*.json file there."
                )

        if not creds or not creds.valid:
            if not os.path.exists(config.GOOGLE_CREDENTIALS_FILE):
                raise FileNotFoundError(
                    "Missing Google OAuth client file. Set GOOGLE_CREDENTIALS_FILE in .env.local, "
                    "place credentials.json in the project root, or keep a single "
                    "client_secret_*.json file there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                config.GOOGLE_CREDENTIALS_FILE, config.ALL_SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(config.GOOGLE_TOKEN_FILE, "w", encoding="utf-8") as token:
            token.write(creds.to_json())

    return creds


def get_people_service():
    """Build and return a Google People API service object."""
    creds = get_credentials()
    return build("people", "v1", credentials=creds)


def get_sheets_service():
    """Build and return a Google Sheets API service object."""
    creds = get_credentials()
    return build("sheets", "v4", credentials=creds)
