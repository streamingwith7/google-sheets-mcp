"""
One-time helper script to get a Google OAuth 2.0 refresh token.

Run this ONCE on your Mac to authorize access to Google Sheets and Drive.
It will open your browser, ask you to log in, and print a refresh token.
Copy that token -- you'll paste it into Render as an environment variable.

Usage:
    python get_refresh_token.py
"""

import json
import os
import sys

# ---------------------------------------------------------------------------
# 1. Ask for your OAuth credentials (Client ID and Client Secret)
# ---------------------------------------------------------------------------
print("\n=== Google Sheets OAuth Setup ===\n")
client_id = input("Paste your Google Client ID: ").strip()
client_secret = input("Paste your Google Client Secret: ").strip()

if not client_id or not client_secret:
    print("Error: Both Client ID and Client Secret are required.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# 2. Build a temporary client config (same shape as a downloaded JSON)
# ---------------------------------------------------------------------------
client_config = {
    "installed": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

# ---------------------------------------------------------------------------
# 3. Run the OAuth "installed app" flow -- opens your browser
# ---------------------------------------------------------------------------
from google_auth_oauthlib.flow import InstalledAppFlow

# We need access to Google Sheets (read/write) and Google Drive (list files)
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Disable the OAuthlib HTTPS check since we're running on localhost
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)

# This starts a tiny local web server, opens the browser, and waits for
# Google to redirect back with an authorization code.
credentials = flow.run_local_server(
    port=8086,
    open_browser=True,
    success_message="Authentication complete! You can close this tab and go back to your terminal.",
)

# ---------------------------------------------------------------------------
# 4. Print the refresh token
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("SUCCESS! Here is your refresh token:\n")
print(credentials.refresh_token)
print("\n" + "=" * 60)
print("Copy the token above. You will need it as the")
print("GOOGLE_REFRESH_TOKEN environment variable on Render.")
print("Do NOT share this token with anyone.\n")
