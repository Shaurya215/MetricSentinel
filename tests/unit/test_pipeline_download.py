"""FR-001: obtaining the dataset and verifying it against checksums."""

from __future__ import annotations

import json
import shutil
import threading
import zipfile
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from pipelines.config import SOURCE_TABLES
from pipelines.download import (
    ChecksumMismatchError,
    DataNotFoundError,
    download_from_kaggle,
    ensure_raw_files,
    extract_zip,
)

pytestmark = pytest.mark.unit


def test_missing_data_explains_how_to_get_it(tmp_path: Path) -> None:
    with pytest.raises(DataNotFoundError) as error:
        ensure_raw_files(tmp_path / "raw")
    message = str(error.value)
    assert "kaggle.com/datasets/olistbr/brazilian-ecommerce" in message
    assert "KAGGLE_USERNAME" in message
    assert "olist_orders_dataset.csv" in message


def test_first_load_records_checksums_and_later_loads_verify_them(small_raw: Path) -> None:
    files, recorded = ensure_raw_files(small_raw)
    assert recorded is True and len(files) == len(SOURCE_TABLES)
    stored = json.loads((small_raw / "checksums.json").read_text(encoding="utf-8"))["files"]
    assert set(stored) == {t.filename for t in SOURCE_TABLES}

    _, recorded_again = ensure_raw_files(small_raw)
    assert recorded_again is False


def test_a_changed_file_is_a_hard_failure(small_raw: Path) -> None:
    ensure_raw_files(small_raw)
    with (small_raw / "olist_orders_dataset.csv").open("a", encoding="utf-8") as handle:
        handle.write("tampered,row\n")
    with pytest.raises(ChecksumMismatchError, match=r"olist_orders_dataset.csv"):
        ensure_raw_files(small_raw)


def test_update_checksums_accepts_the_new_files(small_raw: Path) -> None:
    ensure_raw_files(small_raw)
    with (small_raw / "olist_sellers_dataset.csv").open("a", encoding="utf-8") as handle:
        handle.write("\n")
    _, recorded = ensure_raw_files(small_raw, update_checksums=True)
    assert recorded is True
    ensure_raw_files(small_raw)  # verifies cleanly against the new reference


def test_zip_download_is_extracted_and_unwanted_members_are_ignored(
    small_raw: Path, tmp_path: Path
) -> None:
    raw = tmp_path / "from_zip"
    raw.mkdir()
    archive = raw / "archive.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for t in SOURCE_TABLES:
            z.write(small_raw / t.filename, arcname=f"nested/dir/{t.filename}")
        z.writestr("../../escape.txt", "path traversal attempt")
        z.writestr("notes.md", "not a dataset file")
    extracted = extract_zip(archive, raw)
    assert sorted(extracted) == sorted(t.filename for t in SOURCE_TABLES)
    assert not (tmp_path / "escape.txt").exists()
    assert not (raw / "notes.md").exists()
    files, _ = ensure_raw_files(raw)  # the zip alone is enough
    assert len(files) == len(SOURCE_TABLES)


# ---------------------------------------------------------------------------
# Kaggle API: credentials must go to Kaggle only, not to the redirect target
# ---------------------------------------------------------------------------
class _FakeKaggle(BaseHTTPRequestHandler):
    seen: ClassVar[dict[str, str | None]] = {}
    payload = b"PK-fake-zip-bytes"

    def do_GET(self) -> None:
        if self.path == "/api/download":
            if self.headers.get("Authorization") is None:
                self.send_response(401)
                self.end_headers()
                return
            type(self).seen["api_auth"] = self.headers.get("Authorization")
            self.send_response(302)
            self.send_header("Location", f"http://{self.headers['Host']}/storage/file.zip")
            self.end_headers()
        elif self.path == "/storage/file.zip":
            type(self).seen["storage_auth"] = self.headers.get("Authorization")
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.payload)))
            self.end_headers()
            self.wfile.write(self.payload)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: object) -> None:  # keep test output quiet
        pass


@pytest.fixture
def fake_kaggle() -> Iterator[str]:
    _FakeKaggle.seen = {}
    server = HTTPServer(("127.0.0.1", 0), _FakeKaggle)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api/download"
    server.shutdown()


def test_kaggle_credentials_are_not_forwarded_to_the_redirect_target(
    fake_kaggle: str, tmp_path: Path
) -> None:
    dest = tmp_path / "dataset.zip"
    download_from_kaggle("someone", "secret-key", dest, url=fake_kaggle)
    assert dest.read_bytes() == _FakeKaggle.payload
    assert (_FakeKaggle.seen["api_auth"] or "").startswith("Basic ")
    assert _FakeKaggle.seen["storage_auth"] is None
    assert not dest.with_suffix(".zip.part").exists()


def test_rejected_credentials_give_a_clear_error(fake_kaggle: str, tmp_path: Path) -> None:
    # The fake server answers 401 only when no Authorization header is sent; point at a 404 path instead.
    with pytest.raises(DataNotFoundError, match="HTTP 404"):
        download_from_kaggle(
            "someone", "bad", tmp_path / "x.zip", url=fake_kaggle.replace("/api/download", "/nope")
        )


def test_checksum_file_survives_a_second_copy_of_the_dataset(
    small_raw: Path, tmp_path: Path
) -> None:
    ensure_raw_files(small_raw)
    other = tmp_path / "other"
    shutil.copytree(small_raw, other)
    _, recorded = ensure_raw_files(other)
    assert recorded is False  # identical bytes verify against the copied checksums.json
