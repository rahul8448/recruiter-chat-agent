"""Persistence for recruiter contact requests (the "inquiries store").

Split out of tools.py so the durability concerns live in one place. Three
properties matter here, and none of them are free:

1. **Serialized read-modify-write.** Recording a contact reads the whole
   store, merges into it, and writes it back. Gradio serves concurrent
   requests from a thread pool, so two recruiters submitting at the same
   moment could interleave those steps and silently lose one of the two
   records. `transaction()` holds a lock across the whole cycle.

2. **Atomic writes.** Writing in place means a crash, a SIGTERM from a
   cloud platform mid-deploy, or a full disk can leave a half-written file
   — which, with encryption on, is unrecoverable rather than merely
   truncated. Every write goes to a temp file in the same directory, is
   flushed to disk, and is then `os.replace`d over the target, which is
   atomic on POSIX.

3. **Fail loud, not lossy.** A read that can't be understood returns empty
   *and refuses to let a write clobber the file*, so a wrong encryption key
   can't silently destroy real data.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings
from .errors import StorageError

logger = logging.getLogger(__name__)

# Guards the read-modify-write cycle in transaction(). Module-level because
# the store is a single process-wide file; a per-call lock would guard
# nothing. This does NOT coordinate across processes — a multi-replica
# deployment needs a real database, not a JSON file (see README).
_LOCK = threading.RLock()

Record = dict[str, Any]


def _build_fernet(settings: Settings) -> Fernet | None:
    """Fernet instance for at-rest encryption, or None when disabled."""
    if not settings.encryption_enabled:
        return None
    try:
        return Fernet(settings.inquiries_encryption_key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        # Refuse to fall back to plaintext: the operator explicitly asked
        # for encryption, and silently writing PII in the clear because of
        # a typo'd key is exactly the failure they were guarding against.
        raise StorageError(
            f"INQUIRIES_ENCRYPTION_KEY is set but is not a valid Fernet key ({exc}). "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"'
        ) from exc


def read_records(settings: Settings | None = None) -> list[Record]:
    """Every stored contact request, oldest-added first.

    Returns an empty list when the store doesn't exist yet. Raises
    StorageError when the file exists but can't be understood — callers
    must not treat that as "no records", because overwriting it would
    destroy real data.
    """
    settings = settings or Settings.from_env()
    path = settings.inquiries_path

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        logger.debug("No inquiries store at %s yet — starting empty.", path)
        return []
    except OSError as exc:
        raise StorageError(f"Could not read the inquiries store at {path}: {exc}") from exc

    if not raw.strip():
        return []

    fernet = _build_fernet(settings)
    if fernet is not None:
        try:
            raw = fernet.decrypt(raw)
        except InvalidToken as exc:
            raise StorageError(
                f"Could not decrypt {path} with INQUIRIES_ENCRYPTION_KEY — wrong key, or "
                "the file was written before encryption was enabled. Refusing to continue "
                "rather than risk overwriting real contact details: fix the key, or move "
                "the file aside if you intend to start fresh."
            ) from exc

    try:
        records = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        hint = (
            "It may be encrypted with a key that isn't set in this environment — try "
            "setting INQUIRIES_ENCRYPTION_KEY."
            if fernet is None
            else "The file is decryptable but the contents aren't valid JSON."
        )
        raise StorageError(f"{path} is not readable as JSON. {hint}") from exc

    if not isinstance(records, list):
        raise StorageError(
            f"{path} should contain a JSON list of records, found {type(records).__name__}."
        )

    return records


def write_records(records: list[Record], settings: Settings | None = None) -> None:
    """Persist `records`, atomically and with owner-only permissions."""
    settings = settings or Settings.from_env()
    path = settings.inquiries_path
    fernet = _build_fernet(settings)

    try:
        payload = (json.dumps(records, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StorageError(f"Could not serialize records to JSON: {exc}") from exc

    if fernet is not None:
        payload = fernet.encrypt(payload)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError(f"Could not create {path.parent}: {exc}") from exc

    tmp_path: Path | None = None
    try:
        # Same directory as the target so os.replace stays on one
        # filesystem and is therefore atomic.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            # Force to disk before the rename, so a power loss can't leave
            # the rename visible while the contents are still buffered.
            os.fsync(handle.fileno())

        # Owner read/write only — recruiter PII shouldn't be world-readable.
        # Set before the rename so the file is never briefly permissive.
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        tmp_path = None
    except OSError as exc:
        raise StorageError(f"Could not write the inquiries store at {path}: {exc}") from exc
    finally:
        if tmp_path is not None and tmp_path.exists():
            # A failed write shouldn't leave debris behind.
            try:
                tmp_path.unlink()
            except OSError:
                logger.warning("Could not clean up temp file %s", tmp_path, exc_info=True)

    logger.debug("Wrote %d record(s) to %s", len(records), path)


@contextmanager
def transaction(settings: Settings | None = None) -> Iterator[list[Record]]:
    """Read the store, let the caller mutate it, then write it back — with
    the whole cycle serialized against concurrent callers.

    Yields the mutable list of records. On a clean exit the list is
    persisted; if the body raises, nothing is written, so a failed merge
    can't leave the store half-updated.
    """
    settings = settings or Settings.from_env()
    with _LOCK:
        records = read_records(settings)
        yield records
        write_records(records, settings)
