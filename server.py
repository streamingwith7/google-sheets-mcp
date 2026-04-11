"""
Google Sheets & Drive MCP Server
=================================
A remote MCP (Model Context Protocol) server that wraps the Google Sheets
and Google Drive APIs. It lets Claude read, write, search, and manage
spreadsheets AND files/folders in your Google Drive.

Transport: Streamable HTTP (so it works as a Claude remote MCP connector).
Auth:      Uses a long-lived Google OAuth 2.0 refresh token from env vars.
"""

import os

# ---------------------------------------------------------------------------
# Google API client setup
# ---------------------------------------------------------------------------
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# MCP SDK imports
# ---------------------------------------------------------------------------
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Read Google OAuth credentials from environment variables.
# These are set once on the server (e.g. in Render's dashboard).
# ---------------------------------------------------------------------------
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")

# The scopes we need:
#   - spreadsheets: read and write spreadsheet data
#   - drive: list and create spreadsheet files
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _build_credentials():
    """
    Create Google OAuth 2.0 credentials from the stored refresh token.

    The Google library automatically refreshes the access token when needed,
    so the server stays authenticated as long as the refresh token is valid.
    """
    return Credentials(
        token=None,  # will be fetched automatically on first request
        refresh_token=GOOGLE_REFRESH_TOKEN,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
    )


def _get_sheets_service():
    """
    Build and return an authenticated Google Sheets API v4 client.
    Used for reading/writing cell data and managing tabs.
    """
    credentials = _build_credentials()
    return build("sheets", "v4", credentials=credentials)


def _get_drive_service():
    """
    Build and return an authenticated Google Drive API v3 client.
    Used for listing and searching spreadsheet files.
    """
    credentials = _build_credentials()
    return build("drive", "v3", credentials=credentials)


# ---------------------------------------------------------------------------
# Create the MCP server
# ---------------------------------------------------------------------------
# The `name` shows up in Claude's UI when the connector is registered.
# Use the PORT environment variable (Render sets this automatically).
port = int(os.environ.get("PORT", 8000))

mcp = FastMCP(
    name="Google Sheets & Drive",
    instructions=(
        "Manage Google Sheets and Google Drive. You can list/create/read/write "
        "spreadsheets, manage tabs, AND list/create/move/rename/delete files "
        "and folders in Google Drive."
    ),
    host="0.0.0.0",
    port=port,
    # Stateless mode: each request is independent (no session tracking).
    # This is required for remote MCP connectors like Claude, where
    # requests may come from different servers/IPs.
    stateless_http=True,
    # Return JSON responses instead of SSE streams.
    json_response=True,
)


# ===========================================================================
# Tool 1: list_spreadsheets
# ===========================================================================
@mcp.tool()
def list_spreadsheets(query: str = "", max_results: int = 20) -> list[dict]:
    """
    Search Google Drive for spreadsheets you have access to.

    Args:
        query: Optional search term to filter spreadsheets by name.
               Leave empty to list all spreadsheets.
        max_results: Maximum number of spreadsheets to return (default 20).

    Returns a list of spreadsheets, each with 'id', 'name', and 'modifiedTime'.
    """
    drive = _get_drive_service()

    # Always filter to only spreadsheet files
    q = "mimeType='application/vnd.google-apps.spreadsheet'"

    # If the user provided a search term, also filter by name
    if query:
        # Escape single quotes in the query to avoid breaking the filter
        safe_query = query.replace("'", "\\'")
        q += f" and name contains '{safe_query}'"

    # Request only the fields we need (saves bandwidth)
    response = (
        drive.files()
        .list(
            q=q,
            pageSize=max_results,
            fields="files(id, name, modifiedTime)",
            orderBy="modifiedTime desc",
        )
        .execute()
    )

    # Build a clean list of results
    results = []
    for f in response.get("files", []):
        results.append(
            {
                "id": f["id"],
                "name": f["name"],
                "modifiedTime": f.get("modifiedTime", ""),
            }
        )

    return results


# ===========================================================================
# Tool 2: create_spreadsheet
# ===========================================================================
@mcp.tool()
def create_spreadsheet(title: str) -> dict:
    """
    Create a new blank Google Spreadsheet.

    Args:
        title: The name for the new spreadsheet.

    Returns the new spreadsheet's id, title, and URL.
    """
    sheets = _get_sheets_service()

    # The Sheets API can create a spreadsheet directly
    body = {"properties": {"title": title}}
    result = sheets.spreadsheets().create(body=body).execute()

    return {
        "id": result["spreadsheetId"],
        "title": result["properties"]["title"],
        "url": result["spreadsheetUrl"],
    }


# ===========================================================================
# Tool 3: get_spreadsheet_info
# ===========================================================================
@mcp.tool()
def get_spreadsheet_info(spreadsheet_id: str) -> dict:
    """
    Get information about a spreadsheet, including all its tabs (sheets).

    Args:
        spreadsheet_id: The ID of the spreadsheet (from the URL or list_spreadsheets).

    Returns the spreadsheet title and a list of tabs with their names and IDs.
    """
    sheets = _get_sheets_service()

    # Fetch spreadsheet metadata (but not the cell data)
    result = (
        sheets.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="properties.title,sheets.properties")
        .execute()
    )

    # Extract tab info from the response
    tabs = []
    for sheet in result.get("sheets", []):
        props = sheet.get("properties", {})
        tabs.append(
            {
                "sheetId": props.get("sheetId"),
                "title": props.get("title", ""),
                "index": props.get("index", 0),
                "rowCount": props.get("gridProperties", {}).get("rowCount", 0),
                "columnCount": props.get("gridProperties", {}).get("columnCount", 0),
            }
        )

    return {
        "title": result.get("properties", {}).get("title", ""),
        "spreadsheetId": spreadsheet_id,
        "tabs": tabs,
    }


# ===========================================================================
# Tool 4: add_tab
# ===========================================================================
@mcp.tool()
def add_tab(spreadsheet_id: str, tab_name: str) -> dict:
    """
    Add a new tab (sheet) to an existing spreadsheet.

    Args:
        spreadsheet_id: The ID of the spreadsheet.
        tab_name: The name for the new tab.

    Returns the new tab's sheetId and title.
    """
    sheets = _get_sheets_service()

    # Use batchUpdate with an AddSheet request
    body = {
        "requests": [
            {
                "addSheet": {
                    "properties": {
                        "title": tab_name,
                    }
                }
            }
        ]
    }

    result = (
        sheets.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body=body)
        .execute()
    )

    # The response includes the properties of the newly created sheet
    new_sheet = result["replies"][0]["addSheet"]["properties"]

    return {
        "sheetId": new_sheet["sheetId"],
        "title": new_sheet["title"],
    }


# ===========================================================================
# Tool 5: read_sheet
# ===========================================================================
@mcp.tool()
def read_sheet(spreadsheet_id: str, range: str = "Sheet1") -> list[list[str]]:
    """
    Read data from a sheet range.

    Args:
        spreadsheet_id: The ID of the spreadsheet.
        range: The A1 notation range to read. Examples:
               - "Sheet1" (all data in the first tab)
               - "Sheet1!A1:D10" (specific cell range)
               - "MyTab!A:F" (columns A through F in the "MyTab" tab)

    Returns a list of rows, where each row is a list of cell values (strings).
    """
    sheets = _get_sheets_service()

    # Use values().get() to read the cell data
    result = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=range)
        .execute()
    )

    # "values" contains a 2D list of cell data; may be missing if range is empty
    rows = result.get("values", [])

    return rows


# ===========================================================================
# Tool 6: write_sheet
# ===========================================================================
@mcp.tool()
def write_sheet(
    spreadsheet_id: str,
    range: str,
    values: list[list[str]],
    mode: str = "overwrite",
) -> dict:
    """
    Write data to a sheet range.

    Args:
        spreadsheet_id: The ID of the spreadsheet.
        range: The A1 notation range to write to (e.g. "Sheet1!A1", "MyTab!A1:D5").
        values: A list of rows, where each row is a list of cell values.
                Example: [["Name", "Age"], ["Alice", "30"], ["Bob", "25"]]
        mode: How to write the data:
              - "overwrite" (default): Replace existing data in the range.
              - "append": Add new rows after the last row with data.

    Returns info about how many cells were updated.
    """
    sheets = _get_sheets_service()

    # The body is the same for both modes
    body = {"values": values}

    if mode == "append":
        # append() adds rows after the last row that has data
        result = (
            sheets.spreadsheets()
            .values()
            .append(
                spreadsheetId=spreadsheet_id,
                range=range,
                valueInputOption="USER_ENTERED",  # Lets Google parse numbers, dates, etc.
                insertDataOption="INSERT_ROWS",
                body=body,
            )
            .execute()
        )
        updates = result.get("updates", {})
        return {
            "updatedRange": updates.get("updatedRange", ""),
            "updatedRows": updates.get("updatedRows", 0),
            "updatedColumns": updates.get("updatedColumns", 0),
            "updatedCells": updates.get("updatedCells", 0),
        }
    else:
        # update() overwrites (PUT) the specified range
        result = (
            sheets.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=range,
                valueInputOption="USER_ENTERED",  # Lets Google parse numbers, dates, etc.
                body=body,
            )
            .execute()
        )
        return {
            "updatedRange": result.get("updatedRange", ""),
            "updatedRows": result.get("updatedRows", 0),
            "updatedColumns": result.get("updatedColumns", 0),
            "updatedCells": result.get("updatedCells", 0),
        }


# ===========================================================================
# Tool 7: search_sheet
# ===========================================================================
@mcp.tool()
def search_sheet(
    spreadsheet_id: str,
    tab_name: str = "Sheet1",
    query: str = "",
) -> list[dict]:
    """
    Search for rows containing a specific text in a sheet tab.

    Reads all data from the tab and returns rows where any cell
    contains the query string (case-insensitive match).

    Args:
        spreadsheet_id: The ID of the spreadsheet.
        tab_name: The name of the tab to search in (default "Sheet1").
        query: The text to search for. If empty, returns all rows.

    Returns a list of matching rows, each with 'row_number' (1-based) and 'values'.
    """
    sheets = _get_sheets_service()

    # Read all data from the specified tab
    result = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=tab_name)
        .execute()
    )

    rows = result.get("values", [])
    query_lower = query.lower()

    matches = []
    for i, row in enumerate(rows):
        # Row numbers in spreadsheets are 1-based
        row_number = i + 1

        # If no query, return all rows
        if not query:
            matches.append({"row_number": row_number, "values": row})
            continue

        # Check if any cell in the row contains the query (case-insensitive)
        for cell in row:
            if query_lower in str(cell).lower():
                matches.append({"row_number": row_number, "values": row})
                break  # found a match in this row, move to the next one

    return matches


# ===========================================================================
# Tool 8: delete_tab
# ===========================================================================
@mcp.tool()
def delete_tab(spreadsheet_id: str, tab_name: str) -> str:
    """
    Delete a tab (sheet) from a spreadsheet.

    Args:
        spreadsheet_id: The ID of the spreadsheet.
        tab_name: The name of the tab to delete.

    Note: You cannot delete the last remaining tab in a spreadsheet.
    """
    sheets = _get_sheets_service()

    # First, we need to find the numeric sheetId for this tab name.
    # The Sheets API requires the sheetId (an integer), not the tab name.
    metadata = (
        sheets.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties")
        .execute()
    )

    # Search through all tabs to find the one with the matching name
    sheet_id = None
    for sheet in metadata.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == tab_name:
            sheet_id = props.get("sheetId")
            break

    if sheet_id is None:
        return f"Error: No tab named '{tab_name}' found in this spreadsheet."

    # Now delete the tab using batchUpdate with a DeleteSheet request
    body = {
        "requests": [
            {
                "deleteSheet": {
                    "sheetId": sheet_id,
                }
            }
        ]
    }

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body=body
    ).execute()

    return f"Tab '{tab_name}' deleted successfully."


# ===========================================================================
# GOOGLE DRIVE TOOLS
# ===========================================================================


# ===========================================================================
# Tool 9: list_files
# ===========================================================================
@mcp.tool()
def list_files(
    folder_id: str = "root",
    query: str = "",
    file_type: str = "",
    max_results: int = 20,
) -> list[dict]:
    """
    List files and folders in Google Drive.

    Args:
        folder_id: The folder to list. Use "root" for the top-level Drive.
                   Use a folder ID to list contents of a specific folder.
        query: Optional search term to filter by file name.
        file_type: Optional filter: "folder", "spreadsheet", "document",
                   "pdf", "image", or "" for all types.
        max_results: Maximum number of results to return (default 20).

    Returns a list of files with 'id', 'name', 'mimeType', and 'modifiedTime'.
    """
    drive = _get_drive_service()

    # Build the search query
    q_parts = [f"'{folder_id}' in parents", "trashed = false"]

    if query:
        safe_query = query.replace("'", "\\'")
        q_parts.append(f"name contains '{safe_query}'")

    # Map friendly type names to MIME types
    type_map = {
        "folder": "application/vnd.google-apps.folder",
        "spreadsheet": "application/vnd.google-apps.spreadsheet",
        "document": "application/vnd.google-apps.document",
        "pdf": "application/pdf",
        "image": "image/",
    }
    if file_type and file_type in type_map:
        mime = type_map[file_type]
        if file_type == "image":
            q_parts.append(f"mimeType contains '{mime}'")
        else:
            q_parts.append(f"mimeType = '{mime}'")

    q = " and ".join(q_parts)

    response = (
        drive.files()
        .list(
            q=q,
            pageSize=max_results,
            fields="files(id, name, mimeType, modifiedTime, size)",
            orderBy="modifiedTime desc",
        )
        .execute()
    )

    results = []
    for f in response.get("files", []):
        results.append(
            {
                "id": f["id"],
                "name": f["name"],
                "mimeType": f.get("mimeType", ""),
                "modifiedTime": f.get("modifiedTime", ""),
                "size": f.get("size", ""),
            }
        )

    return results


# ===========================================================================
# Tool 10: search_drive
# ===========================================================================
@mcp.tool()
def search_drive(query: str, max_results: int = 20) -> list[dict]:
    """
    Search across your entire Google Drive for files and folders by name.

    Unlike list_files, this searches EVERYWHERE — not just one folder.

    Args:
        query: The search term to look for in file names.
        max_results: Maximum number of results (default 20).

    Returns a list of matching files with 'id', 'name', 'mimeType', and 'modifiedTime'.
    """
    drive = _get_drive_service()

    safe_query = query.replace("'", "\\'")
    q = f"name contains '{safe_query}' and trashed = false"

    response = (
        drive.files()
        .list(
            q=q,
            pageSize=max_results,
            fields="files(id, name, mimeType, modifiedTime, parents)",
            orderBy="modifiedTime desc",
        )
        .execute()
    )

    results = []
    for f in response.get("files", []):
        results.append(
            {
                "id": f["id"],
                "name": f["name"],
                "mimeType": f.get("mimeType", ""),
                "modifiedTime": f.get("modifiedTime", ""),
                "parents": f.get("parents", []),
            }
        )

    return results


# ===========================================================================
# Tool 11: create_folder
# ===========================================================================
@mcp.tool()
def create_folder(name: str, parent_folder_id: str = "root") -> dict:
    """
    Create a new folder in Google Drive.

    Args:
        name: The name for the new folder.
        parent_folder_id: Where to create it. Use "root" for the top-level
                          of your Drive, or a folder ID to nest it inside
                          an existing folder.

    Returns the new folder's id and name.
    """
    drive = _get_drive_service()

    body = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_folder_id],
    }

    result = drive.files().create(body=body, fields="id, name").execute()

    return {"id": result["id"], "name": result["name"]}


# ===========================================================================
# Tool 12: delete_file
# ===========================================================================
@mcp.tool()
def delete_file(file_id: str, permanent: bool = False) -> str:
    """
    Delete a file or folder from Google Drive.

    Args:
        file_id: The ID of the file or folder to delete.
        permanent: If False (default), moves to Trash (recoverable).
                   If True, permanently deletes (cannot be undone!).

    Works for any file type: documents, spreadsheets, folders, PDFs, etc.
    """
    drive = _get_drive_service()

    if permanent:
        drive.files().delete(fileId=file_id).execute()
        return f"File {file_id} permanently deleted."
    else:
        # Move to trash by updating the 'trashed' property
        drive.files().update(fileId=file_id, body={"trashed": True}).execute()
        return f"File {file_id} moved to Trash."


# ===========================================================================
# Tool 13: rename_file
# ===========================================================================
@mcp.tool()
def rename_file(file_id: str, new_name: str) -> dict:
    """
    Rename a file or folder in Google Drive.

    Args:
        file_id: The ID of the file or folder to rename.
        new_name: The new name.

    Returns the updated file info.
    """
    drive = _get_drive_service()

    result = (
        drive.files()
        .update(fileId=file_id, body={"name": new_name}, fields="id, name")
        .execute()
    )

    return {"id": result["id"], "name": result["name"]}


# ===========================================================================
# Tool 14: move_file
# ===========================================================================
@mcp.tool()
def move_file(file_id: str, new_folder_id: str) -> dict:
    """
    Move a file or folder to a different folder in Google Drive.

    Args:
        file_id: The ID of the file/folder to move.
        new_folder_id: The ID of the destination folder.

    Returns the updated file info with its new parent folder.
    """
    drive = _get_drive_service()

    # First, get the current parent(s) so we can remove them
    file_info = (
        drive.files().get(fileId=file_id, fields="parents").execute()
    )
    current_parents = ",".join(file_info.get("parents", []))

    # Move by adding new parent and removing old parent(s)
    result = (
        drive.files()
        .update(
            fileId=file_id,
            addParents=new_folder_id,
            removeParents=current_parents,
            fields="id, name, parents",
        )
        .execute()
    )

    return {
        "id": result["id"],
        "name": result["name"],
        "parents": result.get("parents", []),
    }


# ===========================================================================
# Tool 15: get_file_info
# ===========================================================================
@mcp.tool()
def get_file_info(file_id: str) -> dict:
    """
    Get detailed information about a file or folder in Google Drive.

    Args:
        file_id: The ID of the file or folder.

    Returns name, type, size, creation date, modification date, owner, and URL.
    """
    drive = _get_drive_service()

    result = (
        drive.files()
        .get(
            fileId=file_id,
            fields="id, name, mimeType, size, createdTime, modifiedTime, owners, webViewLink, parents",
        )
        .execute()
    )

    owners = [o.get("displayName", o.get("emailAddress", "")) for o in result.get("owners", [])]

    return {
        "id": result["id"],
        "name": result["name"],
        "mimeType": result.get("mimeType", ""),
        "size": result.get("size", "N/A"),
        "createdTime": result.get("createdTime", ""),
        "modifiedTime": result.get("modifiedTime", ""),
        "owners": owners,
        "webViewLink": result.get("webViewLink", ""),
        "parents": result.get("parents", []),
    }


# ===========================================================================
# Tool 16: copy_file
# ===========================================================================
@mcp.tool()
def copy_file(file_id: str, new_name: str = "", destination_folder_id: str = "") -> dict:
    """
    Make a copy of a file in Google Drive.

    Args:
        file_id: The ID of the file to copy.
        new_name: Optional new name for the copy. If empty, Google names it "Copy of ...".
        destination_folder_id: Optional folder to place the copy in.
                               If empty, copies to the same folder as the original.

    Returns the new copy's id, name, and URL.
    """
    drive = _get_drive_service()

    body = {}
    if new_name:
        body["name"] = new_name
    if destination_folder_id:
        body["parents"] = [destination_folder_id]

    result = (
        drive.files()
        .copy(fileId=file_id, body=body, fields="id, name, webViewLink")
        .execute()
    )

    return {
        "id": result["id"],
        "name": result["name"],
        "webViewLink": result.get("webViewLink", ""),
    }


# ===========================================================================
# Start the server
# ===========================================================================
if __name__ == "__main__":
    # Run with streamable-http transport so Claude can connect remotely.
    mcp.run(transport="streamable-http")
