import asyncio
import base64
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from dropbox.files import FileMetadata, FolderMetadata, ListFolderResult
from dropbox.sharing import AccessLevel
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image

import server


def file_md(path, size=5):
    return FileMetadata(
        name=path.rsplit("/", 1)[-1], id="id:" + path, path_display=path, path_lower=path.lower(),
        size=size, rev="0123456789", client_modified=datetime(2026, 1, 1), server_modified=datetime(2026, 1, 1),
    )


def ctx(*permissions):
    return SimpleNamespace(headers={"x-auth-user": "google-oauth2|1", "x-auth-permissions": ",".join(permissions)})


@pytest.fixture
def dbx(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(server, "dbx", lambda: client)
    monkeypatch.setattr(server, "WRITABLE_PATHS", ["/Shared/Team", "ns:42"])
    monkeypatch.setattr(server, "WRITE_PERMISSION", "dropbox:write")
    return client


@pytest.mark.parametrize("raw, expected", [
    ("", ""), ("/", ""), ("//", ""), ("Docs/", "/Docs"), ("/Docs//a/./b.txt", "/Docs/a/b.txt"),
    ("ns:42", "ns:42"), ("ns:42/sub/", "ns:42/sub"), ("id:abc", "id:abc"),
])
def test_clean(raw, expected):
    assert server.clean(raw) == expected


def test_clean_refuses_parent_segments():
    with pytest.raises(ToolError):
        server.clean("/Shared/Team/../../Private/secret.txt")


@pytest.mark.parametrize("path, writable", [
    ("/Shared/Team", True),
    ("/shared/team/Notes.txt", True),  # Dropbox paths are case-insensitive
    ("/Shared/Team/a/b.txt", True),
    ("/Shared/TeamOther/x.txt", False),  # prefix of a name, not a folder
    ("/Shared", False),
    ("/Private/x.txt", False),
    ("ns:42/x.txt", True),
    ("ns:420/x.txt", False),
    ("id:anything", False),
])
def test_is_writable(dbx, path, writable):
    assert server.is_writable(path) == writable


def test_list_folder_pages_and_flags_writable(dbx):
    dbx.files_list_folder.return_value = ListFolderResult(
        entries=[FolderMetadata(name="Team", id="id:t", path_display="/Shared/Team", path_lower="/shared/team",
                                shared_folder_id="42")],
        cursor="c1", has_more=True)
    dbx.files_list_folder_continue.return_value = ListFolderResult(
        entries=[file_md("/Shared/readme.md")], cursor="c2", has_more=False)

    result = server.list_folder("/Shared/")

    dbx.files_list_folder.assert_called_once_with("/Shared", recursive=False, limit=2000)
    assert result["truncated"] is False
    team, readme = result["entries"]
    assert team == {"name": "Team", "path": "/Shared/Team", "id": "id:t", "type": "folder",
                    "shared_folder_id": "42", "writable": True}
    assert readme["type"] == "file" and readme["writable"] is False


def test_list_folder_in_unmounted_shared_folder_builds_paths(dbx):
    # Entries in a namespace that isn't mounted come back without path_display.
    entry = file_md("/x.txt")
    entry.path_display = None
    entry.path_lower = None
    dbx.files_list_folder.return_value = ListFolderResult(entries=[entry], cursor="c", has_more=False)

    [e] = server.list_folder("ns:42")["entries"]
    assert e["path"] == "ns:42/x.txt" and e["writable"] is True


def test_read_file_text_image_and_binary(dbx):
    dbx.files_download.return_value = (file_md("/a.txt"), SimpleNamespace(content="héllo".encode()))
    assert server.read_file("/a.txt") == "héllo"

    dbx.files_download.return_value = (file_md("/p.png"), SimpleNamespace(content=b"\x89PNG"))
    image = server.read_file("/p.png")
    assert isinstance(image, Image) and image.to_image_content().mime_type == "image/png"

    dbx.files_download.return_value = (file_md("/b.bin"), SimpleNamespace(content=b"\xff\xfe\x00"))
    with pytest.raises(ToolError, match="base64"):
        server.read_file("/b.bin")
    assert base64.b64decode(server.read_file("/b.bin", encoding="base64")) == b"\xff\xfe\x00"


def test_read_file_refuses_large_files(dbx, monkeypatch):
    monkeypatch.setattr(server, "MAX_READ_BYTES", 4)
    response = MagicMock()
    dbx.files_download.return_value = (file_md("/big.txt", size=5), response)
    with pytest.raises(ToolError, match="limit"):
        server.read_file("/big.txt")
    response.close.assert_called_once()


def test_write_file_inside_writable_path(dbx):
    dbx.files_upload.return_value = file_md("/Shared/Team/notes.txt")
    result = server.write_file("/Shared/Team/notes.txt", "hi", ctx("dropbox:write"))

    data, path = dbx.files_upload.call_args.args
    assert (data, path) == (b"hi", "/Shared/Team/notes.txt")
    assert dbx.files_upload.call_args.kwargs["mode"] == server.WriteMode.add
    assert result["writable"] is True


@pytest.mark.parametrize("path, permissions, message", [
    ("/Private/notes.txt", ["dropbox:write"], "read-only"),
    ("/Shared/Team/../Private/x.txt", ["dropbox:write"], r"'\.\.'"),
    ("id:abc", ["dropbox:write"], "full path"),
    ("/Shared/Team/notes.txt", ["read"], "permission"),
])
def test_write_file_refusals(dbx, path, permissions, message):
    with pytest.raises(ToolError, match=message):
        server.write_file(path, "hi", ctx(*permissions))
    dbx.files_upload.assert_not_called()


def test_write_file_without_caddy_headers_is_refused(dbx):
    with pytest.raises(ToolError, match="permission"):
        server.write_file("/Shared/Team/notes.txt", "hi", SimpleNamespace(headers=None))


def test_list_shared_folders_merges_mounted_and_unmounted(dbx):
    def shared(name, sid, path):
        return SimpleNamespace(name=name, shared_folder_id=sid, path_lower=path and path.lower(), path_display=path,
                               access_type=AccessLevel.editor, is_team_folder=False)
    dbx.sharing_list_folders.return_value = SimpleNamespace(entries=[shared("Team", "1", "/Shared/Team")], cursor="c")
    dbx.sharing_list_folders_continue.return_value = SimpleNamespace(entries=[shared("Old", "7", None)], cursor=None)
    dbx.sharing_list_mountable_folders.return_value = SimpleNamespace(
        entries=[shared("Old", "7", None), shared("Clients", "42", None)], cursor=None)

    folders = server.list_shared_folders()

    assert [(f["name"], f["path"], f["in_my_dropbox"], f["writable"]) for f in folders] == [
        ("Clients", "ns:42", False, True),
        ("Old", "ns:7", False, False),
        ("Team", "/Shared/Team", True, True),
    ]
    assert folders[0]["access"] == "editor"


def test_tools_are_registered_with_read_only_hints():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert set(tools) == {"account_info", "list_folder", "search", "get_metadata", "read_file",
                          "list_shared_folders", "write_file"}
    assert all(t.annotations.read_only_hint for name, t in tools.items() if name != "write_file")
    assert tools["write_file"].annotations.read_only_hint is False
