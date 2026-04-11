"""
Google Sheets MCP Server
========================
A remote MCP (Model Context Protocol) server that wraps the Google Sheets
and Google Drive APIs. It lets Claude read, write, search, and manage
spreadsheets on your behalf.

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
    name="Google Sheets",
    instructions=(
        "Manage Google Sheets spreadsheets. You can list spreadsheets, "
        "create new ones, read and write data, search for rows, and "
        "manage tabs (sheets) within a spreadsheet."
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
# Start the server
# ===========================================================================
if __name__ == "__main__":
    # Run with streamable-http transport so Claude can connect remotely.
    mcp.run(transport="streamable-http")
