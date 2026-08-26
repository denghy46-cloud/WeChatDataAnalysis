import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from wechat_decrypt_tool.routers import account_prepare


SQLITE_HEADER = b"SQLite format 3\x00"
DB_KEY = "ab" * 32


def _write_ready_account(output_root: Path, account: str, db_storage: Path) -> Path:
    account_dir = output_root / account
    account_dir.mkdir(parents=True, exist_ok=True)
    for name in ("contact.db", "session.db", "message_0.db"):
        (account_dir / name).write_bytes(SQLITE_HEADER + b"fixture")
    (account_dir / "_source.json").write_text(
        json.dumps({"db_storage_path": str(db_storage), "wxid_dir": str(db_storage.parent)}),
        encoding="utf-8",
    )
    return account_dir


def test_prepare_state_reports_reusable_account_without_returning_key(tmp_path: Path) -> None:
    db_storage = tmp_path / "wxid_demo_1234" / "db_storage"
    db_storage.mkdir(parents=True)
    output_root = tmp_path / "output" / "databases"
    _write_ready_account(output_root, "wxid_demo", db_storage)

    saved = {
        "status": "success",
        "keys": {"db_key": DB_KEY, "updated_at": "2026-08-17T00:00:00"},
    }
    with (
        patch.object(account_prepare, "get_output_databases_dir", return_value=output_root),
        patch.object(account_prepare, "get_saved_keys", new=AsyncMock(return_value=saved)),
    ):
        state = asyncio.run(account_prepare.get_account_prepare_state("wxid_demo", str(db_storage)))

    assert state["key_saved"] is True
    assert state["decrypted_ready"] is True
    assert state["export_ready"] is True
    assert state["next_action"] == "open_chat"
    assert DB_KEY not in json.dumps(state)


def test_prepare_account_requests_capture_for_new_account(tmp_path: Path) -> None:
    db_storage = tmp_path / "wxid_new_1234" / "db_storage"
    db_storage.mkdir(parents=True)
    request = account_prepare.AccountPrepareRequest(
        account="wxid_new",
        db_storage_path=str(db_storage),
    )
    with (
        patch.object(account_prepare, "get_output_databases_dir", return_value=tmp_path / "output"),
        patch.object(
            account_prepare,
            "get_saved_keys",
            new=AsyncMock(return_value={"status": "success", "keys": {}}),
        ),
    ):
        result = asyncio.run(account_prepare.prepare_account(request))

    assert result["status"] == "needs_key_capture"
    assert result["next_action"] == "capture_key"
    assert result["key_saved"] is False


def test_prepare_account_decrypts_missing_copy_with_saved_key(tmp_path: Path) -> None:
    db_storage = tmp_path / "wxid_demo_1234" / "db_storage"
    db_storage.mkdir(parents=True)
    output_root = tmp_path / "output" / "databases"
    request = account_prepare.AccountPrepareRequest(
        account="wxid_demo",
        db_storage_path=str(db_storage),
    )

    def fake_decrypt(path: str, key: str):
        assert path == str(db_storage)
        assert key == DB_KEY
        account_dir = _write_ready_account(output_root, "wxid_demo", db_storage)
        return {
            "status": "success",
            "message": "ok",
            "successful_count": 3,
            "failed_count": 0,
            "account_results": {
                "wxid_demo": {
                    "output_dir": str(account_dir),
                    "source_db_storage_path": str(db_storage),
                    "source_wxid_dir": str(db_storage.parent),
                }
            },
        }

    saved = {"status": "success", "keys": {"db_key": DB_KEY}}
    with (
        patch.object(account_prepare, "get_output_databases_dir", return_value=output_root),
        patch.object(account_prepare, "get_saved_keys", new=AsyncMock(return_value=saved)),
        patch.object(account_prepare, "_decrypt_with_saved_key", side_effect=fake_decrypt),
        patch.object(account_prepare, "_persist_db_keys", return_value=(True, [])),
    ):
        result = asyncio.run(account_prepare.prepare_account(request))

    assert result["status"] == "success"
    assert result["decrypted_now"] is True
    assert result["reused_saved_key"] is True
    assert result["export_ready"] is True
    assert DB_KEY not in json.dumps(result)


def test_detection_and_decrypt_pages_wire_the_automatic_account_flow() -> None:
    root = Path(__file__).resolve().parents[1]
    detection = (root / "frontend" / "pages" / "detection-result.vue").read_text(encoding="utf-8")
    decrypt = (root / "frontend" / "pages" / "decrypt.vue").read_text(encoding="utf-8")
    api = (root / "frontend" / "composables" / "useApi.js").read_text(encoding="utf-8")

    assert "密钥已保存" in detection
    assert "本地副本已就绪" in detection
    assert "首次获取并解密" in detection
    assert "prepareDetectedAccount(account)" in detection
    assert "<GlobalExportDialog" in detection
    assert "account.auto_prepare === true" in decrypt
    assert "await handleGetDbKey()" in decrypt
    assert "await handleDecrypt()" in decrypt
    assert "await navigateTo('/chat')" in decrypt
    assert "getAccountPrepareState" in api
    assert "prepareAccount" in api
