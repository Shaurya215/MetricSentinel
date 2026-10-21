"""Fetch the Olist files and verify them against `checksums.json` (FR-001).

Three ways to get the data, tried in this order:

1. The nine CSV files are already in the raw directory.
2. A zip containing them is in the raw directory (the manual Kaggle download).
3. Kaggle credentials are configured and the zip is downloaded from the Kaggle API.

Checksums use trust-on-first-use: the first successful load records the SHA-256 of
each file in `checksums.json` (which should then be committed). Every later load
must match, otherwise it is a hard failure.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.logging import get_logger
from pipelines.config import CHECKSUMS_FILENAME, KAGGLE_DOWNLOAD_URL, SOURCE_TABLES

log = get_logger(__name__)

MANUAL_DOWNLOAD_HELP = """\
The Olist dataset is not in {raw_dir}. Get it in one of two ways:

  A) Manual (no setup): open https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce,
     click Download, and put the zip (archive.zip) into {raw_dir}. Then run `make data` again.
  B) Automatic: create a Kaggle API token (https://www.kaggle.com/settings -> API),
     set KAGGLE_USERNAME and KAGGLE_KEY in .env, then run `make data` again.
"""


class DataNotFoundError(RuntimeError):
    """The dataset is not on disk and could not be downloaded."""


class ChecksumMismatchError(RuntimeError):
    """A raw file differs from the checksum recorded on first load."""


@dataclass(frozen=True)
class RawFile:
    table: str
    path: Path
    sha256: str
    size_bytes: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _missing(raw_dir: Path) -> list[str]:
    return [t.filename for t in SOURCE_TABLES if not (raw_dir / t.filename).is_file()]


def extract_zip(zip_path: Path, raw_dir: Path) -> list[str]:
    """Extract only the expected CSVs, by base name (this also blocks path traversal)."""
    wanted = {t.filename for t in SOURCE_TABLES}
    extracted: list[str] = []
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            name = Path(member.filename).name
            if member.is_dir() or name not in wanted:
                continue
            with archive.open(member) as src, (raw_dir / name).open("wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(name)
    return sorted(extracted)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _stream_to_file(response: Any, dest: Path) -> None:
    part = dest.with_suffix(dest.suffix + ".part")
    with part.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    part.replace(dest)


def download_from_kaggle(
    username: str, key: str, dest: Path, url: str = KAGGLE_DOWNLOAD_URL
) -> None:
    """Download the dataset zip. Credentials are sent only to the first host.

    Kaggle answers with a redirect to signed storage. Following it with the
    Authorization header attached would both leak the key and be rejected, so the
    redirect is followed manually, without credentials.
    """
    token = base64.b64encode(f"{username}:{key}".encode()).decode()
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
    try:
        with opener.open(request, timeout=60) as response:  # served directly, no redirect
            _stream_to_file(response, dest)
            return
    except urllib.error.HTTPError as error:
        if error.code not in (301, 302, 303, 307, 308):
            raise DataNotFoundError(
                f"Kaggle refused the download (HTTP {error.code}). Check KAGGLE_USERNAME and "
                "KAGGLE_KEY, and that you accepted the dataset's terms on its Kaggle page."
            ) from error
        target = error.headers["Location"]
    with urllib.request.urlopen(target, timeout=300) as response:
        _stream_to_file(response, dest)


def _load_checksums(raw_dir: Path) -> dict[str, str]:
    path = raw_dir / CHECKSUMS_FILENAME
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return dict(data.get("files", {}))


def _write_checksums(raw_dir: Path, files: dict[str, str]) -> None:
    payload = {
        "dataset": "olistbr/brazilian-ecommerce",
        "algorithm": "sha256",
        "files": dict(sorted(files.items())),
    }
    (raw_dir / CHECKSUMS_FILENAME).write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def ensure_raw_files(
    raw_dir: Path,
    *,
    kaggle_username: str | None = None,
    kaggle_key: str | None = None,
    update_checksums: bool = False,
) -> tuple[list[RawFile], bool]:
    """Make sure the nine CSVs are present and verified.

    Returns the verified files and whether `checksums.json` was written in this call
    (first load, or `update_checksums`). Raises `DataNotFoundError` or
    `ChecksumMismatchError`.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)

    if _missing(raw_dir):
        for zip_path in sorted(raw_dir.glob("*.zip")):
            extract_zip(zip_path, raw_dir)
            if not _missing(raw_dir):
                log.info("raw_zip_extracted", zip=zip_path.name)
                break

    if _missing(raw_dir) and kaggle_username and kaggle_key:
        zip_path = raw_dir / "brazilian-ecommerce.zip"
        log.info("kaggle_download_started", dataset="olistbr/brazilian-ecommerce")
        download_from_kaggle(kaggle_username, kaggle_key, zip_path)
        extract_zip(zip_path, raw_dir)

    missing = _missing(raw_dir)
    if missing:
        raise DataNotFoundError(
            MANUAL_DOWNLOAD_HELP.format(raw_dir=raw_dir) + f"\nMissing files: {', '.join(missing)}"
        )

    files = [
        RawFile(
            t.table,
            raw_dir / t.filename,
            sha256_file(raw_dir / t.filename),
            (raw_dir / t.filename).stat().st_size,
        )
        for t in SOURCE_TABLES
    ]

    recorded = _load_checksums(raw_dir)
    current = {f.path.name: f.sha256 for f in files}
    if recorded and not update_checksums:
        bad = sorted(name for name, digest in current.items() if recorded.get(name) != digest)
        if bad:
            raise ChecksumMismatchError(
                "Raw files differ from checksums.json: "
                + ", ".join(bad)
                + ". If the dataset was intentionally replaced, re-run with --update-checksums."
            )
        return files, False

    _write_checksums(raw_dir, current)
    log.info("checksums_recorded", files=len(current), forced=update_checksums)
    return files, True
