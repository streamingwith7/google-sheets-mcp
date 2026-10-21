# Google Drive, Sheets & Gmail MCP Server (multi-account)

A remote [MCP](https://modelcontextprotocol.io) server that gives Claude Google Drive, Google Sheets and Gmail tools for **more than one Google account** from a single custom connector. Every tool takes an `account` argument (for example `personal` or `fdhb`), so Claude always says which account it is acting on.

## Tools

| Area | Tools |
|---|---|
| Accounts | `list_accounts` |
| Sheets | `list_spreadsheets`, `create_spreadsheet`, `get_spreadsheet_info`, `add_tab`, `delete_tab`, `read_sheet`, `write_sheet`, `search_sheet` |
| Drive | `list_files`, `search_drive` (name, type, owned-by-me, shared-with-me), `get_file_info`, `get_file_permissions`, `read_file` (Docs/Sheets/Slides as text), `create_folder`, `create_text_file`, `rename_file`, `move_file`, `copy_file`, `share_file`, `remove_permission`, `delete_file` |
| Gmail | `search_emails`, `read_email`, `read_thread`, `list_labels`, `modify_labels`, `create_draft`, `send_email`, `send_draft` |

## How auth works

- Each account is a long-lived Google OAuth **refresh token** stored as an environment variable on the server (`ACCOUNT_<NAME>_REFRESH_TOKEN`). One OAuth client (`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`) serves all accounts.
- The MCP endpoint lives at an unguessable path: `https://<host>/mcp-<MCP_PATH_SECRET>`. Treat that URL like a password; it is the only thing standing between the internet and your Drive.
- The legacy single-account variable `GOOGLE_REFRESH_TOKEN` still works and maps to the account name `fdhb`. With no `MCP_PATH_SECRET` the endpoint is `/mcp`, as before.

## Setup

### 1. Google Cloud OAuth client (once)

1. [Google Cloud Console](https://console.cloud.google.com/) > create or pick a project.
2. **APIs & Services > Library**: enable **Google Sheets API**, **Google Drive API** and **Gmail API**.
3. **APIs & Services > OAuth consent screen**: External, add yourself (and any other account you will authorize) as a test user, add the scopes `.../auth/spreadsheets`, `.../auth/drive`, `.../auth/gmail.modify`.
4. **Credentials > Create credentials > OAuth client ID > Desktop app**. Copy the Client ID and Client Secret.

### 2. A refresh token per account (on your Mac)

```bash
git clone https://github.com/streamingwith7/google-sheets-mcp
cd google-sheets-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python get_refresh_token.py      # sign in as the PERSONAL account, name it "personal"
python get_refresh_token.py      # run again signed in as the work account, name it "fdhb"
```

Each run prints an `ACCOUNT_<NAME>_REFRESH_TOKEN` line.

### 3. Deploy on Render

1. Render dashboard > **New +** > **Blueprint** > select this repo. `render.yaml` defines the service.
2. Paste `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and the refresh tokens when prompted. `MCP_PATH_SECRET` is generated for you.
3. After the first deploy, open the service > **Environment** and copy the `MCP_PATH_SECRET` value.

Your connector URL is `https://<service>.onrender.com/mcp-<MCP_PATH_SECRET>`.

The free plan sleeps after ~15 minutes idle; the first call after that takes 30-60 s. Upgrade the instance if that annoys you.

### 4. Add it to Claude

claude.ai > **Customize > Connectors > Add custom connector** > paste the URL. No sign-in step: the URL *is* the credential.

## Running locally

```bash
export GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... ACCOUNT_PERSONAL_REFRESH_TOKEN=...
python server.py            # serves http://localhost:8000/mcp
```

## Adding another account later

Run `get_refresh_token.py` again with a new name, add `ACCOUNT_<NAME>_REFRESH_TOKEN` on Render, redeploy. If that account uses a different OAuth client, also set `ACCOUNT_<NAME>_CLIENT_ID` and `ACCOUNT_<NAME>_CLIENT_SECRET`.
