import os
import tempfile
import pytest
import hashlib
from unittest.mock import MagicMock
from backup import calculate_md5, read_checksums, write_checksums, monitor_and_upload, DropboxClient
import backup

# Test calculate_md5 function
def test_calculate_md5():
    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
        temp_file.write(b"test data")
        temp_file.close()
        assert calculate_md5(temp_file.name) == hashlib.md5(b"test data").hexdigest()
        os.remove(temp_file.name)

# Test read_checksums function
def test_read_checksums():
    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
        temp_file.write(b"file1 abcdef1234567890\nfile2 1234567890abcdef\n")
        temp_file.close()
        checksums = read_checksums(temp_file.name)
        assert checksums == {"file1": "abcdef1234567890", "file2": "1234567890abcdef"}
        os.remove(temp_file.name)

# Test write_checksums function
def test_write_checksums():
    checksums = {"file1": "abcdef1234567890", "file2": "1234567890abcdef"}
    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
        write_checksums(temp_file.name, checksums)
        temp_file.close()
        with open(temp_file.name, "r") as f:
            lines = f.readlines()
            assert lines == ["file1 abcdef1234567890\n", "file2 1234567890abcdef\n"]
        os.remove(temp_file.name)

# Test monitor_and_upload function
def test_monitor_and_upload(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        file1_path = os.path.join(temp_dir, "file1")
        file2_path = os.path.join(temp_dir, "file2")
        checksums_file = os.path.join(temp_dir, "checksums.txt")
        
        with open(file1_path, "w") as f:
            f.write("test data 1")
        with open(file2_path, "w") as f:
            f.write("test data 2")
        with open(checksums_file, "w") as f:
            f.write(f"file1 {backup.calculate_md5(file1_path)}\n")

        dropbox_client = MagicMock()

        # Mock environment variables
        backup.WATCH_FOLDER = temp_dir
        backup.DROPBOX_FOLDER = "/dropbox_folder"

        backup.monitor_and_upload(dropbox_client)

        # Ensure that file2 was uploaded
        dropbox_client.upload.assert_any_call(file2_path, os.path.join("/dropbox_folder", "file2"))
        
        # Ensure that checksums.txt was uploaded
        dropbox_client.upload.assert_any_call(checksums_file, os.path.join("/dropbox_folder", "checksums.txt"))
        
        # Ensure checksums.txt is updated with new checksums
        with open(checksums_file, "r") as f:
            lines = f.readlines()
            assert len(lines) == 3
            splits = [x.split(" ")[0] for x in lines]
            assert "checksums.txt" in splits
            assert "file1" in splits
            assert "file2" in splits
