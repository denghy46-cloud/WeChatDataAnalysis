from __future__ import annotations

import asyncio
import json
import stat

import pytest
from fastapi import HTTPException

from wechat_decrypt_tool import wechat_update_guard as guard
from wechat_decrypt_tool.routers import system


@pytest.fixture
def guard_environment(tmp_path, monkeypatch):
    output = tmp_path / "output"
    cache = tmp_path / "home" / guard.SPARKLE_CACHE_RELATIVE
    app = tmp_path / "Applications" / "WeChat.app"
    app.mkdir(parents=True)
    cache.mkdir(parents=True)
    (cache / "staged-update.pkg").write_bytes(b"staged")

    preferences = {
        "SUEnableAutomaticChecks": True,
        "SUAutomaticallyUpdate": False,
    }
    immutable_paths: set[str] = set()

    def read_default(key: str):
        if key not in preferences:
            return {"exists": False, "value": None}
        return {"exists": True, "value": preferences[key]}

    def write_bool_default(key: str, value: bool):
        preferences[key] = bool(value)

    def restore_default(key: str, saved):
        if saved.get("exists"):
            preferences[key] = saved.get("value")
        else:
            preferences.pop(key, None)

    def fake_chflags(path, flags):
        key = str(path)
        if flags:
            immutable_paths.add(key)
        else:
            immutable_paths.discard(key)

    def cache_is_locked(path):
        if str(path) not in immutable_paths or not path.exists():
            return False
        return not bool(path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

    monkeypatch.setattr(guard.sys, "platform", "darwin")
    monkeypatch.setattr(guard, "WECHAT_APP", app)
    monkeypatch.setattr(guard, "get_output_dir", lambda: output)
    monkeypatch.setattr(guard, "_sparkle_cache", lambda: cache)
    monkeypatch.setattr(guard, "_read_default", read_default)
    monkeypatch.setattr(guard, "_write_bool_default", write_bool_default)
    monkeypatch.setattr(guard, "_restore_default", restore_default)
    monkeypatch.setattr(guard, "_wechat_is_running", lambda: False)
    monkeypatch.setattr(guard, "_wechat_version", lambda: ("4.1.12", "269365"))
    monkeypatch.setattr(guard, "_signature_status", lambda: (True, "腾讯官方签名有效"))
    monkeypatch.setattr(guard.os, "chflags", fake_chflags, raising=False)
    monkeypatch.setattr(guard, "_cache_is_locked", cache_is_locked)
    monkeypatch.setattr(guard, "_remove_legacy_launch_agents", lambda: None)
    monkeypatch.setattr(guard, "_enforcer_active", lambda: True)

    return {
        "output": output,
        "cache": cache,
        "app": app,
        "preferences": preferences,
        "immutable_paths": immutable_paths,
    }


def test_enable_quarantines_staged_update_and_restores_original_preferences(guard_environment) -> None:
    env = guard_environment

    enabled = guard.enable_wechat_update_guard()

    assert enabled["enabled"] is True
    assert enabled["healthy"] is True
    assert enabled["cacheLocked"] is True
    assert enabled["officialSignature"] is True
    assert enabled["enforcementActive"] is True
    assert env["preferences"] == {
        "SUEnableAutomaticChecks": False,
        "SUAutomaticallyUpdate": False,
    }
    assert env["cache"].is_dir()
    assert not (env["cache"] / "staged-update.pkg").exists()

    quarantine = env["output"] / "wechat-update-guard" / "quarantine"
    staged_files = list(quarantine.glob("*/staged-update.pkg"))
    assert len(staged_files) == 1
    assert staged_files[0].read_bytes() == b"staged"

    state_path = env["output"] / "wechat-update-guard" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["enabled"] is True
    assert state["wechatBuild"] == "269365"
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600

    disabled = guard.disable_wechat_update_guard()

    assert disabled["enabled"] is False
    assert disabled["cacheLocked"] is False
    assert env["preferences"] == {
        "SUEnableAutomaticChecks": True,
        "SUAutomaticallyUpdate": False,
    }
    # Stale updater payloads stay quarantined instead of being reactivated.
    assert staged_files[0].exists()


def test_repeated_enable_repairs_preferences_without_replacing_original_snapshot(guard_environment) -> None:
    env = guard_environment
    guard.enable_wechat_update_guard()
    env["preferences"]["SUEnableAutomaticChecks"] = True

    repaired = guard.enable_wechat_update_guard()
    assert repaired["enabled"] is True
    assert env["preferences"]["SUEnableAutomaticChecks"] is False

    guard.disable_wechat_update_guard()
    assert env["preferences"]["SUEnableAutomaticChecks"] is True


def test_running_wechat_blocks_toggle_before_any_mutation(guard_environment, monkeypatch) -> None:
    env = guard_environment
    monkeypatch.setattr(guard, "_wechat_is_running", lambda: True)

    with pytest.raises(guard.WeChatRunningError, match="完整退出微信"):
        guard.enable_wechat_update_guard()

    assert (env["cache"] / "staged-update.pkg").exists()
    assert env["preferences"]["SUEnableAutomaticChecks"] is True
    assert not (env["output"] / "wechat-update-guard" / "state.json").exists()


def test_runtime_enforcer_repairs_preferences_changed_by_wechat(guard_environment) -> None:
    env = guard_environment
    guard.enable_wechat_update_guard()
    env["preferences"]["SUEnableAutomaticChecks"] = True
    env["preferences"]["SUAutomaticallyUpdate"] = True

    guard._enforce_preferences_once()

    assert env["preferences"]["SUEnableAutomaticChecks"] is False
    assert env["preferences"]["SUAutomaticallyUpdate"] is False


def test_non_macos_status_is_explicitly_unsupported(monkeypatch) -> None:
    monkeypatch.setattr(guard.sys, "platform", "linux")

    status = guard.get_wechat_update_guard_status()

    assert status["supported"] is False
    assert status["enabled"] is False
    assert "仅支持 macOS" in status["message"]


def test_system_router_exposes_status_and_toggle(monkeypatch) -> None:
    expected = {"supported": True, "enabled": True}
    monkeypatch.setattr(system, "get_wechat_update_guard_status", lambda: expected)
    monkeypatch.setattr(system, "set_wechat_update_guard", lambda enabled: {**expected, "received": enabled})

    status = asyncio.run(system.wechat_update_guard_status())
    toggled = asyncio.run(
        system.toggle_wechat_update_guard(system.WeChatUpdateGuardToggleRequest(enabled=True))
    )

    assert status == expected
    assert toggled == {**expected, "received": True}


def test_system_router_maps_running_wechat_to_conflict(monkeypatch) -> None:
    def raise_running(_enabled):
        raise guard.WeChatRunningError("请先完整退出微信")

    monkeypatch.setattr(system, "set_wechat_update_guard", raise_running)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(system.toggle_wechat_update_guard(system.WeChatUpdateGuardToggleRequest(enabled=True)))

    assert caught.value.status_code == 409
    assert "完整退出微信" in str(caught.value.detail)
