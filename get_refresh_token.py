"""
One-time helper: get a Google OAuth 2.0 refresh token for ONE Google account.

Run it once per account you want the server to reach (e.g. once signed in as
your personal Gmail, once as your work account). It opens your browser, you
pick the account and approve, and it prints the refresh token to paste into
Render as ACCOUNT_<NAME>_REFRESH_TOKEN.

Usage:
    python get_refresh_token.py
"""

import os
import sys

print("\n=== Google OAuth refresh token ===\n")
client_id = input("Paste your Google Client ID: ").strip()
client_secret = input("Paste your Google Client Secret: ").strip()
account_name = input("Short name for this account (e.g. personal, fdhb): ").strip().lower() or "personal"

if not client_id or not client_secret:
    print("Error: Both Client ID and Client Secret are required.")
    sys.exit(1)

client_config = {
    "installed": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

# Sheets + Drive + Gmail (read, draft, send, label). Must match SCOPES in server.py.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.modify",
]

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
credentials = flow.run_local_server(
    port=8086,
    open_browser=True,
    # prompt=consent guarantees Google returns a refresh token even if you
    # already approved this app before.
    prompt="consent",
    success_message="Done. You can close this tab and go back to your terminal.",
)

env_key = f"ACCOUNT_{account_name.upper()}_REFRESH_TOKEN"
print("\n" + "=" * 60)
print(f"SUCCESS. Add this environment variable on Render:\n")
print(f"  {env_key}")
print(f"  {credentials.refresh_token}")
print("\n" + "=" * 60)
print("Do NOT share this token with anyone. Run the script again for the next account.\n")
