from __future__ import annotations

import errno
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from .app_paths import get_output_dir


WECHAT_APP = Path("/Applications/WeChat.app")
WECHAT_BUNDLE_ID = "com.tencent.xinWeChat"
SPARKLE_CACHE_RELATIVE = Path("Library/Caches/com.tencent.xinWeChat/org.sparkle-project.Sparkle")
MANAGED_DEFAULTS = ("SUEnableAutomaticChecks", "SUAutomaticallyUpdate")
LEGACY_LAUNCH_AGENT_LABELS = (
    "com.lifearchive.wcda.wechat-update-guard.checks",
    "com.lifearchive.wcda.wechat-update-guard.installs",
)
ENFORCEMENT_INTERVAL_SECONDS = 10
STATE_VERSION = 1
UF_IMMUTABLE = getattr(stat, "UF_IMMUTABLE", 0x00000002)
_OPERATION_LOCK = threading.RLock()
_ENFORCER_STOP = threading.Event()
_ENFORCER_THREAD: threading.Thread | None = None


class WeChatUpdateGuardError(RuntimeError):
    pass


class WeChatRunningError(WeChatUpdateGuardError):
    pass


def _guard_root() -> Path:
    return get_output_dir() / "wechat-update-guard"


def _state_path() -> Path:
    return _guard_root() / "state.json"


def _quarantine_root() -> Path:
    return _guard_root() / "quarantine"


def _sparkle_cache() -> Path:
    return Path.home() / SPARKLE_CACHE_RELATIVE


def _launch_agents_dir() -> Path:
    return Path.home() / "Library/LaunchAgents"


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WeChatUpdateGuardError(f"无法执行系统命令 {args[0]}：{exc}") from exc


def _read_default(key: str) -> dict[str, Any]:
    result = _run(["/usr/bin/defaults", "read", WECHAT_BUNDLE_ID, key])
    if result.returncode != 0:
        return {"exists": False, "value": None}
    raw = result.stdout.strip()
    normalized = raw.casefold()
    if normalized in {"1", "true", "yes"}:
        value: bool | str = True
    elif normalized in {"0", "false", "no"}:
        value = False
    else:
        value = raw
    return {"exists": True, "value": value}


def _write_bool_default(key: str, value: bool) -> None:
    result = _run(
        [
            "/usr/bin/defaults",
            "write",
            WECHAT_BUNDLE_ID,
            key,
            "-bool",
            "true" if value else "false",
        ]
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "未知错误"
        raise WeChatUpdateGuardError(f"无法写入微信更新偏好 {key}：{detail}")


def _restore_default(key: str, saved: dict[str, Any]) -> None:
    if not bool(saved.get("exists")):
        result = _run(["/usr/bin/defaults", "delete", WECHAT_BUNDLE_ID, key])
        # defaults returns non-zero when the key is already absent; that is fine.
        if result.returncode != 0 and _read_default(key).get("exists"):
            detail = result.stderr.strip() or result.stdout.strip() or "未知错误"
            raise WeChatUpdateGuardError(f"无法恢复微信更新偏好 {key}：{detail}")
        return

    value = saved.get("value")
    if isinstance(value, bool):
        _write_bool_default(key, value)
        return

    result = _run(["/usr/bin/defaults", "write", WECHAT_BUNDLE_ID, key, str(value or "")])
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "未知错误"
        raise WeChatUpdateGuardError(f"无法恢复微信更新偏好 {key}：{detail}")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp_path, 0o600)
    temp_path.replace(path)


def _load_state() -> dict[str, Any]:
    path = _state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise WeChatUpdateGuardError(f"更新保护状态文件损坏：{exc}") from exc
    return payload if isinstance(payload, dict) else {}


def _wechat_is_running() -> bool:
    for process in psutil.process_iter(("name", "exe")):
        try:
            info = process.info or {}
            if str(info.get("name") or "").casefold() != "wechat":
                continue
            executable = str(info.get("exe") or "")
            if not executable or "/WeChat.app/" in executable:
                return True
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return False


def _wechat_version() -> tuple[str, str]:
    info_path = WECHAT_APP / "Contents/Info.plist"
    try:
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return "", ""
    version = str(info.get("CFBundleShortVersionString") or "").strip()
    build = str(info.get("CFBundleVersion") or "").strip()
    return version, build


def _signature_status() -> tuple[bool, str]:
    if not WECHAT_APP.is_dir():
        return False, "未找到 /Applications/WeChat.app"
    verify = _run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(WECHAT_APP)])
    if verify.returncode != 0:
        detail = verify.stderr.strip() or verify.stdout.strip() or "签名校验失败"
        return False, detail
    detail = _run(["/usr/bin/codesign", "-dv", "--verbose=4", str(WECHAT_APP)])
    output = f"{detail.stdout}\n{detail.stderr}"
    official = "TeamIdentifier=5A4RE8SF68" in output
    return official, "腾讯官方签名有效" if official else "签名有效，但不是已识别的腾讯官方签名"


def _cache_is_locked(cache: Path) -> bool:
    try:
        current = cache.stat()
    except OSError:
        return False
    immutable = bool(getattr(current, "st_flags", 0) & UF_IMMUTABLE)
    writable_bits = current.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    return immutable and not writable_bits


def _unlock_cache(cache: Path) -> None:
    if not cache.exists():
        return
    try:
        os.chflags(cache, 0)
    except AttributeError as exc:
        raise WeChatUpdateGuardError("当前 Python 运行环境不支持 macOS 文件标志。") from exc
    except FileNotFoundError:
        return
    os.chmod(cache, 0o700)


def _lock_cache(cache: Path) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    os.chmod(cache, 0o500)
    try:
        os.chflags(cache, UF_IMMUTABLE)
    except AttributeError as exc:
        os.chmod(cache, 0o700)
        raise WeChatUpdateGuardError("当前 Python 运行环境不支持 macOS 文件标志。") from exc
    except OSError:
        os.chmod(cache, 0o700)
        raise


def _next_quarantine_path() -> Path:
    root = _quarantine_root()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return root / f"{stamp}-Sparkle"


def _move_directory(source: Path, destination: Path) -> None:
    try:
        source.rename(destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.move(str(source), str(destination))


def _remove_legacy_launch_agents() -> None:
    for label in LEGACY_LAUNCH_AGENT_LABELS:
        _run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
        try:
            (_launch_agents_dir() / f"{label}.plist").unlink(missing_ok=True)
        except OSError:
            pass


def _enforce_preferences_once() -> None:
    if sys.platform != "darwin":
        return
    with _OPERATION_LOCK:
        try:
            state = _load_state()
            if not state.get("enabled"):
                return
            for key in MANAGED_DEFAULTS:
                _write_bool_default(key, False)
        except Exception:
            # The immutable Sparkle cache remains the hard update barrier even
            # if a preference refresh is temporarily unavailable.
            return


def _enforcer_loop() -> None:
    while not _ENFORCER_STOP.wait(ENFORCEMENT_INTERVAL_SECONDS):
        _enforce_preferences_once()


def start_update_guard_enforcer() -> None:
    global _ENFORCER_THREAD
    if sys.platform != "darwin":
        return
    _remove_legacy_launch_agents()
    if _ENFORCER_THREAD is not None and _ENFORCER_THREAD.is_alive():
        return
    _ENFORCER_STOP.clear()
    _enforce_preferences_once()
    _ENFORCER_THREAD = threading.Thread(
        target=_enforcer_loop,
        name="wechat-update-guard",
        daemon=True,
    )
    _ENFORCER_THREAD.start()


def stop_update_guard_enforcer() -> None:
    global _ENFORCER_THREAD
    _ENFORCER_STOP.set()
    thread = _ENFORCER_THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)
    _ENFORCER_THREAD = None


def _enforcer_active() -> bool:
    return _ENFORCER_THREAD is not None and _ENFORCER_THREAD.is_alive()


def _quarantined_paths() -> list[str]:
    root = _quarantine_root()
    if not root.is_dir():
        return []
    try:
        return [str(path) for path in sorted(root.iterdir()) if path.is_dir()]
    except OSError:
        return []


def get_wechat_update_guard_status() -> dict[str, Any]:
    supported = sys.platform == "darwin"
    state: dict[str, Any] = {}
    state_error = ""
    if supported:
        try:
            state = _load_state()
        except WeChatUpdateGuardError as exc:
            state_error = str(exc)

    preferences = {
        key: _read_default(key).get("value") if supported else None
        for key in MANAGED_DEFAULTS
    }
    cache = _sparkle_cache()
    cache_locked = _cache_is_locked(cache) if supported else False
    enforcement_active = _enforcer_active() if supported else False
    requested = bool(state.get("enabled"))
    checks_disabled = preferences.get("SUEnableAutomaticChecks") is False
    installs_disabled = preferences.get("SUAutomaticallyUpdate") is False
    enabled = supported and requested and cache_locked
    version, build = _wechat_version() if supported else ("", "")
    signature_official, signature_message = _signature_status() if supported else (False, "仅支持 macOS")
    quarantined = _quarantined_paths() if supported else []

    if not supported:
        message = "微信更新保护仅支持 macOS。"
    elif state_error:
        message = state_error
    elif enabled:
        message = "微信自动更新已阻止；缓存锁持续生效，WCDA 运行时会自动纠正更新偏好。"
    elif requested:
        message = "更新保护配置不完整，请完整退出微信后重新开启。"
    else:
        message = "微信自动更新保护未开启。"

    return {
        "supported": supported,
        "enabled": enabled,
        "requested": requested,
        "healthy": enabled and signature_official,
        "message": message,
        "wechatRunning": _wechat_is_running() if supported else False,
        "wechatVersion": version,
        "wechatBuild": build,
        "officialSignature": signature_official,
        "signatureMessage": signature_message,
        "preferences": preferences,
        "checksCurrentlyDisabled": checks_disabled,
        "automaticInstallDisabled": installs_disabled,
        "enforcementActive": enforcement_active,
        "enforcementIntervalSeconds": ENFORCEMENT_INTERVAL_SECONDS,
        "cachePath": str(cache),
        "cacheLocked": cache_locked,
        "quarantineCount": len(quarantined),
        "quarantinePaths": quarantined,
    }


def _enable_wechat_update_guard_locked() -> dict[str, Any]:
    if sys.platform != "darwin":
        raise WeChatUpdateGuardError("微信更新保护仅支持 macOS。")
    if _wechat_is_running():
        raise WeChatRunningError("请先完整退出微信，再开启更新保护。")
    if not WECHAT_APP.is_dir():
        raise WeChatUpdateGuardError("未找到 /Applications/WeChat.app。")

    state = _load_state()
    previous_defaults = state.get("previousDefaults")
    if not isinstance(previous_defaults, dict):
        previous_defaults = {key: _read_default(key) for key in MANAGED_DEFAULTS}

    cache = _sparkle_cache()
    moved_to: Path | None = None
    try:
        cache_was_locked = _cache_is_locked(cache)
        if cache.exists() and not cache_was_locked:
            try:
                has_entries = next(cache.iterdir(), None) is not None
            except OSError:
                has_entries = True
            if has_entries:
                moved_to = _next_quarantine_path()
                _move_directory(cache, moved_to)
            else:
                cache.rmdir()

        for key in MANAGED_DEFAULTS:
            _write_bool_default(key, False)
        _remove_legacy_launch_agents()
        if not cache_was_locked:
            _lock_cache(cache)

        version, build = _wechat_version()
        quarantine_paths = list(state.get("quarantinePaths") or [])
        if moved_to is not None:
            quarantine_paths.append(str(moved_to))
        state = {
            "version": STATE_VERSION,
            "enabled": True,
            "enabledAt": datetime.now(timezone.utc).isoformat(),
            "wechatVersion": version,
            "wechatBuild": build,
            "previousDefaults": previous_defaults,
            "quarantinePaths": quarantine_paths,
        }
        _atomic_write_json(_state_path(), state)
    except Exception as exc:
        try:
            _unlock_cache(cache)
            if moved_to is not None and moved_to.exists():
                if cache.exists():
                    shutil.rmtree(cache)
                _move_directory(moved_to, cache)
            for key, saved in previous_defaults.items():
                if isinstance(saved, dict):
                    _restore_default(key, saved)
        except Exception:
            pass
        if isinstance(exc, WeChatUpdateGuardError):
            raise
        raise WeChatUpdateGuardError(f"开启微信更新保护失败：{exc}") from exc

    return get_wechat_update_guard_status()


def enable_wechat_update_guard() -> dict[str, Any]:
    with _OPERATION_LOCK:
        return _enable_wechat_update_guard_locked()


def _disable_wechat_update_guard_locked() -> dict[str, Any]:
    if sys.platform != "darwin":
        raise WeChatUpdateGuardError("微信更新保护仅支持 macOS。")
    state = _load_state()
    if not state.get("enabled"):
        return get_wechat_update_guard_status()
    if _wechat_is_running():
        raise WeChatRunningError("请先完整退出微信，再关闭更新保护。")

    cache = _sparkle_cache()
    previous_defaults = state.get("previousDefaults") or {}
    try:
        _remove_legacy_launch_agents()
        _unlock_cache(cache)
        for key in MANAGED_DEFAULTS:
            saved = previous_defaults.get(key)
            if isinstance(saved, dict):
                _restore_default(key, saved)
    except Exception:
        # Keep the cache protected if preference restoration did not complete.
        try:
            if not _cache_is_locked(cache):
                _lock_cache(cache)
        except Exception:
            pass
        raise

    state["enabled"] = False
    state["disabledAt"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(_state_path(), state)
    return get_wechat_update_guard_status()


def disable_wechat_update_guard() -> dict[str, Any]:
    with _OPERATION_LOCK:
        return _disable_wechat_update_guard_locked()


def set_wechat_update_guard(enabled: bool) -> dict[str, Any]:
    return enable_wechat_update_guard() if enabled else disable_wechat_update_guard()


__all__ = [
    "WeChatRunningError",
    "WeChatUpdateGuardError",
    "disable_wechat_update_guard",
    "enable_wechat_update_guard",
    "get_wechat_update_guard_status",
    "set_wechat_update_guard",
    "start_update_guard_enforcer",
    "stop_update_guard_enforcer",
]
