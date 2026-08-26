from __future__ import annotations

import os
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from wechat_decrypt_tool import key_service
from wechat_decrypt_tool import macos_resign_key_capture as capture


def _identity(*, cdhash: str = "official-cdhash") -> capture.CodeIdentity:
    return capture.CodeIdentity(
        identifier="com.tencent.xinWeChat",
        team_identifier="5A4RE8SF68",
        cdhash=cdhash,
        authority="Developer ID Application: Tencent Technology (Shenzhen) Company Limited",
        designated_requirement="identifier com.tencent.xinWeChat and anchor apple generic",
        version="4.1.12",
        build="269341",
    )


def _transaction(root: Path) -> capture.CaptureTransaction:
    app_path = root / "WeChat.app"
    staging = root / f".WeChat.wcda-key-capture-{'0' * 32}"
    return capture.CaptureTransaction(
        schema_version=1,
        transaction_id="0" * 32,
        state="temporary_wechat_active",
        app_path=str(app_path),
        staging_root=str(staging),
        work_path=str(staging / "working.app"),
        recovery_path=str(staging / "recovery.app"),
        original_slot_path=str(staging / "original-slot.app"),
        original_identity=asdict(_identity()),
        swapped=True,
    )


def test_target_database_prefers_session_salt(tmp_path: Path) -> None:
    database_root = tmp_path / "db_storage"
    session = database_root / "session" / "session.db"
    message = database_root / "message" / "message_0.db"
    session.parent.mkdir(parents=True)
    message.parent.mkdir(parents=True)
    session_salt = bytes.fromhex("11" * 16)
    session.write_bytes(session_salt + b"encrypted")
    message.write_bytes(bytes.fromhex("22" * 16) + b"encrypted")

    selected, salt = capture._target_database(database_root)

    assert selected == session
    assert salt == session_salt


def test_journal_is_private_and_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture, "_capture_state_dir", lambda: tmp_path / "state")
    transaction = _transaction(tmp_path)

    capture._write_journal(transaction)

    journal = capture.capture_journal_path()
    assert capture._load_journal() == transaction
    assert os.stat(journal).st_mode & 0o777 == 0o600
    assert os.stat(journal.parent).st_mode & 0o777 == 0o700


def test_transaction_rejects_paths_outside_staging(tmp_path: Path) -> None:
    transaction = _transaction(tmp_path)
    transaction.recovery_path = str(tmp_path / "unrelated.app")

    with pytest.raises(capture.MacosResignCaptureError) as raised:
        capture._validate_transaction_paths(transaction)

    assert raised.value.code == "RECOVERY_STATE_INVALID"


def test_recovery_uses_verified_secondary_copy_when_primary_slot_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture, "_capture_state_dir", lambda: tmp_path / "state")
    transaction = _transaction(tmp_path)
    app_path = Path(transaction.app_path)
    recovery_path = Path(transaction.recovery_path)
    app_path.mkdir()
    recovery_path.mkdir(parents=True)
    capture._write_journal(transaction)
    swapped = False

    def inspect(path: str | Path) -> capture.CodeIdentity:
        nonlocal swapped
        candidate = Path(path)
        if candidate == app_path:
            return _identity() if swapped else _identity(cdhash="temporary")
        if candidate == recovery_path:
            return _identity()
        raise AssertionError(f"unexpected identity path: {candidate}")

    def atomic_swap(first: Path, second: Path) -> None:
        nonlocal swapped
        assert first == app_path
        assert second == recovery_path
        swapped = True

    monkeypatch.setattr(capture, "inspect_code_identity", inspect)
    monkeypatch.setattr(capture, "_atomic_swap", atomic_swap)
    monkeypatch.setattr(capture, "_terminate_wechat_processes", lambda **_kwargs: None)
    monkeypatch.setattr(capture, "_verify_bundle", lambda _path: None)

    result = capture.recover_official_wechat(cleanup=False)

    assert result["recovered"] is True
    assert result["state"] == "restored"
    assert swapped is True


def test_recovery_accepts_verified_official_update_before_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture, "_capture_state_dir", lambda: tmp_path / "state")
    transaction = _transaction(tmp_path)
    transaction.swapped = False
    transaction.state = "preparing"
    app_path = Path(transaction.app_path)
    staging_root = Path(transaction.staging_root)
    app_path.mkdir()
    staging_root.mkdir()
    capture._write_journal(transaction)
    updated = capture.CodeIdentity(
        **{
            **asdict(_identity(cdhash="updated-official-cdhash")),
            "build": "269365",
        }
    )

    monkeypatch.setattr(capture, "inspect_code_identity", lambda _path: updated)
    monkeypatch.setattr(capture, "_verify_bundle", lambda _path: None)
    monkeypatch.setattr(
        capture,
        "_atomic_swap",
        lambda *_args: pytest.fail("an unswapped official update must not be swapped"),
    )

    result = capture.recover_official_wechat(cleanup=False)

    assert result["recovered"] is True
    assert result["state"] == "official_app_intact"
    assert result["build"] == "269365"


def test_capture_never_reports_success_when_recovery_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture, "_capture_state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(capture.time, "sleep", lambda _seconds: None)
    app_path = tmp_path / "WeChat.app"
    database_root = tmp_path / "db_storage"
    app_path.mkdir()
    database_root.mkdir()
    official = _identity()

    monkeypatch.setattr(capture, "_preflight", lambda *_args: (official, b"s" * 16))
    monkeypatch.setattr(capture, "_terminate_wechat_processes", lambda **_kwargs: None)

    def clone(_source: Path, destination: Path) -> None:
        destination.mkdir()

    monkeypatch.setattr(capture, "_clone_bundle", clone)
    monkeypatch.setattr(capture, "_verify_bundle", lambda _path: None)
    monkeypatch.setattr(capture, "inspect_code_identity", lambda _path: official)
    monkeypatch.setattr(capture, "_sign_working_copy", lambda *_args: None)
    monkeypatch.setattr(capture, "_atomic_swap", lambda *_args: None)
    monkeypatch.setattr(
        capture,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, b"", b""),
    )
    monkeypatch.setattr(capture, "_wait_for_wechat_pid", lambda _path: 1234)
    monkeypatch.setattr(
        capture,
        "recover_official_wechat",
        lambda **_kwargs: (_ for _ in ()).throw(
            capture.MacosResignCaptureError(
                "restore failed", code="RECOVERY_VERIFY_FAILED"
            )
        ),
    )

    with pytest.raises(capture.MacosResignCaptureError) as raised:
        capture.capture_database_key_with_temporary_resign(
            wechat_app_path=app_path,
            db_storage_path=database_root,
            validator=lambda *_args: {
                "valid": True,
                "verified_roles": ["message", "session"],
                "modes": {},
            },
            lldb_capture=lambda **_kwargs: "ab" * 32,
        )

    assert raised.value.code == "RECOVERY_VERIFY_FAILED"


def test_key_service_routes_explicit_macos_resign_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    def fake_capture(**kwargs: object) -> dict[str, object]:
        received.update(kwargs)
        return {"db_key": "ab" * 32, "method": "macos_resign_lldb"}

    monkeypatch.setattr(key_service, "is_macos", lambda: True)
    monkeypatch.setattr(
        capture, "capture_database_key_with_temporary_resign", fake_capture
    )

    result = key_service.get_db_key_workflow(
        wechat_install_path="/Applications/WeChat.app",
        db_storage_path="/private/tmp/wxid/db_storage",
        key_mode="macos_resign_lldb",
        timeout_seconds=30,
    )

    assert result["method"] == "macos_resign_lldb"
    assert received["wechat_app_path"] == "/Applications/WeChat.app"
    assert received["db_storage_path"] == "/private/tmp/wxid/db_storage"
    assert received["timeout_seconds"] == 300.0
