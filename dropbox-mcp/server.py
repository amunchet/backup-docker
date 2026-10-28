#!/usr/bin/env python3
"""
Dropbox MCP server: read-only access to a Dropbox account, including shared
folders, or to just one folder of it (DROPBOX_FOLDER), with writes allowed only
under the paths listed in WRITABLE_PATHS.

Runs behind caddy-auth0 (https://github.com/amunchet/caddy-auth0), which
authenticates the caller and passes their permissions in X-Auth-Permissions.
Writes additionally need the WRITE_PERMISSION permission.

Serves Streamable HTTP at /mcp on port 8000, statelessly.
"""
import base64
import codecs
import functools
import mimetypes
import os
import posixpath
from typing import Literal

import dropbox
from dotenv import load_dotenv
from dropbox.common import PathRoot
from dropbox.files import CommitInfo, FileMetadata, FolderMetadata, SearchOptions, UploadSessionCursor, WriteMode
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image
from mcp_types import ToolAnnotations

load_dotenv()

APP_KEY = os.getenv("APP_KEY")
APP_SECRET = os.getenv("APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
# The only folder clients can read, which is then their top level (''): a path, or ns:<id>/path
# in a team folder or shared folder. Unset means all of Dropbox.
DROPBOX_FOLDER = os.getenv("DROPBOX_FOLDER", "").strip().rstrip("/")
# Use the team's root namespace, so team folders appear at the top level.
DROPBOX_TEAM_SPACE = os.getenv("DROPBOX_TEAM_SPACE", "").lower() in ("1", "true", "yes")
# Comma-separated Dropbox folders (or files) that may be written to, inside DROPBOX_FOLDER if it's set.
WRITABLE_PATHS = [p.strip() for p in os.getenv("WRITABLE_PATHS", "").split(",") if p.strip()]
# Permission the caller needs (from caddy-auth0) to write.
WRITE_PERMISSION = os.getenv("WRITE_PERMISSION") or "dropbox:write"
# Most bytes one read_file call returns; bigger files are read in several calls.
MAX_READ_BYTES = int(os.getenv("MAX_READ_BYTES") or 10 * 1024 * 1024)
LIST_LIMIT = int(os.getenv("LIST_LIMIT") or 2000)
# Most bytes one write_file call carries; bigger files go up in several calls. Dropbox takes at most 150 MiB per request.
MAX_WRITE_BYTES = int(os.getenv("MAX_WRITE_BYTES") or 100 * 1024 * 1024)
if not 0 < MAX_WRITE_BYTES <= 150 * 1024 * 1024:
    raise SystemExit(f"MAX_WRITE_BYTES must be between 1 and {150 * 1024 * 1024} (150 MiB), not {MAX_WRITE_BYTES}.")

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

mcp = MCPServer(
    "dropbox",
    instructions=(
        "Read files in one folder of the user's Dropbox. Paths look like '/Folder/file.txt'; '' is "
        "the top of the folder. " if DROPBOX_FOLDER else
        "Read files in the user's Dropbox, including shared folders. Paths look like "
        "'/Folder/file.txt'; '' is the root. Team folders and shared folders that aren't in "
        "the user's Dropbox are reached as 'ns:<shared_folder_id>/...' (see list_shared_folders). "
    ) + "Everything is read-only except the folders listed by account_info.",
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
    (client, path within its root, prefix for the paths it returns) for a clean path. Every call
    goes through here, and with DROPBOX_FOLDER set, every path is inside it. Calls only reach
    inside the folder they're rooted at, so a shared or team folder outside it (ns:<id>/...)
    is reached by rooting calls at that folder, and the paths that come back are relative to it.
    """
    if DROPBOX_FOLDER:
        if path and not path.startswith("/"):  # ns:, id: and rev: paths could lead anywhere
            raise ToolError("Only paths inside this server's folder work, like '/Folder/file.txt'; '' is its top.")
        path = DROPBOX_FOLDER + path
    if not path.startswith("ns:"):
        return dbx(), path, ""
    prefix, _, rest = path.partition("/")
    return dbx(prefix[3:]), "/" + rest if rest else "", "" if DROPBOX_FOLDER else prefix


def writable_paths() -> list[str]:
    """
    WRITABLE_PATHS as clients' paths. With DROPBOX_FOLDER set, they're inside it, given either from
    it ('/Outbox') or in full ('ns:42/Outbox'), which then loses the folder's part.
    """
    paths = [clean(p) for p in WRITABLE_PATHS]
    if not DROPBOX_FOLDER:
        return paths
    folder = DROPBOX_FOLDER.lower()
    paths = [p[len(folder):] if (p.lower() + "/").startswith(folder + "/") else p for p in paths]
    outside = [p for p in paths if p and not p.startswith("/")]
    if outside:
        raise ToolError(f"Server misconfigured: WRITABLE_PATHS {', '.join(outside)} isn't inside DROPBOX_FOLDER {DROPBOX_FOLDER}.")
    return paths


def is_writable(path: str) -> bool:
    """True if the path is inside one of WRITABLE_PATHS (case-insensitive, like Dropbox)."""
    path = clean(path).lower()
    return any(path == w or path.startswith(w + "/") for w in (w.lower() for w in writable_paths()))


def describe(entry, prefix: str = "", fallback_path: str | None = None) -> dict:
    """The parts of Dropbox metadata worth showing a model, with `prefix` from locate() on its path."""
    path = fallback_path or entry.id
    if entry.path_display:
        # Clients' paths start at DROPBOX_FOLDER, so leave off the folders leading to it (one per '/' in it).
        parts = entry.path_display.split("/")[1 + DROPBOX_FOLDER.count("/"):]
        path = prefix + "".join("/" + part for part in parts)
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


def dropbox_errors(tool):
    """Pass Dropbox's errors on to clients; the MCP library hides any other error's text."""
    @functools.wraps(tool)
    def wrapper(*args, **kwargs):
        try:
            return tool(*args, **kwargs)
        except dropbox.exceptions.DropboxException as e:
            raise ToolError(f"Dropbox said: {getattr(e, 'error', None) or e}") from e
    return wrapper


@mcp.tool(annotations=READ_ONLY)
@dropbox_errors
def account_info(ctx: Context) -> dict:
    """Show the Dropbox account, the folder clients see as '', who is calling, and which paths are writable."""
    account = dbx().users_get_current_account()
    who = caller(ctx)
    return {
        "dropbox_account": {"name": account.name.display_name, "email": account.email},
        "folder": DROPBOX_FOLDER or "/",
        "team_space": DROPBOX_TEAM_SPACE,
        "caller": who,
        "writable_paths": writable_paths(),
        "caller_can_write": bool(WRITABLE_PATHS) and WRITE_PERMISSION in who["permissions"],
        "max_read_bytes": MAX_READ_BYTES,
        "max_write_bytes": MAX_WRITE_BYTES,
    }


@mcp.tool(annotations=READ_ONLY)
@dropbox_errors
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
@dropbox_errors
def search(query: str, path: str = "", max_results: int = 50) -> list[dict]:
    """Search file and folder names and contents, optionally only under `path`."""
    client, inner, prefix = locate(clean(path))
    options = SearchOptions(path=inner or None, max_results=max(1, min(max_results, 1000)))
    result = client.files_search_v2(query, options=options)
    return [describe(m.metadata.get_metadata(), prefix) for m in result.matches]


@mcp.tool(annotations=READ_ONLY)
@dropbox_errors
def get_metadata(path: str) -> dict:
    """Show details of one file or folder."""
    path = clean(path)
    client, inner, prefix = locate(path)
    return describe(client.files_get_metadata(inner), prefix, path)


@mcp.tool(annotations=READ_ONLY, structured_output=False)
@dropbox_errors
def read_file(path: str, encoding: Literal["auto", "base64"] = "auto", offset: int = 0, length: int = 0) -> str | Image | dict:
    """
    Read a file. encoding="auto" returns text, or the image itself for images;
    "base64" returns any file's bytes base64-encoded.
    A file bigger than max_read_bytes (see account_info) is read in pieces of up to
    that many bytes: each comes back with `content`, `size`, `next_offset` and
    `more`; call again with offset=next_offset until more is false. offset and
    length (0 means as much as allowed) read just part of any file.
    """
    client, inner, _ = locate(clean(path))
    meta, response = client.files_download(inner)
    if not offset and not length and meta.size <= MAX_READ_BYTES:
        return whole_file(meta, response.content, encoding)
    response.close()
    return file_piece(client, inner, meta, encoding, offset, min(length or MAX_READ_BYTES, MAX_READ_BYTES))


def whole_file(meta: FileMetadata, data: bytes, encoding: str) -> str | Image:
    """A whole file's bytes as read_file returns them."""
    if encoding == "base64":
        return base64.b64encode(data).decode()
    mime = mimetypes.guess_type(meta.name)[0] or ""
    if mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        return Image(data=data, format=mime.split("/")[1])
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{meta.name} is a binary file ({mime or 'unknown type'}); use encoding='base64'.")


def file_piece(client, inner: str, meta: FileMetadata, encoding: str, offset: int, length: int) -> dict:
    """`length` bytes of a file from `offset`, fetched alone with an HTTP range, so any size of file can be read."""
    if not 0 <= offset <= meta.size or length < 0:
        raise ToolError(f"{meta.name} is {meta.size} bytes; offset must be 0 to {meta.size} and length 0 or more.")
    data = b""
    if offset < meta.size:
        _, response = client.files_download(inner, extra_headers={"Range": f"bytes={offset}-{offset + length - 1}"})
        data = response.content
    if encoding == "base64":
        content = base64.b64encode(data).decode()
    else:
        # A piece can end inside a character; leave its first bytes to the next piece.
        try:
            content = codecs.getincrementaldecoder("utf-8")().decode(data, final=offset + len(data) >= meta.size)
        except UnicodeDecodeError:
            raise ToolError(f"{meta.name} isn't UTF-8 text at offset {offset}; use encoding='base64'.")
        data = content.encode("utf-8")
        if not data and offset < meta.size:
            raise ToolError(f"{length} bytes at offset {offset} of {meta.name} are less than one character; read more.")
    next_offset = offset + len(data)
    return {"content": content, "encoding": "base64" if encoding == "base64" else "text",
            "size": meta.size, "offset": offset, "next_offset": next_offset, "more": next_offset < meta.size}


@mcp.tool(annotations=READ_ONLY)
@dropbox_errors
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


# Limited to one folder, the names of all the others stay hidden too.
if DROPBOX_FOLDER:
    mcp.remove_tool("list_shared_folders")


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
@dropbox_errors
def write_file(path: str, content: str, ctx: Context, encoding: Literal["text", "base64"] = "text", overwrite: bool = False,
               more: bool = False, upload_id: str = "", offset: int = 0) -> dict:
    """
    Write a file, only inside the writable paths shown by account_info. Fails if
    the file exists unless overwrite=True. encoding is "text" or "base64".
    Each call carries at most max_write_bytes (see account_info). Write a bigger
    file in pieces, in order, each with the same path: the first with more=True,
    which returns an upload_id; the next ones with that upload_id, the offset it
    returned, and more=True; the last one with more=False, which saves the file.
    """
    path = clean(path)
    if not path.startswith(("/", "ns:")):
        raise ToolError("Give the file's full path, e.g. '/Shared/Team/notes.txt'.")
    if not is_writable(path):
        raise ToolError(f"{path} is read-only. Writable paths: {', '.join(writable_paths()) or 'none'}.")
    if WRITE_PERMISSION not in caller(ctx)["permissions"]:
        raise ToolError(f"Writing needs the '{WRITE_PERMISSION}' permission.")
    data = base64.b64decode(content) if encoding == "base64" else content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise ToolError(f"This piece is {len(data)} bytes; the limit is {MAX_WRITE_BYTES} per call. Send it in pieces with more=True.")
    mode = WriteMode.overwrite if overwrite else WriteMode.add
    client, inner, prefix = locate(path)
    # Pieces wait in an upload session at Dropbox, so the server keeps nothing between calls.
    if more and not upload_id:
        if offset:
            raise ToolError("The first piece starts at offset 0; give upload_id to continue an upload.")
        return {"upload_id": client.files_upload_session_start(data).session_id, "offset": len(data)}
    if upload_id:
        cursor = UploadSessionCursor(upload_id, offset)
        if more:
            client.files_upload_session_append_v2(data, cursor)
            return {"upload_id": upload_id, "offset": offset + len(data)}
        commit = CommitInfo(inner, mode=mode, autorename=False)
        return describe(client.files_upload_session_finish(data, cursor, commit), prefix)
    return describe(client.files_upload(data, inner, mode=mode, autorename=False), prefix)


if __name__ == "__main__":  # pragma: no cover
    # Listen on all interfaces so Caddy can reach it (don't publish the port). Stateless, so each
    # request stands alone, and clients' connections survive restarts.
    # Requests may carry a whole write_file piece, base64-encoded (4/3 its size), plus room for the JSON around it.
    mcp.run("streamable-http", host="0.0.0.0", port=8000, stateless_http=True,
            max_request_body_size=MAX_WRITE_BYTES * 4 // 3 + 1024 * 1024)
