"""运行时安装编排：micromamba → pinned env → pip 装 server 包 → 冒烟 → 写哨兵。"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

from vibecad.runtime import micromamba, paths, spec, status
from vibecad.runtime.status import Phase, RuntimeStatus

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


def _local_source_root() -> Path | None:
    """源码 checkout / MCPB editable install 时，找到包含当前文件的项目根。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "src" / "vibecad" / "runtime" / "installer.py"
        if (parent / "pyproject.toml").is_file() and candidate.is_file():
            try:
                if candidate.resolve() == here:
                    return parent
            except OSError:
                continue
    return None


def _pip_spec() -> str:
    """目标 server 必须与 bootstrap 同源同版本，禁止漂移到 PyPI latest。"""
    raw = os.environ.get("VIBECAD_PIP_SPEC")
    if raw:
        return os.path.abspath(raw) if os.path.exists(raw) else raw
    if source := _local_source_root():
        return str(source)
    return f"vibecad=={spec.VIBECAD_VERSION}"


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
            # 两个 bootstrap 进程可能同时在旧 receipt 上起步；后来拿锁者必须二检，
            # 避免前一个刚完成同步后又重复 pip/建 env。
            if self.is_ready():
                self._emit(Phase.READY, 100.0, "运行时已就绪")
                return
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
        status.write_runtime_receipt()

    def _install_server_package(self, micromamba_path: Path, root: Path, env: Path) -> None:
        # Windows 直接启动 conda Python 可能缺 Library/bin 的 DLL/PATH 注入；统一经
        # micromamba run 进入环境，和首次安装的跨平台语义一致。
        _run([
            str(micromamba_path), "run", "-r", str(root), "-p", str(env),
            "python", "-m", "pip", "install", "--upgrade", _pip_spec(),
        ])

    def _verify_or_raise(self, python: Path, message: str) -> None:
        if not status.verify_runtime(python):
            raise InstallError(message)

    def _remove_managed_env(self, env: Path) -> None:
        """只删除 VIBECAD_HOME 下固定托管前缀；符号链接只解链，绝不跟随到外部。"""
        expected = paths.mamba_root_prefix() / "envs" / "vibecad"
        if paths.user_override_env() is not None or env != expected:
            raise InstallError(f"拒绝删除非托管运行时前缀：{env}")
        try:
            mode = env.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISDIR(mode):
            shutil.rmtree(env)
        else:
            env.unlink()

    def _do_install(self) -> None:
        # M-B：override 优先——用户用 VIBECAD_FREECAD_ENV 指定现成 env 时只校验复用、绝不自建，
        # 保证「被校验解释器 == 写哨兵前缀 == launcher re-exec 进入的解释器」三者一致。
        if paths.user_override_env() is not None:
            self._emit(Phase.VERIFYING, 95.0, "校验自带 FreeCAD env（VIBECAD_FREECAD_ENV）")
            self._verify_or_raise(
                paths.active_runtime_python(),
                "VIBECAD_FREECAD_ENV 指定的 env 缺 FreeCAD/mcp/vibecad，"
                f"或其中 vibecad 与当前 v{spec.VIBECAD_VERSION} 不一致；"
                "不会自动改写用户 env，请手动安装匹配版本（详见 install.log）",
            )
            self._write_sentinel()
            self._emit(Phase.READY, 100.0, "运行时就绪（复用自带 env）")
            return

        root, env, mm = paths.mamba_root_prefix(), paths.env_prefix(), paths.micromamba_path()
        python = paths.env_python()
        if os.path.lexists(env):
            # receipt 缺失/损坏也不等于 2-3GB 引擎损坏。精确验证通过时只补 receipt；
            # FreeCAD 健康但 server 旧/缺失时只同步 pip 包。
            if status.verify_runtime(python):
                self._write_sentinel()
                self._emit(Phase.READY, 100.0, "运行时就绪（已补全版本凭据）")
                return
            if status.engine_compatible(python):
                self._emit(
                    Phase.INSTALLING_PIP,
                    80.0,
                    f"同步 server 到 VibeCAD v{spec.VIBECAD_VERSION}（复用现有 FreeCAD）",
                )
                micromamba.ensure_micromamba(mm)
                self._install_server_package(mm, root, env)
                self._emit(Phase.VERIFYING, 95.0, "验证 FreeCAD 与 server 版本")
                self._verify_or_raise(
                    python,
                    f"现有 FreeCAD env 中的 vibecad 未能同步到 v{spec.VIBECAD_VERSION}"
                    "（详见 install.log）",
                )
                self._write_sentinel()
                self._emit(Phase.READY, 100.0, "运行时就绪（已复用 FreeCAD 并同步 server）")
                return

            # micromamba create 不允许覆盖 existing prefix。确认是固定托管前缀后先删，
            # 其他 VIBECAD_HOME 内容（日志、views、micromamba、自定义文件）全部保留。
            self._emit(Phase.CREATING_ENV, 15.0, "现有 FreeCAD env 不健康，安全重建托管前缀")
            self._remove_managed_env(env)

        self._emit(Phase.DOWNLOADING_MICROMAMBA, 5.0, "获取 micromamba")
        micromamba.ensure_micromamba(mm)
        self._emit(Phase.CREATING_ENV, 20.0, "创建环境并解析 FreeCAD（约 2-3GB，请耐心）")
        _run([str(mm), "create", "-y", "-r", str(root), "-p", str(env),
              "-c", "conda-forge", "--override-channels", spec.PYTHON_PIN, spec.FREECAD_PIN])
        self._emit(Phase.INSTALLING_PIP, 80.0, "安装 server 依赖")
        self._install_server_package(mm, root, env)
        self._emit(Phase.VERIFYING, 95.0, "冒烟验证 import FreeCAD + vibecad.server")
        # M-B/m-4：统一 active + 验 server 可起
        self._verify_or_raise(
            paths.active_runtime_python(),
            "env 已建但 FreeCAD/server import 或 vibecad 版本校验失败（详见 install.log）",
        )
        self._write_sentinel()
        self._emit(Phase.READY, 100.0, "运行时就绪")
