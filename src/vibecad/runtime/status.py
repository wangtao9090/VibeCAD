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

from vibecad.runtime import paths

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
# 更严就绪校验：连 vibecad.server（连带 mcp）一起 import，确保 re-exec 进去后 server 真能起（m-4）
_VERIFY_SNIPPET = _PREP + "import FreeCAD, Part; import vibecad.server\n"
_STALE_SECONDS = 3600


class Phase(StrEnum):
    NOT_STARTED = "not_started"
    DOWNLOADING_MICROMAMBA = "downloading_micromamba"
    CREATING_ENV = "creating_env"
    INSTALLING_PIP = "installing_pip"
    VERIFYING = "verifying"
    READY = "ready"
    FAILED = "failed"


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


def runtime_ready() -> bool:
    """廉价就绪探测：读哨兵文件，不 import FreeCAD（保握手秒回）。"""
    return paths.ready_sentinel().exists()


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


def verify_runtime(python: Path | None = None) -> bool:
    """安装期/override 校验：import FreeCAD, Part + vibecad.server（连带 mcp），
    确保 re-exec 目标解释器真能起 server（m-4 / M-B）。"""
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
