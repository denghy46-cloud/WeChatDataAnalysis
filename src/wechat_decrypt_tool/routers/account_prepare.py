from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..account_identity import canonical_account_name
from ..app_paths import get_output_databases_dir
from ..key_store import normalize_key_store_path
from ..logging_config import get_logger
from ..path_fix import PathFixRoute
from ..wechat_decrypt import decrypt_wechat_databases
from .decrypt import (
    _acquire_decrypt_account_guards,
    _persist_db_keys,
    _release_decrypt_account_guards,
    _resolve_decrypt_guard_accounts,
)
from .keys import get_saved_keys


router = APIRouter(route_class=PathFixRoute)
logger = get_logger(__name__)

_DB_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SQLITE_HEADER = b"SQLite format 3\x00"


class AccountPrepareRequest(BaseModel):
    account: str = Field(..., description="Detected WeChat account name")
    db_storage_path: str = Field(..., description="Absolute path to the account db_storage directory")


def _is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as source:
            return source.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


def _account_output_candidates(account: str) -> list[Path]:
    base = get_output_databases_dir()
    requested = str(account or "").strip()
    canonical = canonical_account_name(requested)
    result: list[Path] = []
    seen: set[str] = set()

    for candidate in (base / requested, base / canonical):
        key = str(candidate)
        if requested and key not in seen:
            seen.add(key)
            result.append(candidate)

    try:
        children = list(base.iterdir()) if base.is_dir() else []
    except OSError:
        children = []
    for candidate in children:
        if not candidate.is_dir() or canonical_account_name(candidate.name) != canonical:
            continue
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def _source_matches(account_dir: Path, db_storage_path: str) -> bool:
    source_file = account_dir / "_source.json"
    if not source_file.is_file():
        # Imported/legacy decrypted directories may not carry source metadata.
        return True
    try:
        source = json.loads(source_file.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(source, dict):
        return False

    requested = normalize_key_store_path(db_storage_path)
    stored = normalize_key_store_path(source.get("db_storage_path"))
    if requested and stored:
        return requested == stored
    return True


def _decrypted_account_state(account: str, db_storage_path: str) -> tuple[bool, str]:
    for account_dir in _account_output_candidates(account):
        if not account_dir.is_dir() or not _source_matches(account_dir, db_storage_path):
            continue
        if not _is_sqlite_file(account_dir / "contact.db"):
            continue
        if not _is_sqlite_file(account_dir / "session.db"):
            continue
        try:
            message_ready = any(
                item.is_file()
                and item.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".db3"}
                and item.name.lower().startswith("message")
                and _is_sqlite_file(item)
                for item in account_dir.iterdir()
            )
        except OSError:
            message_ready = False
        if message_ready:
            return True, str(account_dir.resolve())
    return False, ""


async def _account_state(account: str, db_storage_path: str) -> tuple[dict[str, Any], str]:
    saved = await get_saved_keys(account=account, db_storage_path=db_storage_path)
    keys = saved.get("keys") if isinstance(saved, dict) else {}
    keys = keys if isinstance(keys, dict) else {}
    db_key = str(keys.get("db_key") or "").strip()
    key_saved = bool(_DB_KEY_RE.fullmatch(db_key))
    decrypted_ready, output_dir = _decrypted_account_state(account, db_storage_path)
    state = {
        "status": "success",
        "account": canonical_account_name(account) or str(account or "").strip(),
        "key_saved": key_saved,
        "key_updated_at": str(keys.get("updated_at") or "").strip(),
        "decrypted_ready": decrypted_ready,
        "output_dir": output_dir,
        "export_ready": decrypted_ready,
        "next_action": (
            "open_chat" if key_saved and decrypted_ready
            else "decrypt_saved_key" if key_saved
            else "capture_key"
        ),
    }
    return state, db_key


@router.get("/api/account/prepare", summary="Inspect account key and decrypted-copy readiness")
async def get_account_prepare_state(account: str, db_storage_path: str):
    state, _db_key = await _account_state(account, db_storage_path)
    return state


def _decrypt_with_saved_key(db_storage_path: str, db_key: str) -> dict[str, Any]:
    guard_accounts = _resolve_decrypt_guard_accounts(db_storage_path)
    guards = _acquire_decrypt_account_guards(guard_accounts, reason="account:prepare")
    try:
        return decrypt_wechat_databases(db_storage_path=db_storage_path, key=db_key)
    finally:
        _release_decrypt_account_guards(guards, reason="account:prepare")


@router.post("/api/account/prepare", summary="Prepare an account using its matching saved key")
async def prepare_account(request: AccountPrepareRequest):
    account = str(request.account or "").strip()
    db_storage_path = str(request.db_storage_path or "").strip()
    if not account:
        raise HTTPException(status_code=400, detail="缺少微信账号。")
    storage = Path(db_storage_path).expanduser()
    if not storage.is_absolute() or not storage.is_dir():
        raise HTTPException(status_code=400, detail="数据库路径不存在或不是绝对目录。")

    state, db_key = await _account_state(account, db_storage_path)
    if state["key_saved"] and state["decrypted_ready"]:
        return {**state, "reused_saved_key": True, "message": "账号本地副本已就绪。"}
    if not state["key_saved"]:
        return {
            **state,
            "status": "needs_key_capture",
            "reused_saved_key": False,
            "message": "该账号尚未保存匹配的数据库密钥。",
        }

    logger.info("[account-prepare] decrypting with matching saved key account=%s", account)
    result = await asyncio.to_thread(_decrypt_with_saved_key, db_storage_path, db_key)
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=str(result.get("message") or "使用已保存密钥解密失败。"))

    persisted, persistence_errors = _persist_db_keys(result.get("account_results"), db_key)
    ready, output_dir = _decrypted_account_state(account, db_storage_path)
    if not ready:
        raise HTTPException(status_code=500, detail="解密完成，但未生成可查询的账号数据库副本。")

    return {
        "status": "success",
        "account": canonical_account_name(account) or account,
        "key_saved": True,
        "decrypted_ready": True,
        "export_ready": True,
        "next_action": "open_chat",
        "output_dir": output_dir,
        "reused_saved_key": True,
        "decrypted_now": True,
        "success_count": int(result.get("successful_count") or 0),
        "failure_count": int(result.get("failed_count") or 0),
        "db_key_persisted": persisted,
        "db_key_persistence_errors": persistence_errors,
        "message": str(result.get("message") or "账号已自动解密。"),
    }
