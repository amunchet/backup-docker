#!/usr/bin/env python3
"""
Dropbox MCP server: read-only access to a Dropbox account, including shared
folders, with writes allowed only under the paths listed in WRITABLE_PATHS.

Runs behind caddy-auth0 (https://github.com/amunchet/caddy-auth0), which
authenticates the caller and passes their permissions in X-Auth-Permissions.
Writes additionally need the WRITE_PERMISSION permission.

Serves Streamable HTTP at /mcp on port 8000.
"""
import base64
import functools
import mimetypes
import os
import posixpath
from typing import Literal

import dropbox
from dotenv import load_dotenv
from dropbox.common import PathRoot
from dropbox.files import FileMetadata, FolderMetadata, SearchOptions, WriteMode
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image
from mcp_types import ToolAnnotations

load_dotenv()

APP_KEY = os.getenv("APP_KEY")
APP_SECRET = os.getenv("APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
# Use the team's root namespace, so team folders appear at the top level.
DROPBOX_TEAM_SPACE = os.getenv("DROPBOX_TEAM_SPACE", "").lower() in ("1", "true", "yes")
# Comma-separated Dropbox folders (or files) that may be written to.
WRITABLE_PATHS = [p.strip() for p in os.getenv("WRITABLE_PATHS", "").split(",") if p.strip()]
# Permission the caller needs (from caddy-auth0) to write.
WRITE_PERMISSION = os.getenv("WRITE_PERMISSION") or "dropbox:write"
MAX_READ_BYTES = int(os.getenv("MAX_READ_BYTES") or 10 * 1024 * 1024)
LIST_LIMIT = int(os.getenv("LIST_LIMIT") or 2000)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

mcp = MCPServer(
    "dropbox",
    instructions=(
        "Read files in the user's Dropbox, including shared folders. Paths look like "
        "'/Folder/file.txt'; '' is the root. Team folders and shared folders that aren't in "
        "the user's Dropbox are reached as 'ns:<shared_folder_id>/...' (see list_shared_folders). "
        "Everything is read-only except the folders listed by account_info."
    ),
)


@functools.cache
def dbx(namespace: str = "") -> dropbox.Dropbox:
    """The Dropbox client, created on first use; with `namespace`, one rooted at that shared or team folder."""
    if namespace:
        return dbx().with_path_root(PathRoot.namespace_id(namespace))
    if not (APP_KEY and APP_SECRET and DROPBOX_REFRESH_TOKEN):
        raise ToolError("Server misconfigured: set APP_KEY, APP_SECRET and DROPBOX_REFRESH_TOKEN.")
    client = dropbox.Dropbox(oauth2_refresh_token=DROPBOX_REFRESH_TOKEN, app_key=APP_KEY, app_secret=APP_SECRET)
    if DROPBOX_TEAM_SPACE:
        root = client.users_get_current_account().root_info.root_namespace_id
        client = client.with_path_root(PathRoot.root(root))
    return client


def clean(path: str) -> str:
    """Normalize a Dropbox path ('' for root, '/a/b', 'ns:1/a', 'id:x'), refusing '..'."""
    path = (path or "").strip()
    if path.startswith(("id:", "rev:")):
        return path
    prefix = ""
    if path.startswith("ns:"):
        prefix, _, path = path.partition("/")
    if ".." in path.split("/"):
        raise ToolError(f"'..' is not allowed in paths: {path}")
    path = posixpath.normpath("/" + path.lstrip("/"))
    return prefix + ("" if path == "/" else path)


def locate(path: str):
    """
    (client, path within its root, prefix for the paths it returns) for a clean path. Calls only
    reach inside the folder they're rooted at, so a shared or team folder outside it (ns:<id>/...)
    is reached by rooting calls at that folder, and the paths that come back are relative to it.
    """
    if not path.startswith("ns:"):
        return dbx(), path, ""
    prefix, _, rest = path.partition("/")
    return dbx(prefix[3:]), "/" + rest if rest else "", prefix


def is_writable(path: str) -> bool:
    """True if the path is inside one of WRITABLE_PATHS (case-insensitive, like Dropbox)."""
    path = clean(path).lower()
    return any(path == w or path.startswith(w + "/") for w in (clean(w).lower() for w in WRITABLE_PATHS))


def describe(entry, prefix: str = "", fallback_path: str | None = None) -> dict:
    """The parts of Dropbox metadata worth showing a model, with `prefix` from locate() on its path."""
    path = prefix + entry.path_display if entry.path_display else fallback_path or entry.id
    out = {"name": entry.name, "path": path, "id": entry.id}
    if isinstance(entry, FileMetadata):
        out.update(type="file", size=entry.size, modified=entry.server_modified.isoformat() + "Z")
    elif isinstance(entry, FolderMetadata):
        out["type"] = "folder"
        if entry.shared_folder_id:
            out["shared_folder_id"] = entry.shared_folder_id
    out["writable"] = is_writable(path)
    return out


def caller(ctx: Context) -> dict:
    """The user caddy-auth0 authenticated, from the X-Auth-* headers."""
    headers = ctx.headers or {}
    return {
        "user": headers.get("x-auth-user"),
        "email": headers.get("x-auth-email"),
        "permissions": [p for p in headers.get("x-auth-permissions", "").split(",") if p],
    }


@mcp.tool(annotations=READ_ONLY)
def account_info(ctx: Context) -> dict:
    """Show the Dropbox account, who is calling, and which paths are writable."""
    account = dbx().users_get_current_account()
    who = caller(ctx)
    return {
        "dropbox_account": {"name": account.name.display_name, "email": account.email},
        "team_space": DROPBOX_TEAM_SPACE,
        "caller": who,
        "writable_paths": WRITABLE_PATHS,
        "caller_can_write": bool(WRITABLE_PATHS) and WRITE_PERMISSION in who["permissions"],
    }


@mcp.tool(annotations=READ_ONLY)
def list_folder(path: str = "", recursive: bool = False) -> dict:
    """List a folder's files and subfolders. Use recursive=True to include everything below it."""
    path = clean(path)
    client, inner, prefix = locate(path)
    result = client.files_list_folder(inner, recursive=recursive, limit=min(LIST_LIMIT, 2000))
    entries = list(result.entries)
    while result.has_more and len(entries) < LIST_LIMIT:
        result = client.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)
    return {
        "path": path,
        "entries": [describe(e, prefix, None if recursive else f"{path}/{e.name}") for e in entries[:LIST_LIMIT]],
        "truncated": len(entries) > LIST_LIMIT or result.has_more,
    }


@mcp.tool(annotations=READ_ONLY)
def search(query: str, path: str = "", max_results: int = 50) -> list[dict]:
    """Search file and folder names and contents, optionally only under `path`."""
    client, inner, prefix = locate(clean(path))
    options = SearchOptions(path=inner or None, max_results=max(1, min(max_results, 1000)))
    result = client.files_search_v2(query, options=options)
    return [describe(m.metadata.get_metadata(), prefix) for m in result.matches]


@mcp.tool(annotations=READ_ONLY)
def get_metadata(path: str) -> dict:
    """Show details of one file or folder."""
    path = clean(path)
    client, inner, prefix = locate(path)
    return describe(client.files_get_metadata(inner), prefix, path)


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def read_file(path: str, encoding: Literal["auto", "base64"] = "auto") -> str | Image:
    """
    Read a file. encoding="auto" returns text, or the image itself for images;
    "base64" returns any file's bytes base64-encoded.
    """
    client, inner, _ = locate(clean(path))
    meta, response = client.files_download(inner)
    if meta.size > MAX_READ_BYTES:
        response.close()
        raise ToolError(f"{meta.name} is {meta.size} bytes; the limit is {MAX_READ_BYTES}.")
    data = response.content
    if encoding == "base64":
        return base64.b64encode(data).decode()
    mime = mimetypes.guess_type(meta.name)[0] or ""
    if mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        return Image(data=data, format=mime.split("/")[1])
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{meta.name} is a binary file ({mime or 'unknown type'}); use encoding='base64'.")


@mcp.tool(annotations=READ_ONLY)
def list_shared_folders() -> list[dict]:
    """
    List every shared folder the account can access, including ones not added to
    its Dropbox. Use `path` from here to browse them.
    """
    folders = {}
    for list_first, list_next in (
        (dbx().sharing_list_folders, dbx().sharing_list_folders_continue),
        (dbx().sharing_list_mountable_folders, dbx().sharing_list_mountable_folders_continue),
    ):
        result = list_first()
        while True:
            for f in result.entries:
                folders[f.shared_folder_id] = f
            if not result.cursor:
                break
            result = list_next(result.cursor)
    out = []
    for f in folders.values():
        path = f.path_display or f"ns:{f.shared_folder_id}"
        out.append({
            "name": f.name,
            "path": path,
            "shared_folder_id": f.shared_folder_id,
            "in_my_dropbox": bool(f.path_lower),
            "access": f.access_type._tag,
            "team_folder": f.is_team_folder,
            "writable": is_writable(path),
        })
    return sorted(out, key=lambda f: f["name"].lower())


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
def write_file(path: str, content: str, ctx: Context, encoding: Literal["text", "base64"] = "text", overwrite: bool = False) -> dict:
    """
    Write a file, only inside the writable paths shown by account_info. Fails if
    the file exists unless overwrite=True. encoding is "text" or "base64".
    """
    path = clean(path)
    if not path.startswith(("/", "ns:")):
        raise ToolError("Give the file's full path, e.g. '/Shared/Team/notes.txt'.")
    if not is_writable(path):
        raise ToolError(f"{path} is read-only. Writable paths: {', '.join(WRITABLE_PATHS) or 'none'}.")
    if WRITE_PERMISSION not in caller(ctx)["permissions"]:
        raise ToolError(f"Writing needs the '{WRITE_PERMISSION}' permission.")
    data = base64.b64decode(content) if encoding == "base64" else content.encode("utf-8")
    mode = WriteMode.overwrite if overwrite else WriteMode.add
    client, inner, prefix = locate(path)
    return describe(client.files_upload(data, inner, mode=mode, autorename=False), prefix)


if __name__ == "__main__":  # pragma: no cover
    # Listen on all interfaces so Caddy can reach it (don't publish the port).
    mcp.run("streamable-http", host="0.0.0.0", port=8000)
