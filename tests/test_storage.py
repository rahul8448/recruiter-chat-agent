"""Tests for the inquiries store's durability guarantees.

These pin down the three properties storage.py exists to provide:
serialized read-modify-write, atomic writes, and failing loud rather than
silently losing data.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from recruiter_chat_agent.config import Settings
from recruiter_chat_agent.errors import StorageError
from recruiter_chat_agent.storage import read_records, transaction, write_records


def test_read_returns_empty_when_store_does_not_exist() -> None:
    assert read_records() == []


def test_write_then_read_round_trips() -> None:
    write_records([{"name": "Jane", "emails": ["jane@example.com"]}])
    records = read_records()
    assert len(records) == 1
    assert records[0]["name"] == "Jane"


def test_written_file_is_owner_only(isolated_store: Path) -> None:
    """Recruiter PII shouldn't be world-readable."""
    write_records([{"name": "Jane"}])
    mode = isolated_store.stat().st_mode & 0o777
    assert mode == 0o600


def test_write_leaves_no_temp_files_behind(isolated_store: Path) -> None:
    """Atomic writes go via a temp file; it must not survive the write."""
    write_records([{"name": "Jane"}])
    leftovers = [p.name for p in isolated_store.parent.iterdir() if p != isolated_store]
    assert leftovers == []


def test_transaction_persists_mutations() -> None:
    with transaction() as records:
        records.append({"name": "Jane"})

    assert len(read_records()) == 1


def test_transaction_does_not_write_when_the_body_raises() -> None:
    """A failed merge must not leave the store half-updated."""
    write_records([{"name": "Original"}])

    with pytest.raises(ValueError):
        with transaction() as records:
            records.append({"name": "Should not persist"})
            raise ValueError("boom")

    records = read_records()
    assert len(records) == 1
    assert records[0]["name"] == "Original"


def test_concurrent_transactions_do_not_lose_records() -> None:
    """The read-modify-write cycle is serialized.

    Without the lock in transaction(), concurrent writers interleave
    read/modify/write and silently drop each other's records — the exact
    failure mode a threaded web server produces under simultaneous
    submissions.
    """
    writers = 12
    barrier = threading.Barrier(writers)
    errors: list[BaseException] = []

    def add_one(index: int) -> None:
        try:
            barrier.wait(timeout=5)  # maximize the overlap
            with transaction() as records:
                records.append({"name": f"recruiter-{index}"})
        except BaseException as exc:  # noqa: BLE001 - surfaced via assert below
            errors.append(exc)

    threads = [threading.Thread(target=add_one, args=(i,)) for i in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    names = {record["name"] for record in read_records()}
    assert names == {f"recruiter-{i}" for i in range(writers)}


def test_unreadable_json_raises_rather_than_looking_empty(isolated_store: Path) -> None:
    """Returning [] here would let a later write clobber real data."""
    isolated_store.write_text("this is not json", encoding="utf-8")

    with pytest.raises(StorageError):
        read_records()


def test_non_list_json_is_rejected(isolated_store: Path) -> None:
    isolated_store.write_text(json.dumps({"not": "a list"}), encoding="utf-8")

    with pytest.raises(StorageError):
        read_records()


def test_encryption_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("INQUIRIES_ENCRYPTION_KEY", key)

    write_records([{"name": "Jane", "emails": ["jane@example.com"]}])
    assert read_records()[0]["name"] == "Jane"


def test_encrypted_file_is_not_plaintext_on_disk(
    isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INQUIRIES_ENCRYPTION_KEY", Fernet.generate_key().decode())
    write_records([{"name": "Jane", "emails": ["secret@example.com"]}])

    raw = isolated_store.read_bytes()
    assert b"secret@example.com" not in raw
    assert b"Jane" not in raw
    assert raw.startswith(b"gAAAA")  # Fernet token prefix


def test_wrong_encryption_key_raises_rather_than_silently_emptying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong key must never look like an empty store — that would let the
    next write destroy the real (still-encrypted) contact details.
    """
    monkeypatch.setenv("INQUIRIES_ENCRYPTION_KEY", Fernet.generate_key().decode())
    write_records([{"name": "Jane"}])

    monkeypatch.setenv("INQUIRIES_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(StorageError):
        read_records()


def test_missing_key_for_encrypted_file_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INQUIRIES_ENCRYPTION_KEY", Fernet.generate_key().decode())
    write_records([{"name": "Jane"}])

    monkeypatch.delenv("INQUIRIES_ENCRYPTION_KEY")
    with pytest.raises(StorageError):
        read_records()


def test_malformed_encryption_key_refuses_to_fall_back_to_plaintext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator asked for encryption; writing PII in the clear because
    of a typo'd key is the exact outcome they were guarding against.
    """
    monkeypatch.setenv("INQUIRIES_ENCRYPTION_KEY", "not-a-valid-fernet-key")

    with pytest.raises(StorageError):
        write_records([{"name": "Jane"}])


def test_settings_can_target_a_custom_path(tmp_path: Path) -> None:
    """Containers need to point the store at a mounted volume."""
    custom = tmp_path / "nested" / "custom.json"
    settings = Settings.from_env().__class__(inquiries_path=custom)

    write_records([{"name": "Jane"}], settings)
    assert custom.exists()
    assert read_records(settings)[0]["name"] == "Jane"
