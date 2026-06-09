"""运行时安装编排：micromamba → pinned env → pip 装 server 包 → 冒烟 → 写哨兵。"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Callable

from vibecad.runtime import micromamba, paths, status
from vibecad.runtime.status import Phase, RuntimeStatus

PYTHON_PIN = "python=3.12"
FREECAD_PIN = "freecad=1.1.0"
ProgressCb = Callable[[RuntimeStatus], None]


class InstallError(RuntimeError):
    pass


def _run(cmd: list, *, cwd: str | None = None) -> None:
    """跑安装子进程；stdout/stderr 重定向到日志，绝不污染 MCP stdio（B2）。"""
    proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        with open(paths.install_log(), "a", encoding="utf-8") as fh:
            fh.write(f"$ {' '.join(map(str, cmd))}\n{proc.stdout or ''}\n")
    except OSError:
        pass
    if proc.returncode != 0:
        tail = (proc.stdout or "")[-2000:]
        raise InstallError(f"命令失败({proc.returncode}): {' '.join(map(str, cmd))}\n{tail}")


def _pip_spec() -> str:
    """预发布期 VIBECAD_PIP_SPEC 指本地源（存在路径才归一化为绝对路径，避免 cwd 歧义，m-3）；
    发布后默认 PyPI 的 'vibecad'。"""
    raw = os.environ.get("VIBECAD_PIP_SPEC")
    if not raw:
        return "vibecad"
    return os.path.abspath(raw) if os.path.exists(raw) else raw


class RuntimeInstaller:
    def __init__(self, on_progress: ProgressCb | None = None):
        self._cb = on_progress or (lambda s: None)

    def is_ready(self) -> bool:
        return status.runtime_ready()  # 哨兵（廉价），不 import

    def _emit(self, phase: Phase, percent: float, message: str) -> None:
        s = RuntimeStatus(phase=phase, percent=percent, message=message)
        status.write_status(s)  # 跨进程可见（M5）
        self._cb(s)

    def install(self) -> None:
        if self.is_ready():
            self._emit(Phase.READY, 100.0, "运行时已就绪")
            return
        paths.vibecad_home().mkdir(parents=True, exist_ok=True)
        with status.FileLock(paths.install_lock()).acquire():
            try:
                self._do_install()
            except Exception as exc:  # noqa: BLE001
                s = RuntimeStatus(
                    phase=Phase.FAILED, percent=0.0, message="安装失败", error=str(exc)
                )
                status.write_status(s)
                self._cb(s)
                raise InstallError(str(exc)) from exc

    def _write_sentinel(self) -> None:
        sentinel = paths.ready_sentinel()
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(FREECAD_PIN, encoding="utf-8")  # 哨兵落在 active_runtime_prefix（M2）

    def _do_install(self) -> None:
        # M-B：override 优先——用户用 VIBECAD_FREECAD_ENV 指定现成 env 时只校验复用、绝不自建，
        # 保证「被校验解释器 == 写哨兵前缀 == launcher re-exec 进入的解释器」三者一致。
        if paths.user_override_env() is not None:
            self._emit(Phase.VERIFYING, 95.0, "校验自带 FreeCAD env（VIBECAD_FREECAD_ENV）")
            if not status.verify_runtime(paths.active_runtime_python()):
                raise InstallError(
                    "VIBECAD_FREECAD_ENV 指定的 env 缺 FreeCAD/mcp/vibecad"
                    " 或不可 import（详见 install.log）"
                )
            self._write_sentinel()
            self._emit(Phase.READY, 100.0, "运行时就绪（复用自带 env）")
            return
        root, env, mm = paths.mamba_root_prefix(), paths.env_prefix(), paths.micromamba_path()
        self._emit(Phase.DOWNLOADING_MICROMAMBA, 5.0, "获取 micromamba")
        micromamba.ensure_micromamba(mm)
        self._emit(Phase.CREATING_ENV, 20.0, "创建环境并解析 FreeCAD（约 2-3GB，请耐心）")
        _run([str(mm), "create", "-y", "-r", str(root), "-p", str(env),
              "-c", "conda-forge", "--override-channels", PYTHON_PIN, FREECAD_PIN])
        self._emit(Phase.INSTALLING_PIP, 80.0, "安装 server 依赖")
        _run([str(mm), "run", "-r", str(root), "-p", str(env),
              "python", "-m", "pip", "install", _pip_spec()])
        self._emit(Phase.VERIFYING, 95.0, "冒烟验证 import FreeCAD + vibecad.server")
        # M-B/m-4：统一 active + 验 server 可起
        if not status.verify_runtime(paths.active_runtime_python()):
            raise InstallError("env 已建但 import FreeCAD/vibecad.server 失败（详见 install.log）")
        self._write_sentinel()
        self._emit(Phase.READY, 100.0, "运行时就绪")
