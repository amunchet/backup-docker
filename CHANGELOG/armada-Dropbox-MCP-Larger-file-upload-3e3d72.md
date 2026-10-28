# Changelog: Dropbox MCP Larger file upload

- Armada session: `Dropbox MCP Larger file upload`
- Branch: `armada/Dropbox-MCP-Larger-file-upload-3e3d72`
- Base branch: `master`
- Started: 2026-10-28 01:05 UTC

Entries are appended newest last, each stamped with the UTC date and time.

## 2026-10-27 20:08 CDT
dropbox-mcp can now write files of several GB.
- **The limit:** the MCP library refuses any request over 4 MiB with *413 Request body too large*. Base64 takes up a third more room, so the largest file `write_file` could carry was about 3 MB.
- **Bigger pieces:** one call now carries up to `MAX_WRITE_BYTES`, which defaults to 100 MiB. Its ceiling is 150 MiB, the most Dropbox takes in one request. The server sizes the request limit to match. `account_info` reports the setting, and an out-of-range value stops the server at startup.
- **Files in pieces:** `write_file` takes `more`, `upload_id` and `offset`, which send a file as a series of pieces through a Dropbox upload session. The pieces wait at Dropbox, so the server stays stateless and a restart in the middle loses nothing. Every piece has the same path, permission and writable-folder checks as a whole file.
- **Testing:** unit tests. The real server ran over HTTP and accepted a 100 MiB piece, then refused one just over the limit. Not yet tried against a real Dropbox account.
