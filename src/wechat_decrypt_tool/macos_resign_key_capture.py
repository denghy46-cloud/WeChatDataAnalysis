from __future__ import annotations

import argparse
import ctypes
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .app_paths import get_data_dir
from .logging_config import get_logger

logger = get_logger(__name__)

_WECHAT_BUNDLE_ID = "com.tencent.xinWeChat"
_KEY_MARKER = "__WCDA_DATABASE_KEY__="
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TRANSACTION_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_COMMAND_OUTPUT = 256 * 1024
_RENAME_SWAP = 0x00000002


class MacosResignCaptureError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class CodeIdentity:
    identifier: str
    team_identifier: str
    cdhash: str
    authority: str
    designated_requirement: str
    version: str
    build: str


@dataclass
class CaptureTransaction:
    schema_version: int
    transaction_id: str
    state: str
    app_path: str
    staging_root: str
    work_path: str
    recovery_path: str
    original_slot_path: str
    original_identity: dict[str, str]
    swapped: bool = False
    created_at_unix: int = 0
    updated_at_unix: int = 0


def _run(
    args: list[str],
    *,
    timeout: float = 30.0,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MacosResignCaptureError(
            f"无法执行 macOS 密钥捕获命令：{Path(args[0]).name}",
            code="COMMAND_FAILED",
        ) from exc
    if len(result.stdout) + len(result.stderr) > _MAX_COMMAND_OUTPUT:
        raise MacosResignCaptureError(
            "macOS 密钥捕获命令输出异常。", code="COMMAND_OUTPUT_TOO_LARGE"
        )
    if check and result.returncode != 0:
        raise MacosResignCaptureError(
            f"macOS 密钥捕获命令失败：{Path(args[0]).name}",
            code="COMMAND_FAILED",
        )
    return result


def _safe_text(value: object, limit: int = 256) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _info_plist_identity(app_path: Path) -> tuple[str, str, str]:
    try:
        payload = plistlib.loads((app_path / "Contents" / "Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException, TypeError, ValueError) as exc:
        raise MacosResignCaptureError(
            "无法读取微信应用身份。", code="INVALID_WECHAT_BUNDLE"
        ) from exc
    if not isinstance(payload, dict):
        raise MacosResignCaptureError(
            "微信应用身份格式无效。", code="INVALID_WECHAT_BUNDLE"
        )
    return (
        _safe_text(payload.get("CFBundleIdentifier")),
        _safe_text(payload.get("CFBundleShortVersionString")),
        _safe_text(payload.get("CFBundleVersion")),
    )


def inspect_code_identity(app_path: str | Path) -> CodeIdentity:
    path = Path(app_path).expanduser().resolve(strict=True)
    identifier, version, build = _info_plist_identity(path)
    details = _run(["/usr/bin/codesign", "-d", "--verbose=4", str(path)], check=True)
    output = (details.stdout + details.stderr).decode("utf-8", errors="replace")
    requirement = _run(["/usr/bin/codesign", "-d", "-r-", str(path)], check=True)
    requirement_text = (requirement.stdout + requirement.stderr).decode(
        "utf-8", errors="replace"
    )

    def field(name: str) -> str:
        match = re.search(rf"(?m)^{re.escape(name)}=([^\r\n]+)$", output)
        return _safe_text(match.group(1)) if match else ""

    authority_match = re.search(r"(?m)^Authority=([^\r\n]+)$", output)
    designated_match = re.search(r"(?m)^designated => (.+)$", requirement_text)
    return CodeIdentity(
        identifier=identifier or field("Identifier"),
        team_identifier=field("TeamIdentifier"),
        cdhash=field("CDHash"),
        authority=_safe_text(authority_match.group(1)) if authority_match else "",
        designated_requirement=(
            _safe_text(designated_match.group(1), 1024) if designated_match else ""
        ),
        version=version,
        build=build,
    )


def _verify_official_identity(identity: CodeIdentity) -> None:
    if identity.identifier != _WECHAT_BUNDLE_ID:
        raise MacosResignCaptureError(
            "目标应用不是微信官方 macOS 客户端。", code="INVALID_WECHAT_BUNDLE"
        )
    if not identity.team_identifier or not identity.cdhash:
        raise MacosResignCaptureError(
            "无法确认微信官方代码签名。", code="INVALID_WECHAT_SIGNATURE"
        )
    if "Tencent" not in identity.authority:
        raise MacosResignCaptureError(
            "微信不是腾讯 Developer ID 签名版本。", code="INVALID_WECHAT_SIGNATURE"
        )
    if "anchor apple" not in identity.designated_requirement:
        raise MacosResignCaptureError(
            "微信指定要求不是 Apple 信任链。", code="INVALID_WECHAT_SIGNATURE"
        )


def _verify_bundle(path: Path) -> None:
    _run(
        [
            "/usr/bin/codesign",
            "--verify",
            "--deep",
            "--strict",
            "--verbose=2",
            str(path),
        ],
        timeout=120.0,
    )


def _capture_state_dir() -> Path:
    return get_data_dir() / "macos-key-capture"


def capture_journal_path() -> Path:
    return _capture_state_dir() / "transaction.json"


def _write_journal(transaction: CaptureTransaction) -> None:
    directory = _capture_state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    transaction.updated_at_unix = int(time.time())
    destination = capture_journal_path()
    temporary = destination.with_name(
        f".{destination.name}.{transaction.transaction_id}.tmp"
    )
    payload = json.dumps(asdict(transaction), ensure_ascii=True, sort_keys=True).encode(
        "utf-8"
    )
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _load_journal() -> CaptureTransaction | None:
    path = capture_journal_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MacosResignCaptureError(
            "无法读取 macOS 微信恢复状态。", code="RECOVERY_STATE_UNREADABLE"
        ) from exc
    if len(raw) > 64 * 1024:
        raise MacosResignCaptureError(
            "macOS 微信恢复状态异常。", code="RECOVERY_STATE_INVALID"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
        transaction = CaptureTransaction(**payload)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise MacosResignCaptureError(
            "macOS 微信恢复状态无效。", code="RECOVERY_STATE_INVALID"
        ) from exc
    if transaction.schema_version != 1 or not _SAFE_TRANSACTION_RE.fullmatch(
        transaction.transaction_id
    ):
        raise MacosResignCaptureError(
            "macOS 微信恢复状态版本无效。", code="RECOVERY_STATE_INVALID"
        )
    return transaction


def _identity_matches(identity: CodeIdentity, expected: dict[str, str]) -> bool:
    return bool(
        identity.identifier == expected.get("identifier")
        and identity.team_identifier == expected.get("team_identifier")
        and identity.cdhash == expected.get("cdhash")
        and identity.designated_requirement == expected.get("designated_requirement")
    )


def _validate_transaction_paths(
    transaction: CaptureTransaction,
) -> tuple[Path, Path, Path, Path, Path]:
    app_path = Path(transaction.app_path).expanduser()
    staging_root = Path(transaction.staging_root).expanduser()
    work_path = Path(transaction.work_path).expanduser()
    recovery_path = Path(transaction.recovery_path).expanduser()
    original_slot = Path(transaction.original_slot_path).expanduser()
    if not app_path.is_absolute() or app_path.suffix.lower() != ".app":
        raise MacosResignCaptureError(
            "恢复状态中的微信路径无效。", code="RECOVERY_STATE_INVALID"
        )
    expected_prefix = f".{app_path.stem}.wcda-key-capture-"
    if (
        not staging_root.is_absolute()
        or staging_root.parent != app_path.parent
        or not staging_root.name.startswith(expected_prefix)
        or work_path != staging_root / "working.app"
        or recovery_path != staging_root / "recovery.app"
        or original_slot != staging_root / "original-slot.app"
        or original_slot.parent != staging_root
    ):
        raise MacosResignCaptureError(
            "恢复状态中的工作目录无效。", code="RECOVERY_STATE_INVALID"
        )
    return app_path, staging_root, work_path, recovery_path, original_slot


def _atomic_swap(first: Path, second: Path) -> None:
    if sys.platform != "darwin":
        raise MacosResignCaptureError(
            "临时重签捕获仅支持 macOS。", code="UNSUPPORTED_PLATFORM"
        )
    libc = ctypes.CDLL(None, use_errno=True)
    renamex_np = getattr(libc, "renamex_np", None)
    if renamex_np is None:
        raise MacosResignCaptureError(
            "当前 macOS 不支持原子应用换位。", code="ATOMIC_SWAP_UNAVAILABLE"
        )
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    result = renamex_np(os.fsencode(first), os.fsencode(second), _RENAME_SWAP)
    if result != 0:
        error_number = ctypes.get_errno()
        raise MacosResignCaptureError(
            f"微信应用原子换位失败（errno={error_number}）。",
            code="ATOMIC_SWAP_FAILED",
        )


def _safe_remove_staging(staging_root: Path, app_path: Path) -> None:
    expected_prefix = f".{app_path.stem}.wcda-key-capture-"
    if staging_root.parent != app_path.parent or not staging_root.name.startswith(
        expected_prefix
    ):
        raise MacosResignCaptureError(
            "拒绝清理未经验证的工作目录。", code="UNSAFE_CLEANUP_PATH"
        )
    if staging_root.is_symlink():
        raise MacosResignCaptureError(
            "拒绝清理符号链接形式的工作目录。", code="UNSAFE_CLEANUP_PATH"
        )
    if staging_root.exists():
        shutil.rmtree(staging_root)


def recover_official_wechat(*, cleanup: bool = True) -> dict[str, Any]:
    transaction = _load_journal()
    if transaction is None:
        return {"recovered": False, "state": "idle"}
    app_path, staging_root, _work_path, recovery_path, original_slot = (
        _validate_transaction_paths(transaction)
    )
    expected = transaction.original_identity
    current_identity: CodeIdentity | None = None
    try:
        current_identity = inspect_code_identity(app_path)
    except (OSError, MacosResignCaptureError):
        current_identity = None

    # WeChat may install an official Sparkle update while it is terminating.
    # Before the atomic swap, that is not a recovery failure: our transaction
    # has never replaced the canonical app.  Accept the newer app only after
    # independently verifying its Tencent/Apple identity and full bundle seal.
    if not transaction.swapped and current_identity is not None:
        try:
            _verify_official_identity(current_identity)
            _verify_bundle(app_path)
        except MacosResignCaptureError:
            pass
        else:
            transaction.state = "official_app_intact"
            _write_journal(transaction)
            if cleanup:
                _safe_remove_staging(staging_root, app_path)
                capture_journal_path().unlink(missing_ok=True)
            return {
                "recovered": True,
                "state": "official_app_intact",
                "version": current_identity.version,
                "build": current_identity.build,
                "team_identifier": current_identity.team_identifier,
            }

    if current_identity is None or not _identity_matches(current_identity, expected):
        restore_slot: Path | None = None
        for candidate in (original_slot, recovery_path):
            if not candidate.exists():
                continue
            candidate_identity = inspect_code_identity(candidate)
            if _identity_matches(candidate_identity, expected):
                restore_slot = candidate
                break
        if restore_slot is None:
            raise MacosResignCaptureError(
                "原版微信恢复副本不存在或身份不匹配，请勿启动当前微信并重新安装官方微信。",
                code="RECOVERY_SLOT_MISSING",
            )
        _terminate_wechat_processes(app_path=app_path, timeout=8.0)
        if app_path.exists():
            _atomic_swap(app_path, restore_slot)
        else:
            os.rename(restore_slot, app_path)
        current_identity = inspect_code_identity(app_path)

    if not _identity_matches(current_identity, expected):
        raise MacosResignCaptureError(
            "腾讯官方微信恢复校验失败。", code="RECOVERY_VERIFY_FAILED"
        )
    _verify_bundle(app_path)
    transaction.swapped = False
    transaction.state = "restored"
    _write_journal(transaction)
    if cleanup:
        _safe_remove_staging(staging_root, app_path)
        capture_journal_path().unlink(missing_ok=True)
    return {
        "recovered": True,
        "state": "restored",
        "version": current_identity.version,
        "build": current_identity.build,
        "team_identifier": current_identity.team_identifier,
    }


def _clone_bundle(source: Path, destination: Path) -> None:
    if destination.exists():
        raise MacosResignCaptureError(
            "macOS 密钥捕获工作目录已存在。", code="STAGING_ALREADY_EXISTS"
        )
    _run(
        ["/bin/cp", "-cRp", str(source), str(destination)],
        timeout=300.0,
    )


def _sign_working_copy(work_path: Path, staging_root: Path) -> None:
    entitlements_path = staging_root / "capture-entitlements.plist"
    entitlements = {
        "com.apple.security.get-task-allow": True,
        "com.apple.security.cs.disable-library-validation": True,
    }
    descriptor = os.open(entitlements_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb", closefd=True) as stream:
        stream.write(plistlib.dumps(entitlements, fmt=plistlib.FMT_XML, sort_keys=True))
        stream.flush()
        os.fsync(stream.fileno())
    _run(
        [
            "/usr/bin/codesign",
            "--force",
            "--sign",
            "-",
            "--identifier",
            _WECHAT_BUNDLE_ID,
            "--entitlements",
            str(entitlements_path),
            "--options",
            "runtime",
            "--timestamp=none",
            str(work_path),
        ],
        timeout=300.0,
    )
    details = _run(
        [
            "/usr/bin/codesign",
            "-d",
            "--entitlements",
            ":-",
            str(work_path),
        ]
    )
    output = details.stdout + details.stderr
    xml_start = output.find(b"<?xml")
    xml_end = output.rfind(b"</plist>")
    try:
        signed_entitlements = plistlib.loads(output[xml_start : xml_end + 8])
    except (plistlib.InvalidFileException, ValueError, TypeError):
        signed_entitlements = {}
    if signed_entitlements.get("com.apple.security.get-task-allow") is not True:
        raise MacosResignCaptureError(
            "临时微信缺少调试许可。", code="TEMP_SIGNATURE_INVALID"
        )


def _wechat_main_executable(app_path: Path) -> Path:
    identifier, _version, _build = _info_plist_identity(app_path)
    if identifier != _WECHAT_BUNDLE_ID:
        raise MacosResignCaptureError(
            "目标应用不是微信。", code="INVALID_WECHAT_BUNDLE"
        )
    info = plistlib.loads((app_path / "Contents" / "Info.plist").read_bytes())
    executable_name = _safe_text(info.get("CFBundleExecutable"))
    executable = app_path / "Contents" / "MacOS" / executable_name
    if not executable.is_file():
        raise MacosResignCaptureError(
            "微信主程序不存在。", code="INVALID_WECHAT_BUNDLE"
        )
    return executable


def _wechat_pids(app_path: Path) -> tuple[int, ...]:
    executable = str(_wechat_main_executable(app_path))
    result = _run(["/bin/ps", "-axo", "pid=,comm="], check=True)
    pids: list[int] = []
    for raw_line in result.stdout.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        pieces = line.split(None, 1)
        if len(pieces) != 2 or pieces[1] != executable:
            continue
        try:
            pids.append(int(pieces[0]))
        except ValueError:
            continue
    return tuple(sorted({pid for pid in pids if pid > 0}))


def _terminate_wechat_processes(*, app_path: Path, timeout: float = 12.0) -> None:
    pids = _wechat_pids(app_path)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _wechat_pids(app_path):
        time.sleep(0.1)
    for pid in _wechat_pids(app_path):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _wait_for_wechat_pid(app_path: Path, timeout: float = 30.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pids = _wechat_pids(app_path)
        if pids:
            return pids[0]
        time.sleep(0.1)
    raise MacosResignCaptureError(
        "临时微信未能启动。", code="TEMP_WECHAT_START_FAILED", retryable=True
    )


def _target_database(db_storage_path: Path) -> tuple[Path, bytes]:
    candidates = [
        db_storage_path / "session" / "session.db",
        db_storage_path / "session.db",
    ]
    candidates.extend(sorted((db_storage_path / "message").glob("message_*.db")))
    candidates.extend(sorted(db_storage_path.glob("message_*.db")))
    for path in candidates:
        try:
            with path.open("rb", buffering=0) as stream:
                page_prefix = stream.read(16)
        except OSError:
            continue
        if len(page_prefix) == 16 and not page_prefix.startswith(b"SQLite format 3"):
            return path, page_prefix
    raise MacosResignCaptureError(
        "活动微信数据库不可读或不是加密数据库。", code="ACTIVE_DATABASE_NOT_FOUND"
    )


_LLDB_SCRIPT = r"""
import lldb
import os
import sys

MARKER = "__WCDA_DATABASE_KEY__="
TARGET_SALT = bytes.fromhex(os.environ.get("WCDA_TARGET_SALT_HEX", ""))

def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value and value.IsValid() else 0

def handle_pbkdf2(frame, _bp_loc, _dict):
    try:
        algorithm = _reg(frame, "x0")
        password_address = _reg(frame, "x1")
        password_length = _reg(frame, "x2")
        salt_address = _reg(frame, "x3")
        salt_length = _reg(frame, "x4")
        prf = _reg(frame, "x5")
        rounds = _reg(frame, "x6")
        if algorithm != 2 or password_length != 32 or salt_length != 16:
            return False
        if prf != 5 or rounds != 256000 or not password_address or not salt_address:
            return False
        process = frame.GetThread().GetProcess()
        error = lldb.SBError()
        salt = process.ReadMemory(salt_address, 16, error)
        if not error.Success() or salt != TARGET_SALT:
            return False
        candidate = process.ReadMemory(password_address, 32, error)
        if not error.Success() or len(candidate) != 32:
            return False
        sys.stdout.write(MARKER + candidate.hex() + "\n")
        sys.stdout.flush()
        return True
    except Exception:
        return False
"""


def _lldb_capture(
    *,
    pid: int,
    target_salt: bytes,
    staging_root: Path,
    timeout: float,
    cancel_event: Any = None,
) -> str:
    script_path = staging_root / "wcda_lldb_capture.py"
    command_path = staging_root / "wcda_lldb_commands.txt"
    script_path.write_text(_LLDB_SCRIPT, encoding="utf-8")
    os.chmod(script_path, 0o600)
    command_path.write_text(
        "\n".join(
            [
                f'command script import "{script_path}"',
                f"process attach --pid {pid}",
                "breakpoint set --name CCKeyDerivationPBKDF",
                "breakpoint command add -F wcda_lldb_capture.handle_pbkdf2 1",
                "process continue",
                "process detach",
                "quit",
                "",
            ]
        ),
        encoding="utf-8",
    )
    os.chmod(command_path, 0o600)
    environment = dict(os.environ)
    environment["WCDA_TARGET_SALT_HEX"] = target_salt.hex()
    process = subprocess.Popen(
        ["/usr/bin/xcrun", "lldb", "--batch", "-s", str(command_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                process.send_signal(signal.SIGINT)
                raise MacosResignCaptureError(
                    "macOS 临时重签密钥获取已停止。",
                    code="CANCELLED",
                    retryable=True,
                )
            if time.monotonic() >= deadline:
                process.send_signal(signal.SIGINT)
                raise MacosResignCaptureError(
                    "等待微信重新登录并派生数据库密钥超时。",
                    code="CAPTURE_TIMEOUT",
                    retryable=True,
                )
            time.sleep(0.1)
        stdout, stderr = process.communicate(timeout=5)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        raise
    if len(stdout) + len(stderr) > _MAX_COMMAND_OUTPUT:
        raise MacosResignCaptureError("LLDB 输出异常。", code="LLDB_OUTPUT_TOO_LARGE")
    candidate = ""
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        if line.startswith(_KEY_MARKER):
            value = line[len(_KEY_MARKER) :].strip().lower()
            if _HEX_64_RE.fullmatch(value):
                candidate = value
                break
    if candidate:
        return candidate
    diagnostic = (stdout + stderr).decode("utf-8", errors="replace").lower()
    if "attach failed" in diagnostic or "not permitted" in diagnostic:
        raise MacosResignCaptureError(
            "LLDB 无法附加临时微信，请确认开发者工具权限。",
            code="LLDB_ATTACH_DENIED",
            retryable=True,
        )
    if "no locations" in diagnostic or "pending breakpoint" in diagnostic:
        raise MacosResignCaptureError(
            "当前微信没有可用的系统 PBKDF2 断点。",
            code="PBKDF2_BREAKPOINT_UNAVAILABLE",
        )
    raise MacosResignCaptureError(
        "没有捕获到与活动数据库匹配的密钥。",
        code="KEY_NOT_CAPTURED",
        retryable=True,
    )


def _preflight(app_path: Path, db_storage_path: Path) -> tuple[CodeIdentity, bytes]:
    if sys.platform != "darwin":
        raise MacosResignCaptureError(
            "临时重签密钥捕获仅支持 macOS。", code="UNSUPPORTED_PLATFORM"
        )
    if os.uname().machine != "arm64":
        raise MacosResignCaptureError(
            "当前实现只支持 Apple Silicon Mac。", code="UNSUPPORTED_ARCHITECTURE"
        )
    if not app_path.is_dir() or app_path.suffix.lower() != ".app":
        raise MacosResignCaptureError(
            "微信应用路径无效。", code="INVALID_WECHAT_BUNDLE"
        )
    if not os.access(app_path.parent, os.W_OK):
        raise MacosResignCaptureError(
            "微信所在目录不可写；当前版本不会保存或代填管理员密码。",
            code="APPLICATIONS_DIRECTORY_NOT_WRITABLE",
        )
    if shutil.which("xcrun") is None:
        raise MacosResignCaptureError(
            "未安装 Xcode Command Line Tools/LLDB。", code="LLDB_UNAVAILABLE"
        )
    _run(["/usr/bin/xcrun", "--find", "lldb"])
    identity = inspect_code_identity(app_path)
    _verify_official_identity(identity)
    _verify_bundle(app_path)
    _database, salt = _target_database(db_storage_path)
    return identity, salt


def capture_database_key_with_temporary_resign(
    *,
    wechat_app_path: str | Path = "/Applications/WeChat.app",
    db_storage_path: str | Path,
    timeout_seconds: float = 300.0,
    cancel_event: Any = None,
    validator: Callable[[str | Path, str], dict[str, Any]] | None = None,
    lldb_capture: Callable[..., str] = _lldb_capture,
) -> dict[str, Any]:
    app_path = Path(wechat_app_path).expanduser().resolve(strict=True)
    database_root = Path(db_storage_path).expanduser().resolve(strict=True)
    if capture_journal_path().exists():
        recover_official_wechat(cleanup=True)
    _terminate_wechat_processes(app_path=app_path)
    # Allow an already-downloaded official Sparkle update to finish replacing
    # the bundle before its identity is recorded as the recovery baseline.
    time.sleep(2.0)
    original_identity, target_salt = _preflight(app_path, database_root)
    transaction_id = uuid.uuid4().hex
    staging_root = (
        app_path.parent / f".{app_path.stem}.wcda-key-capture-{transaction_id}"
    )
    work_path = staging_root / "working.app"
    recovery_path = staging_root / "recovery.app"
    original_slot_path = staging_root / "original-slot.app"
    staging_root.mkdir(mode=0o700)
    transaction = CaptureTransaction(
        schema_version=1,
        transaction_id=transaction_id,
        state="preparing",
        app_path=str(app_path),
        staging_root=str(staging_root),
        work_path=str(work_path),
        recovery_path=str(recovery_path),
        original_slot_path=str(original_slot_path),
        original_identity=asdict(original_identity),
        created_at_unix=int(time.time()),
    )
    _write_journal(transaction)
    captured_key = ""
    try:
        _clone_bundle(app_path, recovery_path)
        _verify_bundle(recovery_path)
        if not _identity_matches(
            inspect_code_identity(recovery_path), transaction.original_identity
        ):
            raise MacosResignCaptureError(
                "腾讯官方微信恢复副本校验失败。", code="RECOVERY_COPY_INVALID"
            )
        _clone_bundle(app_path, work_path)
        _sign_working_copy(work_path, staging_root)
        os.rename(work_path, original_slot_path)
        transaction.state = "ready_to_swap"
        _write_journal(transaction)
        _atomic_swap(app_path, original_slot_path)
        transaction.swapped = True
        transaction.state = "temporary_wechat_active"
        _write_journal(transaction)
        _run(["/usr/bin/open", "-n", str(app_path)], timeout=15.0)
        pid = _wait_for_wechat_pid(app_path)
        transaction.state = "capturing"
        _write_journal(transaction)
        captured_key = lldb_capture(
            pid=pid,
            target_salt=target_salt,
            staging_root=staging_root,
            timeout=float(timeout_seconds),
            cancel_event=cancel_event,
        )
        if not _HEX_64_RE.fullmatch(captured_key):
            raise MacosResignCaptureError(
                "LLDB 返回的数据库密钥格式无效。", code="INVALID_CAPTURED_KEY"
            )
        if validator is None:
            from .wechat_decrypt import validate_realtime_database_key

            validator = validate_realtime_database_key
        validation = validator(database_root, captured_key)
        if validation.get("valid") is not True:
            raise MacosResignCaptureError(
                "捕获到的候选未通过消息库和会话库校验。",
                code="CAPTURE_KEY_MISMATCH",
                retryable=True,
            )
        transaction.state = "key_verified"
        _write_journal(transaction)
        return {
            "db_key": captured_key,
            "method": "macos_resign_lldb",
            "validation": {
                "verified_roles": list(validation.get("verified_roles") or []),
                "modes": dict(validation.get("modes") or {}),
            },
            "wechat_version": original_identity.version,
            "wechat_build": original_identity.build,
        }
    finally:
        try:
            recover_official_wechat(cleanup=True)
        except Exception as recovery_error:
            logger.exception(
                "[macos-resign-key] official WeChat recovery requires attention; "
                "database key was not logged"
            )
            if isinstance(recovery_error, MacosResignCaptureError):
                raise
            raise MacosResignCaptureError(
                "腾讯官方微信自动恢复失败，请勿启动微信并立即执行恢复。",
                code="RECOVERY_FAILED",
            ) from recovery_error


def inspect_resign_capture_capability(
    *,
    wechat_app_path: str | Path = "/Applications/WeChat.app",
    db_storage_path: str | Path | None = None,
) -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"available": False, "code": "UNSUPPORTED_PLATFORM"}
    app_path = Path(wechat_app_path).expanduser()
    result: dict[str, Any] = {
        "available": False,
        "app_path": str(app_path),
        "apple_silicon": os.uname().machine == "arm64",
        "directory_writable": os.access(app_path.parent, os.W_OK),
        "lldb_available": shutil.which("xcrun") is not None,
        "pending_recovery": capture_journal_path().exists(),
    }
    try:
        developer_tools = _run(["/usr/sbin/DevToolsSecurity", "-status"], check=False)
        result["developer_tools_enabled"] = (
            b"enabled" in (developer_tools.stdout + developer_tools.stderr).lower()
        )
    except MacosResignCaptureError:
        result["developer_tools_enabled"] = False
    try:
        identity = inspect_code_identity(app_path)
        _verify_official_identity(identity)
        result.update(
            {
                "wechat_version": identity.version,
                "wechat_build": identity.build,
                "team_identifier": identity.team_identifier,
                "official_signature": True,
            }
        )
        if db_storage_path:
            _database, _salt = _target_database(
                Path(db_storage_path).expanduser().resolve(strict=True)
            )
            result["encrypted_database_ready"] = True
        else:
            result["encrypted_database_ready"] = None
        result["available"] = bool(
            result["apple_silicon"]
            and result["directory_writable"]
            and result["lldb_available"]
            and result.get("official_signature")
            and result.get("encrypted_database_ready") is not False
        )
    except (OSError, MacosResignCaptureError) as exc:
        result["code"] = getattr(exc, "code", "PREFLIGHT_FAILED")
    return result


__all__ = [
    "CaptureTransaction",
    "CodeIdentity",
    "MacosResignCaptureError",
    "capture_database_key_with_temporary_resign",
    "capture_journal_path",
    "inspect_code_identity",
    "inspect_resign_capture_capability",
    "recover_official_wechat",
]


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or recover WCDA's controlled macOS WeChat key-capture transaction."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    status_parser = subparsers.add_parser(
        "status", help="run read-only capability checks"
    )
    status_parser.add_argument("--wechat-app", default="/Applications/WeChat.app")
    status_parser.add_argument("--db-storage")
    subparsers.add_parser("recover", help="restore the verified official WeChat bundle")
    arguments = parser.parse_args()
    try:
        if arguments.command == "recover":
            result = recover_official_wechat(cleanup=True)
        else:
            result = inspect_resign_capture_capability(
                wechat_app_path=arguments.wechat_app,
                db_storage_path=arguments.db_storage,
            )
    except MacosResignCaptureError as exc:
        print(json.dumps({"ok": False, "code": exc.code}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, "result": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
