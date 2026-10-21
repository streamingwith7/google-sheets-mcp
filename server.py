"""
Google Drive, Sheets & Gmail MCP Server (multi-account)
=======================================================
A remote MCP (Model Context Protocol) server that gives Claude Google Drive,
Google Sheets and Gmail tools for MORE THAN ONE Google account from a single
connector. Every tool takes an `account` argument naming which Google account
to act on, so Claude can never accidentally write to the wrong Drive.

Transport: Streamable HTTP (works as a Claude custom connector).
Auth:      One long-lived Google OAuth 2.0 refresh token per account, read
           from environment variables. The MCP endpoint itself lives at an
           unguessable path (MCP_PATH_SECRET) so nobody who stumbles on the
           hostname can use it.

Accounts are declared with environment variables:

    GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET      shared OAuth client (one is fine)
    ACCOUNT_PERSONAL_REFRESH_TOKEN               refresh token for account "personal"
    ACCOUNT_FDHB_REFRESH_TOKEN                   refresh token for account "fdhb"
    ACCOUNT_<NAME>_REFRESH_TOKEN                 any other account you want ("<name>" lowercased)
    ACCOUNT_<NAME>_CLIENT_ID / _CLIENT_SECRET    optional per-account OAuth client override
    GOOGLE_REFRESH_TOKEN                         legacy single-account token -> account "fdhb"
    MCP_PATH_SECRET                              optional; endpoint becomes /mcp-<secret>
    DEFAULT_ACCOUNT                              optional; used only when a tool call omits
                                                 `account` AND exactly one account exists
"""

from __future__ import annotations

import base64
import email.utils
import html
import os
import re
from email.message import EmailMessage
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.modify",
]

_ENV_PREFIX = "ACCOUNT_"
_ENV_SUFFIX = "_REFRESH_TOKEN"


def _load_accounts() -> dict[str, dict[str, str]]:
    """Read every ACCOUNT_<NAME>_REFRESH_TOKEN plus the legacy GOOGLE_REFRESH_TOKEN."""
    shared_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    shared_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    accounts: dict[str, dict[str, str]] = {}

    for key, value in os.environ.items():
        if key.startswith(_ENV_PREFIX) and key.endswith(_ENV_SUFFIX) and value.strip().lower() not in ("", "none", "skip", "-"):
            name = key[len(_ENV_PREFIX) : -len(_ENV_SUFFIX)].lower()
            accounts[name] = {
                "refresh_token": value,
                "client_id": os.environ.get(f"{_ENV_PREFIX}{name.upper()}_CLIENT_ID", shared_id),
                "client_secret": os.environ.get(
                    f"{_ENV_PREFIX}{name.upper()}_CLIENT_SECRET", shared_secret
                ),
            }

    # Backward compatibility with the original single-account deploy.
    legacy = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
    if legacy and "fdhb" not in accounts:
        accounts["fdhb"] = {
            "refresh_token": legacy,
            "client_id": shared_id,
            "client_secret": shared_secret,
        }
    return accounts


ACCOUNTS = _load_accounts()
DEFAULT_ACCOUNT = os.environ.get("DEFAULT_ACCOUNT", "").lower()


def _resolve_account(account: str) -> str:
    """Validate the `account` argument. Raises a clear error on mistakes."""
    name = (account or "").strip().lower()
    if not name:
        if DEFAULT_ACCOUNT and DEFAULT_ACCOUNT in ACCOUNTS:
            return DEFAULT_ACCOUNT
        if len(ACCOUNTS) == 1:
            return next(iter(ACCOUNTS))
        raise ValueError(
            f"`account` is required. Configured accounts: {sorted(ACCOUNTS)}"
        )
    if name not in ACCOUNTS:
        raise ValueError(
            f"Unknown account '{account}'. Configured accounts: {sorted(ACCOUNTS)}"
        )
    return name


def _credentials(account: str) -> Credentials:
    cfg = ACCOUNTS[_resolve_account(account)]
    return Credentials(
        token=None,
        refresh_token=cfg["refresh_token"],
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        token_uri="https://oauth2.googleapis.com/token",
    )


def _sheets(account: str):
    return build("sheets", "v4", credentials=_credentials(account), cache_discovery=False)


def _drive(account: str):
    return build("drive", "v3", credentials=_credentials(account), cache_discovery=False)


def _gmail(account: str):
    return build("gmail", "v1", credentials=_credentials(account), cache_discovery=False)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
port = int(os.environ.get("PORT", 8000))
_secret = os.environ.get("MCP_PATH_SECRET", "").strip()
MCP_PATH = f"/mcp-{_secret}" if _secret else "/mcp"

mcp = FastMCP(
    name="Google Drive, Sheets & Gmail",
    instructions=(
        "Google Drive, Sheets and Gmail for several Google accounts at once. "
        "Every tool takes `account`; configured accounts: "
        + (", ".join(sorted(ACCOUNTS)) or "none yet")
        + ". Call list_accounts first if unsure. Pick the account that owns the "
        "data the user is talking about; never guess between accounts."
    ),
    host="0.0.0.0",
    port=port,
    streamable_http_path=MCP_PATH,
    stateless_http=True,
    json_response=True,
)


# ===========================================================================
# Account tools
# ===========================================================================
@mcp.tool()
def list_accounts() -> list[dict]:
    """
    List the Google accounts this server can act on, with the email address
    behind each one. Call this first when unsure which `account` to pass.
    """
    out = []
    for name in sorted(ACCOUNTS):
        info: dict[str, Any] = {"account": name}
        try:
            about = _drive(name).about().get(fields="user(emailAddress,displayName)").execute()
            info["email"] = about["user"].get("emailAddress", "")
            info["displayName"] = about["user"].get("displayName", "")
            info["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 - surface any auth problem
            info["status"] = f"error: {exc}"
        out.append(info)
    return out


# ===========================================================================
# Google Sheets tools
# ===========================================================================
@mcp.tool()
def list_spreadsheets(account: str, query: str = "", max_results: int = 20) -> list[dict]:
    """
    Search Google Drive for spreadsheets the account can open.

    Args:
        account: Which Google account to use (see list_accounts).
        query: Optional search term to filter spreadsheets by name.
        max_results: Maximum number of spreadsheets to return (default 20).
    """
    q = "mimeType='application/vnd.google-apps.spreadsheet' and trashed = false"
    if query:
        q += f" and name contains '{_escape(query)}'"
    resp = (
        _drive(account)
        .files()
        .list(q=q, pageSize=max_results, fields="files(id, name, modifiedTime)", orderBy="modifiedTime desc")
        .execute()
    )
    return [{"id": f["id"], "name": f["name"], "modifiedTime": f.get("modifiedTime", "")} for f in resp.get("files", [])]


@mcp.tool()
def create_spreadsheet(account: str, title: str, folder_id: str = "") -> dict:
    """
    Create a new blank Google Spreadsheet.

    Args:
        account: Which Google account to use.
        title: Name for the new spreadsheet.
        folder_id: Optional Drive folder to put it in (default: My Drive root).
    """
    result = _sheets(account).spreadsheets().create(body={"properties": {"title": title}}).execute()
    sid = result["spreadsheetId"]
    if folder_id:
        _drive(account).files().update(fileId=sid, addParents=folder_id, removeParents="root", fields="id").execute()
    return {"id": sid, "title": result["properties"]["title"], "url": result["spreadsheetUrl"]}


@mcp.tool()
def get_spreadsheet_info(account: str, spreadsheet_id: str) -> dict:
    """
    Get a spreadsheet's title and all its tabs (name, sheetId, size).

    Args:
        account: Which Google account to use.
        spreadsheet_id: The spreadsheet ID (from its URL or list_spreadsheets).
    """
    result = (
        _sheets(account)
        .spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="properties.title,sheets.properties")
        .execute()
    )
    tabs = []
    for sheet in result.get("sheets", []):
        p = sheet.get("properties", {})
        tabs.append(
            {
                "sheetId": p.get("sheetId"),
                "title": p.get("title", ""),
                "index": p.get("index", 0),
                "rowCount": p.get("gridProperties", {}).get("rowCount", 0),
                "columnCount": p.get("gridProperties", {}).get("columnCount", 0),
            }
        )
    return {"title": result.get("properties", {}).get("title", ""), "spreadsheetId": spreadsheet_id, "tabs": tabs}


@mcp.tool()
def add_tab(account: str, spreadsheet_id: str, tab_name: str) -> dict:
    """
    Add a new tab (sheet) to an existing spreadsheet.

    Args:
        account: Which Google account to use.
        spreadsheet_id: The spreadsheet ID.
        tab_name: Name for the new tab.
    """
    body = {"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
    result = _sheets(account).spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()
    props = result["replies"][0]["addSheet"]["properties"]
    return {"sheetId": props["sheetId"], "title": props["title"]}


@mcp.tool()
def read_sheet(account: str, spreadsheet_id: str, range: str = "Sheet1") -> list[list[str]]:
    """
    Read cell values from a range.

    Args:
        account: Which Google account to use.
        spreadsheet_id: The spreadsheet ID.
        range: A1 notation, e.g. "Sheet1", "Sheet1!A1:D10", "MyTab!A:F".

    Returns rows as lists of strings.
    """
    result = _sheets(account).spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=range).execute()
    return result.get("values", [])


@mcp.tool()
def write_sheet(
    account: str,
    spreadsheet_id: str,
    range: str,
    values: list[list[str]],
    mode: str = "overwrite",
) -> dict:
    """
    Write cell values to a range.

    Args:
        account: Which Google account to use.
        spreadsheet_id: The spreadsheet ID.
        range: A1 notation target, e.g. "Sheet1!A1" or "MyTab!A1:D5".
        values: Rows of cell values, e.g. [["Name", "Age"], ["Alice", "30"]].
        mode: "overwrite" (default) replaces the range; "append" adds rows after the last data row.
    """
    svc = _sheets(account).spreadsheets().values()
    body = {"values": values}
    if mode == "append":
        result = svc.append(
            spreadsheetId=spreadsheet_id,
            range=range,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()
        u = result.get("updates", {})
    else:
        u = svc.update(spreadsheetId=spreadsheet_id, range=range, valueInputOption="USER_ENTERED", body=body).execute()
    return {
        "updatedRange": u.get("updatedRange", ""),
        "updatedRows": u.get("updatedRows", 0),
        "updatedColumns": u.get("updatedColumns", 0),
        "updatedCells": u.get("updatedCells", 0),
    }


@mcp.tool()
def search_sheet(account: str, spreadsheet_id: str, tab_name: str = "Sheet1", query: str = "") -> list[dict]:
    """
    Find rows in a tab where any cell contains `query` (case-insensitive).
    Empty query returns every row.

    Args:
        account: Which Google account to use.
        spreadsheet_id: The spreadsheet ID.
        tab_name: Tab to search (default "Sheet1").
        query: Text to look for.
    """
    rows = (
        _sheets(account).spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=tab_name).execute().get("values", [])
    )
    needle = query.lower()
    out = []
    for i, row in enumerate(rows, start=1):
        if not needle or any(needle in str(c).lower() for c in row):
            out.append({"row_number": i, "values": row})
    return out


@mcp.tool()
def delete_tab(account: str, spreadsheet_id: str, tab_name: str) -> str:
    """
    Delete a tab (sheet) by name. The last remaining tab cannot be deleted.

    Args:
        account: Which Google account to use.
        spreadsheet_id: The spreadsheet ID.
        tab_name: Name of the tab to delete.
    """
    meta = _sheets(account).spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties").execute()
    sheet_id = next(
        (s["properties"]["sheetId"] for s in meta.get("sheets", []) if s.get("properties", {}).get("title") == tab_name),
        None,
    )
    if sheet_id is None:
        return f"Error: No tab named '{tab_name}' in this spreadsheet."
    _sheets(account).spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": [{"deleteSheet": {"sheetId": sheet_id}}]}
    ).execute()
    return f"Tab '{tab_name}' deleted."


# ===========================================================================
# Google Drive tools
# ===========================================================================
_TYPE_MAP = {
    "folder": "application/vnd.google-apps.folder",
    "spreadsheet": "application/vnd.google-apps.spreadsheet",
    "document": "application/vnd.google-apps.document",
    "presentation": "application/vnd.google-apps.presentation",
    "pdf": "application/pdf",
    "image": "image/",
}
_FILE_FIELDS = "id, name, mimeType, modifiedTime, size, parents, owners(emailAddress), shared, webViewLink"


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _file_row(f: dict) -> dict:
    return {
        "id": f["id"],
        "name": f["name"],
        "mimeType": f.get("mimeType", ""),
        "modifiedTime": f.get("modifiedTime", ""),
        "size": f.get("size", ""),
        "parents": f.get("parents", []),
        "owner": (f.get("owners") or [{}])[0].get("emailAddress", ""),
        "shared": f.get("shared", False),
        "webViewLink": f.get("webViewLink", ""),
    }


@mcp.tool()
def list_files(
    account: str,
    folder_id: str = "root",
    query: str = "",
    file_type: str = "",
    max_results: int = 50,
    page_token: str = "",
) -> dict:
    """
    List files and folders inside one Drive folder.

    Args:
        account: Which Google account to use.
        folder_id: "root" for My Drive, or a folder ID.
        query: Optional name filter.
        file_type: "folder", "spreadsheet", "document", "presentation", "pdf", "image" or "".
        max_results: Page size (default 50, max 1000).
        page_token: Pass the previous result's nextPageToken to continue.

    Returns {"files": [...], "nextPageToken": "..."}.
    """
    q = [f"'{folder_id}' in parents", "trashed = false"]
    if query:
        q.append(f"name contains '{_escape(query)}'")
    if file_type in _TYPE_MAP:
        mime = _TYPE_MAP[file_type]
        q.append(f"mimeType contains '{mime}'" if file_type == "image" else f"mimeType = '{mime}'")
    resp = (
        _drive(account)
        .files()
        .list(
            q=" and ".join(q),
            pageSize=min(max_results, 1000),
            pageToken=page_token or None,
            fields=f"nextPageToken, files({_FILE_FIELDS})",
            orderBy="folder, modifiedTime desc",
        )
        .execute()
    )
    return {"files": [_file_row(f) for f in resp.get("files", [])], "nextPageToken": resp.get("nextPageToken", "")}


@mcp.tool()
def search_drive(
    account: str,
    query: str = "",
    file_type: str = "",
    owned_by_me: bool | None = None,
    shared_with_me: bool = False,
    max_results: int = 50,
    page_token: str = "",
) -> dict:
    """
    Search the whole Drive (My Drive + shared) by file name, with filters.

    Args:
        account: Which Google account to use.
        query: Name filter (empty = everything matching the other filters).
        file_type: "folder", "spreadsheet", "document", "presentation", "pdf", "image" or "".
        owned_by_me: True = only files this account owns; False = only files it doesn't; None = both.
        shared_with_me: True = only files other people shared into this account.
        max_results: Page size (default 50, max 1000).
        page_token: Previous result's nextPageToken.

    Returns {"files": [...], "nextPageToken": "..."}.
    """
    q = ["trashed = false"]
    if query:
        q.append(f"name contains '{_escape(query)}'")
    if file_type in _TYPE_MAP:
        mime = _TYPE_MAP[file_type]
        q.append(f"mimeType contains '{mime}'" if file_type == "image" else f"mimeType = '{mime}'")
    if owned_by_me is True:
        q.append("'me' in owners")
    elif owned_by_me is False:
        q.append("not 'me' in owners")
    if shared_with_me:
        q.append("sharedWithMe = true")
    resp = (
        _drive(account)
        .files()
        .list(
            q=" and ".join(q),
            pageSize=min(max_results, 1000),
            pageToken=page_token or None,
            fields=f"nextPageToken, files({_FILE_FIELDS})",
            orderBy="modifiedTime desc",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        )
        .execute()
    )
    return {"files": [_file_row(f) for f in resp.get("files", [])], "nextPageToken": resp.get("nextPageToken", "")}


@mcp.tool()
def get_file_info(account: str, file_id: str) -> dict:
    """
    Full metadata for one file or folder: type, size, dates, owner, link, parents, sharing flag.

    Args:
        account: Which Google account to use.
        file_id: The file or folder ID.
    """
    f = (
        _drive(account)
        .files()
        .get(fileId=file_id, fields=f"{_FILE_FIELDS}, createdTime, description", supportsAllDrives=True)
        .execute()
    )
    row = _file_row(f)
    row["createdTime"] = f.get("createdTime", "")
    row["description"] = f.get("description", "")
    return row


@mcp.tool()
def get_file_permissions(account: str, file_id: str) -> list[dict]:
    """
    Who can access a file or folder: every permission with role, type and email.
    Use this to audit sharing.

    Args:
        account: Which Google account to use.
        file_id: The file or folder ID.
    """
    resp = (
        _drive(account)
        .permissions()
        .list(fileId=file_id, fields="permissions(id, type, role, emailAddress, domain, displayName, allowFileDiscovery)", supportsAllDrives=True)
        .execute()
    )
    return resp.get("permissions", [])


@mcp.tool()
def read_file(account: str, file_id: str, max_chars: int = 60000) -> dict:
    """
    Read a file's text content. Google Docs export as plain text, Sheets as CSV
    (first tab), Slides as plain text; other text-like files are downloaded as-is.
    Binary files (images, PDFs with no text layer) are not readable here.

    Args:
        account: Which Google account to use.
        file_id: The file ID.
        max_chars: Truncate the content after this many characters (default 60000).
    """
    drive = _drive(account)
    meta = drive.files().get(fileId=file_id, fields="name, mimeType", supportsAllDrives=True).execute()
    mime = meta.get("mimeType", "")
    export = {
        "application/vnd.google-apps.document": "text/plain",
        "application/vnd.google-apps.spreadsheet": "text/csv",
        "application/vnd.google-apps.presentation": "text/plain",
    }
    if mime in export:
        data = drive.files().export(fileId=file_id, mimeType=export[mime]).execute()
    elif mime.startswith("text/") or mime in ("application/json", "application/xml", "text/markdown"):
        data = drive.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
    else:
        return {"name": meta.get("name", ""), "mimeType": mime, "content": "", "note": "Binary file; not readable as text."}
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    truncated = len(text) > max_chars
    return {"name": meta.get("name", ""), "mimeType": mime, "content": text[:max_chars], "truncated": truncated}


@mcp.tool()
def create_folder(account: str, name: str, parent_folder_id: str = "root") -> dict:
    """
    Create a folder.

    Args:
        account: Which Google account to use.
        name: Folder name.
        parent_folder_id: "root" or a folder ID to nest inside.
    """
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_folder_id]}
    r = _drive(account).files().create(body=body, fields="id, name, webViewLink").execute()
    return {"id": r["id"], "name": r["name"], "webViewLink": r.get("webViewLink", "")}


@mcp.tool()
def create_text_file(account: str, name: str, content: str, parent_folder_id: str = "root", mime_type: str = "text/plain") -> dict:
    """
    Create a plain file in Drive from text (notes, CSV, markdown, JSON).

    Args:
        account: Which Google account to use.
        name: File name including extension.
        content: The file's text.
        parent_folder_id: "root" or a folder ID.
        mime_type: e.g. "text/plain", "text/csv", "text/markdown", "application/json".
    """
    from googleapiclient.http import MediaInMemoryUpload

    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime_type)
    r = (
        _drive(account)
        .files()
        .create(body={"name": name, "parents": [parent_folder_id]}, media_body=media, fields="id, name, webViewLink")
        .execute()
    )
    return {"id": r["id"], "name": r["name"], "webViewLink": r.get("webViewLink", "")}


@mcp.tool()
def rename_file(account: str, file_id: str, new_name: str) -> dict:
    """
    Rename a file or folder.

    Args:
        account: Which Google account to use.
        file_id: The file or folder ID.
        new_name: The new name.
    """
    r = _drive(account).files().update(fileId=file_id, body={"name": new_name}, fields="id, name", supportsAllDrives=True).execute()
    return {"id": r["id"], "name": r["name"]}


@mcp.tool()
def move_file(account: str, file_id: str, new_folder_id: str) -> dict:
    """
    Move a file or folder into another folder (within the same account).

    Args:
        account: Which Google account to use.
        file_id: The file or folder ID.
        new_folder_id: Destination folder ID ("root" for My Drive).
    """
    drive = _drive(account)
    current = drive.files().get(fileId=file_id, fields="parents", supportsAllDrives=True).execute()
    r = (
        drive.files()
        .update(
            fileId=file_id,
            addParents=new_folder_id,
            removeParents=",".join(current.get("parents", [])),
            fields="id, name, parents",
            supportsAllDrives=True,
        )
        .execute()
    )
    return {"id": r["id"], "name": r["name"], "parents": r.get("parents", [])}


@mcp.tool()
def copy_file(account: str, file_id: str, new_name: str = "", destination_folder_id: str = "") -> dict:
    """
    Copy a file (within the same account).

    Args:
        account: Which Google account to use.
        file_id: The file ID.
        new_name: Optional name for the copy.
        destination_folder_id: Optional folder for the copy.
    """
    body: dict[str, Any] = {}
    if new_name:
        body["name"] = new_name
    if destination_folder_id:
        body["parents"] = [destination_folder_id]
    r = _drive(account).files().copy(fileId=file_id, body=body, fields="id, name, webViewLink", supportsAllDrives=True).execute()
    return {"id": r["id"], "name": r["name"], "webViewLink": r.get("webViewLink", "")}


@mcp.tool()
def copy_tree(account: str, source_folder_id: str, destination_parent_id: str, new_name: str = "", max_files: int = 2000) -> dict:
    """
    Recursively copy a whole folder (and everything inside it) into another
    folder, as new files owned by this account. Works for folders that were
    shared with this account from somewhere else, so it is the way to bring a
    folder across from another Google account.

    Args:
        account: Which Google account to use (the destination account).
        source_folder_id: The folder to copy (must be readable by this account).
        destination_parent_id: Folder to create the copy inside ("root" for My Drive).
        new_name: Optional name for the top-level copy (default: same name).
        max_files: Safety cap on the number of files copied (default 2000).

    Returns counts plus the new top-level folder id, and a list of any items
    that could not be copied (shortcuts, files without copy permission, etc.).
    """
    drive = _drive(account)
    stats = {"folders_created": 0, "files_copied": 0, "skipped": []}

    def _children(folder_id: str):
        token = None
        while True:
            resp = (
                drive.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    pageSize=1000,
                    pageToken=token,
                    fields="nextPageToken, files(id, name, mimeType)",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            yield from resp.get("files", [])
            token = resp.get("nextPageToken")
            if not token:
                return

    def _walk(src_id: str, dst_parent: str, name: str) -> str:
        new_folder = (
            drive.files()
            .create(body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [dst_parent]}, fields="id")
            .execute()
        )
        stats["folders_created"] += 1
        for child in _children(src_id):
            if stats["files_copied"] >= max_files:
                stats["skipped"].append({"name": child["name"], "reason": "max_files reached"})
                continue
            if child["mimeType"] == "application/vnd.google-apps.folder":
                _walk(child["id"], new_folder["id"], child["name"])
            elif child["mimeType"] == "application/vnd.google-apps.shortcut":
                stats["skipped"].append({"name": child["name"], "reason": "shortcut"})
            else:
                try:
                    drive.files().copy(fileId=child["id"], body={"name": child["name"], "parents": [new_folder["id"]]}, fields="id", supportsAllDrives=True).execute()
                    stats["files_copied"] += 1
                except Exception as exc:  # noqa: BLE001 - keep going, report at the end
                    stats["skipped"].append({"name": child["name"], "reason": str(exc)[:200]})
        return new_folder["id"]

    src = drive.files().get(fileId=source_folder_id, fields="name, mimeType", supportsAllDrives=True).execute()
    if src.get("mimeType") != "application/vnd.google-apps.folder":
        raise ValueError("source_folder_id is not a folder; use copy_file for single files")
    top_id = _walk(source_folder_id, destination_parent_id, new_name or src["name"])
    stats["new_folder_id"] = top_id
    stats["new_folder_link"] = f"https://drive.google.com/drive/folders/{top_id}"
    return stats


@mcp.tool()
def share_file(account: str, file_id: str, email: str, role: str = "reader", notify: bool = False) -> dict:
    """
    Share a file or folder with one email address.

    Args:
        account: Which Google account to use.
        file_id: The file or folder ID.
        email: Who to share with.
        role: "reader", "commenter" or "writer".
        notify: Send Google's share email (default False).
    """
    r = (
        _drive(account)
        .permissions()
        .create(
            fileId=file_id,
            body={"type": "user", "role": role, "emailAddress": email},
            sendNotificationEmail=notify,
            fields="id, role, emailAddress",
            supportsAllDrives=True,
        )
        .execute()
    )
    return r


@mcp.tool()
def remove_permission(account: str, file_id: str, permission_id: str) -> str:
    """
    Remove one sharing permission from a file or folder (unshare).
    Get permission IDs from get_file_permissions.

    Args:
        account: Which Google account to use.
        file_id: The file or folder ID.
        permission_id: The permission to remove.
    """
    _drive(account).permissions().delete(fileId=file_id, permissionId=permission_id, supportsAllDrives=True).execute()
    return f"Permission {permission_id} removed from {file_id}."


@mcp.tool()
def delete_file(account: str, file_id: str, permanent: bool = False) -> str:
    """
    Trash (default) or permanently delete a file or folder.

    Args:
        account: Which Google account to use.
        file_id: The file or folder ID.
        permanent: True = delete forever (cannot be undone). False = move to Trash.
    """
    drive = _drive(account)
    if permanent:
        drive.files().delete(fileId=file_id, supportsAllDrives=True).execute()
        return f"File {file_id} permanently deleted."
    drive.files().update(fileId=file_id, body={"trashed": True}, supportsAllDrives=True).execute()
    return f"File {file_id} moved to Trash."


# ===========================================================================
# Gmail tools
# ===========================================================================
def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _strip_html(s: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", s)).strip()


def _body_text(payload: dict) -> str:
    """Prefer text/plain; fall back to stripped text/html; walk multipart parts."""
    plain, html_ = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            html_.append(_decode(data))
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    if html_:
        return _strip_html("\n".join(html_))
    return ""


def _attachments(payload: dict) -> list[dict]:
    out = []

    def walk(part: dict) -> None:
        if part.get("filename"):
            out.append(
                {
                    "filename": part["filename"],
                    "mimeType": part.get("mimeType", ""),
                    "size": part.get("body", {}).get("size", 0),
                    "attachmentId": part.get("body", {}).get("attachmentId", ""),
                }
            )
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload)
    return out


def _message_summary(m: dict) -> dict:
    headers = m.get("payload", {}).get("headers", [])
    return {
        "id": m["id"],
        "threadId": m.get("threadId", ""),
        "date": _header(headers, "Date"),
        "from": _header(headers, "From"),
        "to": _header(headers, "To"),
        "cc": _header(headers, "Cc"),
        "subject": _header(headers, "Subject"),
        "snippet": m.get("snippet", ""),
        "labels": m.get("labelIds", []),
    }


@mcp.tool()
def search_emails(account: str, query: str = "", max_results: int = 20, page_token: str = "") -> dict:
    """
    Search Gmail with normal Gmail search syntax
    (e.g. "from:bank newer_than:30d", "has:attachment subject:closing", "label:inbox is:unread").

    Args:
        account: Which Google account to use.
        query: Gmail search string (empty = most recent mail).
        max_results: Page size (default 20, max 100).
        page_token: Previous result's nextPageToken.

    Returns {"messages": [summary...], "nextPageToken": "..."}. Summaries have
    id, threadId, date, from, to, subject, snippet, labels.
    """
    gmail = _gmail(account)
    resp = gmail.users().messages().list(userId="me", q=query or None, maxResults=min(max_results, 100), pageToken=page_token or None).execute()
    out = []
    for ref in resp.get("messages", []):
        m = gmail.users().messages().get(userId="me", id=ref["id"], format="metadata", metadataHeaders=["From", "To", "Cc", "Subject", "Date"]).execute()
        out.append(_message_summary(m))
    return {"messages": out, "nextPageToken": resp.get("nextPageToken", "")}


@mcp.tool()
def read_email(account: str, message_id: str, max_chars: int = 40000) -> dict:
    """
    Read one email in full: headers, body text, attachment list.

    Args:
        account: Which Google account to use.
        message_id: The message ID (from search_emails or read_thread).
        max_chars: Truncate the body after this many characters.
    """
    m = _gmail(account).users().messages().get(userId="me", id=message_id, format="full").execute()
    s = _message_summary(m)
    body = _body_text(m.get("payload", {}))
    s["body"] = body[:max_chars]
    s["truncated"] = len(body) > max_chars
    s["attachments"] = _attachments(m.get("payload", {}))
    return s


@mcp.tool()
def read_thread(account: str, thread_id: str, max_chars_per_message: int = 8000) -> dict:
    """
    Read a whole conversation thread, oldest message first.

    Args:
        account: Which Google account to use.
        thread_id: The thread ID.
        max_chars_per_message: Truncate each body after this many characters.
    """
    t = _gmail(account).users().threads().get(userId="me", id=thread_id, format="full").execute()
    msgs = []
    for m in t.get("messages", []):
        s = _message_summary(m)
        body = _body_text(m.get("payload", {}))
        s["body"] = body[:max_chars_per_message]
        s["truncated"] = len(body) > max_chars_per_message
        s["attachments"] = _attachments(m.get("payload", {}))
        msgs.append(s)
    return {"threadId": thread_id, "messageCount": len(msgs), "messages": msgs}


@mcp.tool()
def list_labels(account: str) -> list[dict]:
    """
    List Gmail labels (folders) for the account, with unread counts for user labels.

    Args:
        account: Which Google account to use.
    """
    resp = _gmail(account).users().labels().list(userId="me").execute()
    return [{"id": l["id"], "name": l["name"], "type": l.get("type", "")} for l in resp.get("labels", [])]


@mcp.tool()
def modify_labels(account: str, message_id: str, add: list[str] | None = None, remove: list[str] | None = None) -> dict:
    """
    Add and/or remove labels on a message. Use label IDs from list_labels, or the
    system names INBOX, UNREAD, STARRED, IMPORTANT, SPAM, TRASH.
    Archive = remove ["INBOX"]; mark read = remove ["UNREAD"].

    Args:
        account: Which Google account to use.
        message_id: The message ID.
        add: Label IDs to add.
        remove: Label IDs to remove.
    """
    r = (
        _gmail(account)
        .users()
        .messages()
        .modify(userId="me", id=message_id, body={"addLabelIds": add or [], "removeLabelIds": remove or []})
        .execute()
    )
    return {"id": r["id"], "labels": r.get("labelIds", [])}


def _build_mime(to: str, subject: str, body: str, cc: str = "", bcc: str = "", in_reply_to: str = "", references: str = "") -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


@mcp.tool()
def create_draft(account: str, to: str, subject: str, body: str, cc: str = "", bcc: str = "", reply_to_message_id: str = "") -> dict:
    """
    Create a Gmail draft (nothing is sent). To draft a reply, pass the message
    being replied to in reply_to_message_id and the draft joins that thread.

    Args:
        account: Which Google account to use.
        to: Recipient(s), comma separated.
        subject: Subject line.
        body: Plain-text body.
        cc: Optional Cc.
        bcc: Optional Bcc.
        reply_to_message_id: Optional message ID to reply to.
    """
    gmail = _gmail(account)
    thread_id, in_reply_to, references = "", "", ""
    if reply_to_message_id:
        orig = gmail.users().messages().get(userId="me", id=reply_to_message_id, format="metadata", metadataHeaders=["Message-ID", "References", "Subject"]).execute()
        h = orig.get("payload", {}).get("headers", [])
        thread_id = orig.get("threadId", "")
        in_reply_to = _header(h, "Message-ID")
        references = (_header(h, "References") + " " + in_reply_to).strip()
        if not subject:
            subject = _header(h, "Subject")
            if not subject.lower().startswith("re:"):
                subject = "Re: " + subject
    raw = _build_mime(to, subject, body, cc, bcc, in_reply_to, references)
    message: dict[str, Any] = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    d = gmail.users().drafts().create(userId="me", body={"message": message}).execute()
    return {"draftId": d["id"], "messageId": d.get("message", {}).get("id", ""), "threadId": d.get("message", {}).get("threadId", "")}


@mcp.tool()
def send_email(account: str, to: str, subject: str, body: str, cc: str = "", bcc: str = "", reply_to_message_id: str = "") -> dict:
    """
    Send an email immediately. Only call this after the user has explicitly
    approved the exact recipient, subject and body; prefer create_draft otherwise.

    Args:
        account: Which Google account to use.
        to: Recipient(s), comma separated.
        subject: Subject line.
        body: Plain-text body.
        cc: Optional Cc.
        bcc: Optional Bcc.
        reply_to_message_id: Optional message ID to reply to (keeps the thread).
    """
    gmail = _gmail(account)
    thread_id, in_reply_to, references = "", "", ""
    if reply_to_message_id:
        orig = gmail.users().messages().get(userId="me", id=reply_to_message_id, format="metadata", metadataHeaders=["Message-ID", "References", "Subject"]).execute()
        h = orig.get("payload", {}).get("headers", [])
        thread_id = orig.get("threadId", "")
        in_reply_to = _header(h, "Message-ID")
        references = (_header(h, "References") + " " + in_reply_to).strip()
        if not subject:
            subject = _header(h, "Subject")
            if not subject.lower().startswith("re:"):
                subject = "Re: " + subject
    raw = _build_mime(to, subject, body, cc, bcc, in_reply_to, references)
    message: dict[str, Any] = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    r = gmail.users().messages().send(userId="me", body=message).execute()
    return {"messageId": r["id"], "threadId": r.get("threadId", "")}


@mcp.tool()
def send_draft(account: str, draft_id: str) -> dict:
    """
    Send an existing draft. Only after the user has approved it.

    Args:
        account: Which Google account to use.
        draft_id: The draft ID from create_draft.
    """
    r = _gmail(account).users().drafts().send(userId="me", body={"id": draft_id}).execute()
    return {"messageId": r["id"], "threadId": r.get("threadId", "")}


# ===========================================================================
# Start
# ===========================================================================
if __name__ == "__main__":
    print(f"Accounts configured: {sorted(ACCOUNTS) or 'NONE'}")
    print(f"MCP endpoint path: {MCP_PATH}")
    mcp.run(transport="streamable-http")
