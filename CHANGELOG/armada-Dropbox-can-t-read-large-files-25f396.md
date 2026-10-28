# Changelog: Dropbox can't read large files

- Armada session: `Dropbox can't read large files`
- Branch: `armada/Dropbox-can-t-read-large-files-25f396`
- Base branch: `master`
- Started: 2026-10-28 07:51 UTC

Entries are appended newest last, each stamped with the UTC date and time.

## 2026-10-28 02:54 CDT
dropbox-mcp can now read files of any size, including multi-GB ones.
- **The limit:** `read_file` refused any file over `MAX_READ_BYTES` (10 MiB) outright.
- **Pieces:** a bigger file now comes back as its first piece with `size`, `next_offset` and `more`; calling again with `offset=next_offset` returns the next, until `more` is false. `offset` and `length` also read part of any file. Each piece is fetched alone with an HTTP `Range` request, so the server never holds more than one piece. Text pieces end on a character boundary, so no character is split between pieces. `account_info` now reports `max_read_bytes`. Files that fit are returned exactly as before.
- **Deployed server:** the running instance at dropbox-mcp.north.altamontco.com is older than master. It lacks the chunked writes from #2 (no `max_write_bytes`, no `more`/`upload_id` on `write_file`), so it needs a rebuild to get those and this change.
- **Testing:** unit tests with a fake that honors `Range` like Dropbox. Not yet tried against a real Dropbox account.
