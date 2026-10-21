import asyncio
import base64
import importlib
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from dropbox.common import PathRoot
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
    client.at = {"42": MagicMock()}  # clients rooted at a shared or team folder, by its namespace ID
    monkeypatch.setattr(server, "dbx", lambda namespace="": client.at[namespace] if namespace else client)
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


def test_ns_paths_are_reached_from_their_folder_and_come_back_as_ns_paths(dbx):
    """Team folders, and shared folders not added to the account: Dropbox only reaches them rooted at them."""
    no_path = file_md("/Sub/y.txt")
    no_path.path_display = no_path.path_lower = None
    dbx.at["42"].files_list_folder.return_value = ListFolderResult(
        entries=[file_md("/Sub/x.txt"), no_path], cursor="c", has_more=False)

    x, y = server.list_folder("ns:42/Sub/")["entries"]

    dbx.at["42"].files_list_folder.assert_called_once_with("/Sub", recursive=False, limit=2000)
    dbx.files_list_folder.assert_not_called()
    assert (x["path"], y["path"], x["writable"]) == ("ns:42/Sub/x.txt", "ns:42/Sub/y.txt", True)

    dbx.at["42"].files_upload.return_value = file_md("/notes.txt")
    assert server.write_file("ns:42/notes.txt", "hi", ctx("dropbox:write"))["path"] == "ns:42/notes.txt"
    assert dbx.at["42"].files_upload.call_args.args == (b"hi", "/notes.txt")

    dbx.at["42"].files_search_v2.return_value = SimpleNamespace(
        matches=[SimpleNamespace(metadata=SimpleNamespace(get_metadata=lambda: file_md("/Sub/x.txt")))])
    [found] = server.search("x", "ns:42")
    assert found["path"] == "ns:42/Sub/x.txt"
    assert dbx.at["42"].files_search_v2.call_args.kwargs["options"].path is None  # the whole folder


def test_ns_clients_are_rooted_at_their_folder(monkeypatch):
    for name in ("APP_KEY", "APP_SECRET", "DROPBOX_REFRESH_TOKEN"):
        monkeypatch.setattr(server, name, "x")
    monkeypatch.setattr(server.dropbox, "Dropbox", MagicMock())
    server.dbx.cache_clear()
    try:
        assert server.dbx("42") is server.dbx("42")  # kept, so it refreshes its access token only when it expires
        server.dropbox.Dropbox.return_value.with_path_root.assert_called_once_with(PathRoot.namespace_id("42"))
    finally:
        server.dbx.cache_clear()


@pytest.mark.parametrize("folder, namespace, inner", [
    ("/Projects", "", "/Projects"),
    ("ns:42", "42", ""),
    ("ns:42/Drawings", "42", "/Drawings"),
])
def test_with_one_folder_it_is_the_top_level(dbx, monkeypatch, folder, namespace, inner):
    monkeypatch.setattr(server, "DROPBOX_FOLDER", folder)
    monkeypatch.setattr(server, "WRITABLE_PATHS", ["/Outbox"])
    client = dbx.at[namespace] if namespace else dbx
    client.files_list_folder.return_value = ListFolderResult(
        entries=[FolderMetadata(name="Outbox", id="id:o", path_display=f"{inner}/Outbox", path_lower=f"{inner}/outbox".lower()),
                 file_md(f"{inner}/Outbox/a.txt")], cursor="c", has_more=False)

    outbox, a = server.list_folder("", recursive=True)["entries"]

    client.files_list_folder.assert_called_once_with(inner, recursive=True, limit=2000)
    assert (outbox["path"], a["path"], a["writable"]) == ("/Outbox", "/Outbox/a.txt", True)

    client.files_download.return_value = (file_md(f"{inner}/Outbox/a.txt"), SimpleNamespace(content=b"hi"))
    assert server.read_file("/Outbox/a.txt") == "hi"
    client.files_download.assert_called_once_with(f"{inner}/Outbox/a.txt")

    client.files_search_v2.return_value = SimpleNamespace(
        matches=[SimpleNamespace(metadata=SimpleNamespace(get_metadata=lambda: file_md(f"{inner}/Outbox/a.txt")))])
    assert [found["path"] for found in server.search("a")] == ["/Outbox/a.txt"]
    assert client.files_search_v2.call_args.kwargs["options"].path == (inner or None)  # None: all of ns:42

    client.files_upload.return_value = file_md(f"{inner}/Outbox/b.txt")
    assert server.write_file("/Outbox/b.txt", "hi", ctx("dropbox:write"))["path"] == "/Outbox/b.txt"
    assert client.files_upload.call_args.args == (b"hi", f"{inner}/Outbox/b.txt")
    if namespace:
        assert not dbx.mock_calls  # nothing went to the client rooted at your own folder
    assert server.account_info(ctx())["folder"] == folder


def test_with_one_folder_the_folder_itself_is_the_top(monkeypatch):
    """A recursive listing includes the folder itself."""
    monkeypatch.setattr(server, "DROPBOX_FOLDER", "ns:42/Drawings")
    folder = FolderMetadata(name="Drawings", id="id:d", path_display="/Drawings", path_lower="/drawings")
    assert server.describe(folder)["path"] == ""


@pytest.mark.parametrize("path", ["ns:7", "ns:42/Drawings/a.txt", "id:abc", "rev:0123456789"])
def test_with_one_folder_nothing_outside_it_can_be_reached(dbx, monkeypatch, path):
    monkeypatch.setattr(server, "DROPBOX_FOLDER", "ns:42/Drawings")
    monkeypatch.setattr(server, "WRITABLE_PATHS", ["ns:42/Drawings"])  # all of it
    for tool in (server.list_folder, server.get_metadata, server.read_file, lambda p: server.search("a", p)):
        with pytest.raises(ToolError, match="inside this server's folder"):
            tool(path)
    with pytest.raises(ToolError):
        server.write_file(path, "hi", ctx("dropbox:write"))
    assert not dbx.mock_calls and not dbx.at["42"].mock_calls


def test_with_one_folder_writable_paths_may_be_given_in_full(dbx, monkeypatch):
    """With the folder ns:42, clients see ns:42/ai, and write to it, as /ai."""
    monkeypatch.setattr(server, "DROPBOX_FOLDER", "ns:42")
    monkeypatch.setattr(server, "WRITABLE_PATHS", ["ns:42/ai", "/Outbox"])
    dbx.at["42"].files_upload.return_value = file_md("/ai/test.txt")

    assert server.write_file("/ai/test.txt", "hi", ctx("dropbox:write"))["writable"] is True
    assert dbx.at["42"].files_upload.call_args.args == (b"hi", "/ai/test.txt")
    assert server.account_info(ctx())["writable_paths"] == ["/ai", "/Outbox"]
    with pytest.raises(ToolError, match=r"Writable paths: /ai, /Outbox\.$"):
        server.write_file("/notes.txt", "hi", ctx("dropbox:write"))

    monkeypatch.setattr(server, "WRITABLE_PATHS", ["ns:42/"])  # all of it
    assert server.writable_paths() == [""] and server.is_writable("/any/file.txt")

    monkeypatch.setattr(server, "WRITABLE_PATHS", ["ns:420/x"])  # another folder, so never usable
    with pytest.raises(ToolError, match="ns:420/x isn't inside DROPBOX_FOLDER ns:42"):
        server.account_info(ctx())


def test_with_one_folder_other_folders_are_not_listed(monkeypatch):
    monkeypatch.setenv("DROPBOX_FOLDER", "ns:42/Drawings/")
    try:
        importlib.reload(server)
        assert server.DROPBOX_FOLDER == "ns:42/Drawings"
        assert "list_shared_folders" not in {t.name for t in asyncio.run(server.mcp.list_tools())}
        assert "ns:" not in server.mcp.instructions
    finally:
        monkeypatch.delenv("DROPBOX_FOLDER")
        importlib.reload(server)


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


def test_empty_write_permission_still_requires_the_default(monkeypatch):
    monkeypatch.setenv("WRITE_PERMISSION", "")
    importlib.reload(server)
    assert server.WRITE_PERMISSION == "dropbox:write"
