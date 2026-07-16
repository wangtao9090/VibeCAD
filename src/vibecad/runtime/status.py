"""运行时状态机、哨兵就绪探测、健康检查、跨进程文件锁。纯 stdlib。"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vibecad.runtime import paths, spec

# import FreeCAD 前的跨平台兜底（Windows 把 conda Library/bin 注入 PATH；macOS/Linux 无操作）
_PREP = (
    "import os,sys\n"
    "if sys.platform=='win32':\n"
    "    _b=os.path.join(sys.prefix,'Library','bin')\n"
    "    os.environ['PATH']=_b+os.pathsep+os.environ.get('PATH','')\n"
    "    try:\n"
    "        os.add_dll_directory(_b)\n"
    "    except Exception:\n"
    "        pass\n"
    "    _mods=[_b, os.path.join(sys.prefix,'Library','lib')]\n"
    "else:\n"
    "    _mods=[os.path.join(sys.prefix,'lib')]\n"
    "for _m in _mods:\n"
    "    if _m not in sys.path:\n"
    "        sys.path.insert(0, _m)\n"
)
_HEALTH_SNIPPET = _PREP + "import FreeCAD, Part\n"
# 托管 env 复用前必须精确匹配 pins；不能把“可 import 的其他 FreeCAD/Python”盖章成
# 当前 receipt，否则后续启动会跳过真正的引擎升级。
_ENGINE_SNIPPET = (
    _PREP
    + "import FreeCAD, Part, sys\n"
    + f"if sys.version_info[:2] != {spec.PYTHON_VERSION!r}:\n"
    + "    raise RuntimeError('managed runtime Python version mismatch')\n"
    + f"if tuple(map(int, FreeCAD.Version()[:3])) != {spec.FREECAD_VERSION!r}:\n"
    + "    raise RuntimeError('managed runtime FreeCAD version mismatch')\n"
)
# 更严就绪校验：精确引擎 + vibecad.server（连带 mcp）+ server 版本。
_VERIFY_SNIPPET = (
    _ENGINE_SNIPPET
    + "import vibecad, vibecad.server\n"
    + f"if vibecad.__version__ != {spec.VIBECAD_VERSION!r}:\n"
    + "    raise RuntimeError('vibecad runtime version mismatch: ' + vibecad.__version__)\n"
)
_STALE_SECONDS = 3600


class Phase(StrEnum):
    NOT_STARTED = "not_started"
    DOWNLOADING_MICROMAMBA = "downloading_micromamba"
    CREATING_ENV = "creating_env"
    INSTALLING_PIP = "installing_pip"
    VERIFYING = "verifying"
    READY = "ready"
    FAILED = "failed"


class ReceiptState(StrEnum):
    """廉价 receipt 分类；installer 据此区分 pip-only 同步与完整建 env。"""

    MISSING = "missing"
    LEGACY = "legacy"
    CURRENT = "current"
    SERVER_MISMATCH = "server_mismatch"
    INCOMPATIBLE = "incompatible"


class RecoveryKind(StrEnum):
    """启动时只靠 receipt + Python 路径即可判定的保守恢复类别。"""

    READY = "ready"
    UPGRADE_REQUIRED = "upgrade_required"
    REPAIR_REQUIRED = "repair_required"


@dataclass
class RuntimeStatus:
    phase: Phase = Phase.NOT_STARTED
    percent: float = 0.0
    message: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "phase": self.phase.value,
            "percent": self.percent,
            "message": self.message,
            "error": self.error,
        }


def write_status(s: RuntimeStatus) -> None:
    f = paths.status_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f"{f.name}.{os.getpid()}.tmp")  # 每进程独立 tmp，并发不互踩（m-1）
    tmp.write_text(json.dumps(s.to_dict()), encoding="utf-8")
    os.replace(tmp, f)


def read_status() -> RuntimeStatus:
    f = paths.status_file()
    if not f.exists():
        return RuntimeStatus()
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return RuntimeStatus(Phase(d["phase"]), d["percent"], d["message"], d.get("error"))
    except (OSError, ValueError, KeyError):
        return RuntimeStatus()


def _read_receipt_raw() -> str | None:
    try:
        return paths.ready_sentinel().read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None


def read_runtime_receipt() -> dict | None:
    """读取 JSON receipt；legacy 纯文本、损坏内容和非对象均返回 ``None``。"""
    raw = _read_receipt_raw()
    if raw is None:
        return None
    try:
        receipt = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return receipt if isinstance(receipt, dict) else None


def runtime_receipt_state() -> ReceiptState:
    """判定 receipt 与当前 bootstrap 的兼容关系，全程只做小文件读取。"""
    raw = _read_receipt_raw()
    if raw is None:
        return ReceiptState.MISSING
    if raw.strip() == spec.FREECAD_PIN:
        return ReceiptState.LEGACY
    try:
        receipt = json.loads(raw)
    except (TypeError, ValueError):
        return ReceiptState.INCOMPATIBLE
    if not isinstance(receipt, dict):
        return ReceiptState.INCOMPATIBLE

    expected = spec.expected_receipt(external=paths.user_override_env() is not None)
    if receipt == expected:
        return ReceiptState.CURRENT
    without_version = {k: v for k, v in expected.items() if k != "vibecad_version"}
    actual_without_version = {k: v for k, v in receipt.items() if k != "vibecad_version"}
    if actual_without_version == without_version and isinstance(
        receipt.get("vibecad_version"), str
    ):
        return ReceiptState.SERVER_MISMATCH
    return ReceiptState.INCOMPATIBLE


def write_runtime_receipt() -> None:
    """验证成功后的提交点：原子替换 JSON receipt，避免 supervisor 读到半文件。"""
    sentinel = paths.ready_sentinel()
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    tmp = sentinel.with_name(f"{sentinel.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps(
                spec.expected_receipt(external=paths.user_override_env() is not None),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, sentinel)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def runtime_ready() -> bool:
    """廉价就绪探测：receipt 精确匹配且目标 Python 存在，不 import FreeCAD。"""
    return runtime_recovery_kind() is RecoveryKind.READY


def runtime_recovery_kind() -> RecoveryKind:
    """区分轻量 server 同步与可能重建引擎；不启动子进程，保持握手廉价。

    receipt 缺失/损坏时即使 env 最终可被 installer 原地验证并复用，这里仍保守标为
    repair_required，避免在尚未验证前向用户承诺只是轻量升级。外部 override 也始终
    由用户维护；installer 不会改写它，因此版本不匹配不能承诺自动同步。
    """
    state = runtime_receipt_state()
    try:
        python_exists = paths.active_runtime_python().exists()
    except OSError:
        python_exists = False
    if state is ReceiptState.CURRENT and python_exists:
        return RecoveryKind.READY
    if state in {ReceiptState.LEGACY, ReceiptState.SERVER_MISMATCH} and python_exists:
        if paths.user_override_env() is not None:
            return RecoveryKind.REPAIR_REQUIRED
        return RecoveryKind.UPGRADE_REQUIRED
    return RecoveryKind.REPAIR_REQUIRED


def _probe(python: Path | None, snippet: str) -> bool:
    py = python or paths.active_runtime_python()
    if not Path(py).exists():
        return False
    try:
        result = subprocess.run([str(py), "-c", snippet], capture_output=True, timeout=120)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def health_check(python: Path | None = None) -> bool:
    """子进程 import FreeCAD, Part。"""
    return _probe(python, _HEALTH_SNIPPET)


def engine_compatible(python: Path | None = None) -> bool:
    """托管 env 的 Python 与 FreeCAD 版本精确匹配当前 pins。"""
    return _probe(python, _ENGINE_SNIPPET)


def verify_runtime(python: Path | None = None) -> bool:
    """安装期/override 校验 FreeCAD、server 及其版本与 bootstrap 精确一致。"""
    return _probe(python, _VERIFY_SNIPPET)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        # B-1：Windows 上 os.kill(pid,0) 会 TerminateProcess 杀掉目标！改用 OpenProcess 探活
        import ctypes

        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class FileLock:
    """原子 mkdir 跨进程锁；对死进程/超时陈旧锁自动回收，避免永久死锁（M1）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._meta = self.path / "owner.json"

    def try_acquire(self) -> bool:
        try:
            os.mkdir(self.path)
        except FileExistsError:
            if not self._reclaim_if_stale():
                return False
            try:
                os.mkdir(self.path)
            except FileExistsError:
                return False
        with contextlib.suppress(OSError):
            self._meta.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}))
        return True

    def _reclaim_if_stale(self) -> bool:
        try:
            meta = json.loads(self._meta.read_text())
            if not isinstance(meta, dict):  # m-2：非对象的 owner.json 视为损坏
                raise ValueError("owner.json 非对象")
            pid_dead = not _pid_alive(meta.get("pid"))
            stale = pid_dead or (time.time() - meta.get("ts", 0) > _STALE_SECONDS)
        except (OSError, ValueError, TypeError):
            try:
                stale = time.time() - self.path.stat().st_mtime > _STALE_SECONDS
            except OSError:
                return False
        if not stale:
            return False
        # M-D：原子抢占而非盲删——rename 走陈旧锁目录，唯一胜出者成功，杜绝误删他人刚获取的锁
        parked = self.path.with_name(f"{self.path.name}.stale.{os.getpid()}.{time.time_ns()}")
        try:
            os.rename(self.path, parked)
        except OSError:
            return False  # 已被他人抢占/变更 → 放弃
        self._force_remove_dir(parked)
        return True

    @staticmethod
    def _force_remove_dir(d: Path) -> None:
        with contextlib.suppress(OSError):
            (d / "owner.json").unlink()
        with contextlib.suppress(OSError):
            os.rmdir(d)

    def _force_remove(self) -> None:
        self._force_remove_dir(self.path)

    @contextlib.contextmanager
    def acquire(self):
        if not self.try_acquire():
            raise RuntimeError(
                f"安装已在进行（锁 {self.path}）；若确认无安装进程，请手动删除该目录"
            )
        try:
            yield
        finally:
            self._force_remove()
