# Google Sheets MCP Server

A remote [MCP](https://modelcontextprotocol.io) server that lets Claude read, write, and manage your Google Sheets -- from the desktop app, the web, or your phone.

## What it does

Once deployed, Claude can:
- **List your spreadsheets** from Google Drive
- **Create new spreadsheets**
- **Get spreadsheet info** including all tab names
- **Add and delete tabs** within a spreadsheet
- **Read data** from any range of cells
- **Write data** (overwrite or append rows)
- **Search** for specific text across a sheet

---

## Prerequisites

- **Python 3.11+** installed on your Mac
- A **Google Cloud project** with the Google Sheets API and Google Drive API enabled, plus OAuth 2.0 credentials (type: "Desktop app")
- A **GitHub account** (with the `gh` CLI installed -- `brew install gh`)
- A free **[Render](https://render.com)** account for hosting

---

## Step-by-step setup

### Step 1: Enable the Google APIs

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (or use an existing one).
3. Go to **APIs & Services** > **Library**.
4. Search for **Google Sheets API** and click **Enable**.
5. Search for **Google Drive API** and click **Enable**.
6. Go to **APIs & Services** > **Credentials**.
7. Click **Create Credentials** > **OAuth client ID**.
8. Choose **Desktop app** as the application type.
9. Copy the **Client ID** and **Client Secret** -- you'll need them in the next step.

### Step 2: Get your Google OAuth refresh token

This is a one-time step you run on your Mac. It opens your browser, asks you to log into Google, and prints a refresh token that the server uses to stay authenticated.

```bash
# Go into the project folder
cd ~/google-sheets-mcp

# Create a virtual environment and activate it
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the OAuth helper script
python get_refresh_token.py
```

The script will ask you to paste your **Client ID** and **Client Secret**, then open your browser. Log in with the Google account whose spreadsheets you want to manage, and click "Allow".

When it finishes, you'll see a line like:

```
SUCCESS! Here is your refresh token:

1//0eXXXXXXXXXXXXXXXXXXXXXXXXXX
```

**Copy that token** and save it somewhere safe (e.g. a note). You'll need it in Step 4.

---

### Step 3: Push to GitHub

If you haven't already, create a GitHub repo and push the code:

```bash
cd ~/google-sheets-mcp
git init
git add .
git commit -m "Initial commit: Google Sheets MCP server"
gh repo create google-sheets-mcp --public --source=. --push
```

---

### Step 4: Deploy to Render

1. Go to [render.com](https://render.com) and sign in.
2. Click **"New +"** > **"Web Service"**.
3. Connect your GitHub account if you haven't, then select the **google-sheets-mcp** repo.
4. Render will auto-detect the settings from `render.yaml`. Verify:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `python server.py`
5. Scroll to **Environment Variables** and add these three:

   | Key                    | Value                        |
   |------------------------|------------------------------|
   | `GOOGLE_CLIENT_ID`     | Your OAuth Client ID         |
   | `GOOGLE_CLIENT_SECRET` | Your OAuth Client Secret     |
   | `GOOGLE_REFRESH_TOKEN` | The token from Step 2        |

6. Click **"Create Web Service"** and wait for the deploy to finish.
7. Copy your service URL -- it will look like `https://google-sheets-mcp-xxxx.onrender.com`.

---

### Step 5: Register in Claude as a remote MCP connector

#### Claude Desktop (Mac)

1. Open Claude > **Settings** (gear icon) > **Integrations**.
2. Click **"Add custom integration"**.
3. Set the name to **Google Sheets**.
4. Set the URL to: `https://google-sheets-mcp-xxxx.onrender.com/mcp` (your Render URL + `/mcp`).
5. Click **Save**.

#### Claude Web (claude.ai)

1. Go to [claude.ai](https://claude.ai) > **Settings** > **Integrations**.
2. Follow the same steps as above.

#### Claude Mobile (iOS / Android)

Remote MCP integrations added in desktop or web sync automatically to your mobile app.

---

## Testing it out

Start a new conversation with Claude and try:

> "List my Google Sheets spreadsheets."

> "Create a new spreadsheet called 'Budget 2026'."

> "Read the data from spreadsheet ID abc123."

> "Add a row with Name=Alice and Age=30 to my spreadsheet."

> "Search for 'Alice' in my spreadsheet."

---

## Running locally (for development)

```bash
cd ~/google-sheets-mcp
source venv/bin/activate

# Set env vars for local testing
export GOOGLE_CLIENT_ID="your-client-id"
export GOOGLE_CLIENT_SECRET="your-client-secret"
export GOOGLE_REFRESH_TOKEN="your-refresh-token"

python server.py
```

The server runs on `http://localhost:8000`. You can point Claude Desktop at `http://localhost:8000/mcp` for local testing.

---

## Troubleshooting

- **"invalid_grant" error:** Your refresh token may have expired. Re-run `python get_refresh_token.py` to get a new one, then update it in Render.
- **"Access Not Configured" error:** Make sure both the Google Sheets API and Google Drive API are enabled in your Google Cloud project.
- **Server won't start on Render:** Check the Render logs. Usually it's a missing environment variable.
