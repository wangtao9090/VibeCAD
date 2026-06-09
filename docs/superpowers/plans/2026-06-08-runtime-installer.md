# VibeCAD 跨平台运行时安装器 Implementation Plan（v3 · 两轮对抗审查已纳入）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 VibeCAD 的「零安装」跨平台运行时——uvx 引导壳 + re-exec 进 conda env python + 进程内 import FreeCAD，并把 2-3GB 的 FreeCAD 运行时获取做成不破坏 MCP 握手的后台任务，在本机 macOS arm64 实跑验证地基假设 A1/A2/A3。

**Architecture:** `uvx vibecad` 启动纯 stdlib launcher（跑在 uv 临时 python）；launcher 用**廉价哨兵**（`<env>/.vibecad_ready`）判运行时是否就绪：就绪则 `os.execv`（Windows 用 subprocess）交棒到 env 自带 python 运行 `vibecad.server`，从而进程内 `import FreeCAD`（懒加载，零序列化）；未就绪则在当前 python 起 bootstrap server，握手秒回，`ensure_runtime` 后台线程跑 micromamba 引导（下载 micromamba → 建 pinned env `python=3.12 freecad=1.1.0` → pip 装 server 依赖 → 冒烟 → 写哨兵）。进度落 `status.json` 供跨进程轮询。

**Tech Stack:** Python 3.12（conda env）/ 任意≥3.12（launcher，纯 stdlib）· 官方 MCP SDK（FastMCP, mcp≥1.12）· micromamba 2.8.0-0 · conda-forge freecad 1.1.0 · uv/uvx · pytest + ruff · GitHub Actions 平台矩阵。

---

## Context（为什么做这件事）

地基假设 A1（进程内 import FreeCAD）/A2（micromamba 装 freecad）/A3（uvx server 如何用上独立 conda env 的 FreeCAD）已由研究 workflow 果断定案为 **uvx 引导 + re-exec 进 conda python**（详见 `docs/superpowers/research/2026-06-08-install-env-research.md`，已同步飞书）。用户四项决策：① 首里程碑 = **运行时安装器单点突破**（含本机实跑 A1/A2/A3）；② 首启 UX = **显式 `ensure_runtime` 后台 + `get_runtime_status` 轮询 + MVP 重连**，另给 `VIBECAD_AUTO_INSTALL` opt-in；③ **单零件先行**；④ 基线 = **Python 3.12 + freecad 1.1.0 + PyPI 包名 `vibecad`（已查可用）+ 自建 pinned env + `VIBECAD_FREECAD_ENV` opt-in**。

本轮只交付 4 个工具（`ping`/`get_runtime_status`/`ensure_runtime`/`smoke_cad`）证明地基成立；**不含**完整 CAD 工具层、四 agent 接入落地、装配（下一轮）。

### 审查纳入（v2 相对 v1 的关键修正）
对抗审查（6 维 workflow）发现并已修复：**B1** Windows sha256 URL 404；**B2** 安装子进程 stdout 污染 MCP 通道；**B3/M7** 守卫/launcher 看错就绪对象（统一为 `active_runtime_python()` + `_in_conda_runtime()`）；**B4** 集成测试在错误解释器 import（改 subprocess 进 conda python）；**B5** installer 别名导致单测 ERROR；**B6** happy-path 测试自相矛盾；**M1** FileLock 陈旧死锁（加 pid/mtime 回收）；**M2** 就绪探测重型 import 破握手（改 `.vibecad_ready` 哨兵）；**M3/m5** 删 pip 的 numpy 避免 ABI 雷；**M4** Windows DLL 用 PATH 兜底为主 + Windows CI 证实；**M5** 进度落 `status.json` 跨进程可见；**M6** micromamba 下载 .part 原子改名 + 既有文件也校验；**M8** 验收命令用绝对路径/PIP_SPEC。审查同时确认 `.sha256` 解析、120s 超时、`python=3.12/freecad=1.1.0` pin、micromamba 命令、FastMCP 用法、re-exec 无死循环均**正确，勿动**。

### 审查纳入（v3 相对 v2 · 第二轮回归审查）
第二轮聚焦审查确认 v2 **12/14 修对、无误伤**，并再修：**B-1** Windows `os.kill(pid,0)` 实为杀进程 → `_pid_alive` 改 `OpenProcess` 探活；**M-A** FreeCAD/OCCT 进程内 import/export 向 fd1 写、污染 MCP 通道 → `_silence_fd1`（dup2 fd1→fd2）；**M-B** installer 未闭合 `active_runtime` 致 override split-brain → override 短路复用 + 统一 `verify_runtime`（连带验 `vibecad.server`/mcp 可起）；**M-C** 集成测试缺 Windows DLL prep → 加 `status._PREP` + 浮点容差；**M-D** FileLock 回收 TOCTOU → 原子 `rename` 抢占；**M-E** `claude mcp add` 选项前置；并带走 m-1～m-7（write_status 每进程 tmp、owner.json 非 dict 容错、`_pip_spec` 去 `.` 特例、ruff.lint 保留、test_status_shape hermetic、补 `_run` 非零/`_pid_alive`/`_reexec` 测试）。

---

## File Structure

```
src/vibecad/
├── __main__.py            新建  `python -m vibecad` → launcher.main
├── launcher.py            新建  A3 re-exec 决策（纯 stdlib；用哨兵判就绪，不重型 import）
├── server.py             改写  FastMCP：ping/get_runtime_status/ensure_runtime/smoke_cad + _in_conda_runtime
└── runtime/
    ├── __init__.py        改写  仅 re-export 纯 stdlib 符号，严禁 import server/mcp
    ├── platform.py        新建  conda_subdir / is_macos / is_windows / MICROMAMBA_ASSET（纯 stdlib）
    ├── paths.py           新建  HOME/env/python/哨兵/status/lock 路径 + active_runtime_python（纯 stdlib）
    ├── micromamba.py      新建  下载(.part 原子改名)+sha256(按 subdir 拼 URL)（纯 stdlib urllib）
    ├── status.py          新建  Phase/RuntimeStatus + write/read_status + runtime_ready 哨兵 + health_check + FileLock
    └── installer.py       新建  RuntimeInstaller：micromamba→create→pip→smoke→写哨兵；_run 重定向；进度落盘
tests/  test_platform/paths/micromamba/status/installer/launcher/server_tools/runtime_purity + test_runtime_integration(@slow)
pyproject.toml             改写  requires-python>=3.12、entry=launcher:main、mcp>=1.12、pytest markers
.github/workflows/ci.yml   新建  五平台 lint+unit + ubuntu/macos/windows 运行时集成
```

**纪律**：`platform/paths/status` 纯 stdlib（launcher 在 uv 临时 env import 它们）；`runtime/__init__.py` 不得 import mcp（否则破坏 launcher 纯 stdlib 地基，见 Task 11 回归测试）。本机 dev venv 由 `uv` 选系统 Python 3.14（满足 `>=3.12`），单元测试在其上跑（不 import FreeCAD）；FreeCAD 运行时是另装的 pinned 3.12 env，两者隔离——这是正常的，非冲突。

---

## Task 1: 平台检测 `runtime/platform.py`

**Files:** Create `src/vibecad/runtime/platform.py`, `tests/test_platform.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_platform.py
import pytest
from vibecad.runtime import platform as p


def test_known_platforms(monkeypatch):
    cases = [
        ("darwin", "arm64", "osx-arm64"), ("darwin", "x86_64", "osx-64"),
        ("linux", "x86_64", "linux-64"), ("linux", "aarch64", "linux-aarch64"),
        ("win32", "AMD64", "win-64"),
    ]
    for sysname, machine, expected in cases:
        monkeypatch.setattr(p.sys, "platform", sysname)
        monkeypatch.setattr(p, "_machine", lambda: machine)
        assert p.conda_subdir() == expected


def test_unsupported(monkeypatch):
    monkeypatch.setattr(p.sys, "platform", "win32")
    monkeypatch.setattr(p, "_machine", lambda: "ARM64")
    with pytest.raises(p.UnsupportedPlatformError):
        p.conda_subdir()


def test_asset_table_complete():
    for s in ("linux-64", "linux-aarch64", "osx-64", "osx-arm64", "win-64"):
        assert s in p.MICROMAMBA_ASSET


def test_os_predicates(monkeypatch):
    monkeypatch.setattr(p.sys, "platform", "darwin")
    assert p.is_macos() and not p.is_windows()
    monkeypatch.setattr(p.sys, "platform", "win32")
    assert p.is_windows() and not p.is_macos()
```

- [ ] **Step 2: 运行验证失败** — `uv run pytest tests/test_platform.py -v` → FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
# src/vibecad/runtime/platform.py
"""平台检测：映射到 conda subdir。纯 stdlib。"""
from __future__ import annotations
import platform as _platform
import sys


class UnsupportedPlatformError(RuntimeError):
    """无 conda-forge freecad 构建（win-arm64、ppc64le 等）。"""


MICROMAMBA_ASSET: dict[str, str] = {
    "linux-64": "micromamba-linux-64", "linux-aarch64": "micromamba-linux-aarch64",
    "osx-64": "micromamba-osx-64", "osx-arm64": "micromamba-osx-arm64",
    "win-64": "micromamba-win-64.exe",
}


def _machine() -> str:
    return _platform.machine()


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def conda_subdir() -> str:
    machine = _machine().lower()
    if is_macos():
        return "osx-arm64" if machine in ("arm64", "aarch64") else "osx-64"
    if sys.platform.startswith("linux"):
        if machine in ("aarch64", "arm64"):
            return "linux-aarch64"
        if machine in ("x86_64", "amd64"):
            return "linux-64"
        raise UnsupportedPlatformError(f"Linux {machine} 无 freecad 构建")
    if is_windows():
        if machine in ("amd64", "x86_64"):
            return "win-64"
        raise UnsupportedPlatformError(f"Windows {machine} 无 freecad 构建（win-arm64 暂不支持）")
    raise UnsupportedPlatformError(f"未知平台 {sys.platform}/{machine}")
```

- [ ] **Step 4: 验证通过** — `uv run pytest tests/test_platform.py -v` → PASS（4 passed）
- [ ] **Step 5: 提交** — `git add src/vibecad/runtime/platform.py tests/test_platform.py && git commit -m "feat(runtime): platform detection -> conda subdir"`

---

## Task 2: 路径解析 `runtime/paths.py`

**Files:** Create `src/vibecad/runtime/paths.py`, `tests/test_paths.py`

> 引入贯穿全案的「当前运行时」统一解析：`active_runtime_prefix/python`（override 优先），消除 launcher 与 installer 口径不一致（B3/M7）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_paths.py
from vibecad.runtime import paths


def test_home_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "vc"))
    assert paths.vibecad_home() == tmp_path / "vc"


def test_env_layout_unix(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(paths.platform, "is_windows", lambda: False)
    assert paths.env_prefix() == tmp_path / "mamba" / "envs" / "vibecad"
    assert paths.env_python() == tmp_path / "mamba" / "envs" / "vibecad" / "bin" / "python"
    assert paths.ready_sentinel() == tmp_path / "mamba" / "envs" / "vibecad" / ".vibecad_ready"


def test_env_python_windows(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(paths.platform, "is_windows", lambda: True)
    assert paths.env_python().name == "python.exe"
    assert paths.micromamba_path().name == "micromamba.exe"
    assert paths.freecadcmd_path() == tmp_path / "mamba" / "envs" / "vibecad" / "Library" / "bin" / "FreeCADCmd.exe"


def test_active_runtime_prefers_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(paths.platform, "is_windows", lambda: False)
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(tmp_path / "myenv"))
    assert paths.active_runtime_prefix() == tmp_path / "myenv"
    assert paths.active_runtime_python() == tmp_path / "myenv" / "bin" / "python"
    assert paths.ready_sentinel() == tmp_path / "myenv" / ".vibecad_ready"
```

- [ ] **Step 2: 运行验证失败** — `uv run pytest tests/test_paths.py -v` → FAIL

- [ ] **Step 3: 实现**

```python
# src/vibecad/runtime/paths.py
"""运行时落盘路径解析。纯 stdlib，跨平台。env 独立于 uv cache。"""
from __future__ import annotations
import os
from pathlib import Path
from vibecad.runtime import platform


def vibecad_home() -> Path:
    if v := os.environ.get("VIBECAD_HOME"):
        return Path(v).expanduser()
    if platform.is_windows():
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "VibeCAD"
    if platform.is_macos():
        return Path.home() / "Library" / "Application Support" / "VibeCAD"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "VibeCAD"


def mamba_root_prefix() -> Path:
    return vibecad_home() / "mamba"


def env_prefix() -> Path:
    return mamba_root_prefix() / "envs" / "vibecad"


def env_python_for(prefix: Path) -> Path:
    return prefix / "python.exe" if platform.is_windows() else prefix / "bin" / "python"


def env_python() -> Path:
    return env_python_for(env_prefix())


def user_override_env() -> Path | None:
    v = os.environ.get("VIBECAD_FREECAD_ENV")
    return Path(v).expanduser() if v else None


def active_runtime_prefix() -> Path:
    """override 优先，否则托管 env。launcher 与 installer 统一以此为准。"""
    return user_override_env() or env_prefix()


def active_runtime_python() -> Path:
    return env_python_for(active_runtime_prefix())


def ready_sentinel() -> Path:
    """安装成功后写此哨兵；就绪探测读它（廉价，不 import FreeCAD）。"""
    return active_runtime_prefix() / ".vibecad_ready"


def status_file() -> Path:
    return vibecad_home() / "status.json"


def install_lock() -> Path:
    return vibecad_home() / ".install.lock"


def install_log() -> Path:
    return vibecad_home() / "install.log"


def freecadcmd_path() -> Path:
    env = env_prefix()
    return env / "Library" / "bin" / "FreeCADCmd.exe" if platform.is_windows() else env / "bin" / "freecadcmd"


def micromamba_path() -> Path:
    return vibecad_home() / "bin" / ("micromamba.exe" if platform.is_windows() else "micromamba")
```

- [ ] **Step 4: 验证通过** — PASS（4 passed）
- [ ] **Step 5: 提交** — `git add src/vibecad/runtime/paths.py tests/test_paths.py && git commit -m "feat(runtime): path resolution + unified active_runtime_python/sentinel"`

---

## Task 3: micromamba 下载 `runtime/micromamba.py`

**Files:** Create `src/vibecad/runtime/micromamba.py`, `tests/test_micromamba.py`

> 修 **B1**（sha256 URL 按 subdir 拼，不含 `.exe`）、**M6**（下载到 `.part`，校验后原子改名；既有文件也校验）、**m2**（pin 版本）、**m6**（单字段 hash mock）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_micromamba.py
import hashlib
import pytest
from vibecad.runtime import micromamba as mm


def test_download_url_and_sha_url():
    assert mm.download_url("osx-arm64").endswith("/micromamba-osx-arm64")
    assert mm.download_url("win-64").endswith("/micromamba-win-64.exe")
    # B1: sha256 URL 永不含 .exe
    assert mm._sha256_url("win-64").endswith("/micromamba-win-64.sha256")
    assert ".exe.sha256" not in mm._sha256_url("win-64")


def test_download_verify_atomic(tmp_path, monkeypatch):
    dest = tmp_path / "bin" / "micromamba"
    payload = b"fake-binary"
    digest = hashlib.sha256(payload).hexdigest()
    written = {}

    def fake_dl(url, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        written["target"] = target
    monkeypatch.setattr(mm, "_download", fake_dl)
    monkeypatch.setattr(mm, "_fetch_text", lambda url: digest)  # m6: 单字段裸 hash
    out = mm.ensure_micromamba(dest, subdir="osx-arm64")
    assert out.read_bytes() == payload
    assert written["target"].name.endswith(".part")  # 下载先落 .part
    assert not (tmp_path / "bin" / "micromamba.part").exists()  # 已原子改名


def test_existing_file_reverified(tmp_path, monkeypatch):
    dest = tmp_path / "bin" / "micromamba"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"good")
    monkeypatch.setattr(mm, "_fetch_text", lambda url: hashlib.sha256(b"good").hexdigest())
    monkeypatch.setattr(mm, "_download", lambda u, t: pytest.fail("should not download valid existing"))
    assert mm.ensure_micromamba(dest, subdir="osx-arm64") == dest


def test_checksum_mismatch(tmp_path, monkeypatch):
    dest = tmp_path / "bin" / "micromamba"
    monkeypatch.setattr(mm, "_download", lambda u, t: (t.parent.mkdir(parents=True, exist_ok=True), t.write_bytes(b"x")))
    monkeypatch.setattr(mm, "_fetch_text", lambda url: "deadbeef")
    with pytest.raises(mm.ChecksumError):
        mm.ensure_micromamba(dest, subdir="osx-arm64")
    assert not (tmp_path / "bin" / "micromamba.part").exists()  # 失败清理 .part
```

- [ ] **Step 2: 运行验证失败** — FAIL

- [ ] **Step 3: 实现**

```python
# src/vibecad/runtime/micromamba.py
"""下载并校验 micromamba 单文件二进制。纯 stdlib（urllib，自动跟随 302）。"""
from __future__ import annotations
import hashlib
import os
import stat
import urllib.request
from pathlib import Path
from vibecad.runtime import platform

MICROMAMBA_VERSION = "2.8.0-0"  # pin（与 freecad/python 一致纳入可控升级）
_BASE = f"https://github.com/mamba-org/micromamba-releases/releases/download/{MICROMAMBA_VERSION}"


class ChecksumError(RuntimeError):
    """micromamba sha256 与官方校验和不符。"""


def download_url(subdir: str | None = None) -> str:
    subdir = subdir or platform.conda_subdir()
    return f"{_BASE}/{platform.MICROMAMBA_ASSET[subdir]}"


def _sha256_url(subdir: str | None = None) -> str:
    subdir = subdir or platform.conda_subdir()
    # B1: 校验和资源名按 subdir 拼，绝不含二进制的 .exe 后缀
    return f"{_BASE}/micromamba-{subdir}.sha256"


def _download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as resp, open(target, "wb") as fh:  # noqa: S310
        while chunk := resp.read(1 << 20):
            fh.write(chunk)


def _fetch_text(url: str) -> str:
    with urllib.request.urlopen(url) as resp:  # noqa: S310
        return resp.read().decode("utf-8", "replace")


def _sha256_ok(path: Path, subdir: str | None) -> bool:
    expected = _fetch_text(_sha256_url(subdir)).split()[0].strip().lower()  # 单/双字段都鲁棒
    actual = hashlib.sha256(path.read_bytes()).hexdigest().lower()
    return actual == expected


def ensure_micromamba(dest: Path, *, subdir: str | None = None) -> Path:
    """幂等：若 dest 已存在且 sha256 合法直接用；否则下载到 .part 校验后原子改名。"""
    sd = subdir or platform.conda_subdir()
    if dest.exists() and dest.stat().st_size > 0 and _sha256_ok(dest, sd):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        _download(download_url(sd), tmp)
        if not _sha256_ok(tmp, sd):
            raise ChecksumError(f"micromamba sha256 不符（subdir={sd}）")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    if not platform.is_windows():
        os.chmod(dest, dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest
```

- [ ] **Step 4: 验证通过** — PASS（4 passed）
- [ ] **Step 5: 提交** — `git add src/vibecad/runtime/micromamba.py tests/test_micromamba.py && git commit -m "feat(runtime): micromamba download (per-subdir sha256, atomic .part)"`

---

## Task 4: 状态/哨兵/健康检查/文件锁 `runtime/status.py`

**Files:** Create `src/vibecad/runtime/status.py`, `tests/test_status.py`

> 修 **M2**（`runtime_ready` 读哨兵，不 import）、**M5**（`write/read_status` 跨进程）、**M1**（FileLock pid/mtime 回收）、**M4**（health_check 的 `-c` 含 Windows PATH 兜底前缀）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_status.py
from vibecad.runtime import status


def test_status_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    s = status.RuntimeStatus(phase=status.Phase.CREATING_ENV, percent=20.0, message="建环境")
    status.write_status(s)
    got = status.read_status()
    assert got.phase is status.Phase.CREATING_ENV and got.percent == 20.0
    assert status.read_status().to_dict()["message"] == "建环境"


def test_read_status_default_when_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    assert status.read_status().phase is status.Phase.NOT_STARTED


def test_runtime_ready_reads_sentinel(monkeypatch, tmp_path):
    sentinel = tmp_path / ".vibecad_ready"
    monkeypatch.setattr(status.paths, "ready_sentinel", lambda: sentinel)
    assert status.runtime_ready() is False
    sentinel.write_text("freecad=1.1.0")
    assert status.runtime_ready() is True


def test_health_snippet_has_win_dll_prep():
    # M4: -c 片段在 import 前注入 PATH 兜底
    assert "Library" in status._HEALTH_SNIPPET and "import FreeCAD" in status._HEALTH_SNIPPET


def test_health_check_false_when_python_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(status.paths, "active_runtime_python", lambda: tmp_path / "nope")
    assert status.health_check() is False


def test_file_lock_exclusive_and_reentrant(tmp_path):
    lock = status.FileLock(tmp_path / "lock")
    with lock.acquire():
        assert status.FileLock(tmp_path / "lock").try_acquire() is False
    assert status.FileLock(tmp_path / "lock").try_acquire() is True


def test_file_lock_reclaims_dead_pid(tmp_path, monkeypatch):
    lock_dir = tmp_path / "lock"
    lock = status.FileLock(lock_dir)
    assert lock.try_acquire() is True            # 留下 owner.json（本进程 pid）
    monkeypatch.setattr(status, "_pid_alive", lambda pid: False)  # 模拟持锁进程已死
    assert status.FileLock(lock_dir).try_acquire() is True        # 回收陈旧锁


def test_pid_alive_self_and_dead():
    import os  # B-1：跨平台探活（Windows 用 OpenProcess 而非杀进程的 os.kill）
    assert status._pid_alive(os.getpid()) is True
    assert status._pid_alive(2_000_000_000) is False
    assert status._pid_alive(None) is False
```

- [ ] **Step 2: 运行验证失败** — FAIL

- [ ] **Step 3: 实现**

```python
# src/vibecad/runtime/status.py
"""运行时状态机、哨兵就绪探测、健康检查、跨进程文件锁。纯 stdlib。"""
from __future__ import annotations
import contextlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
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
)
_HEALTH_SNIPPET = _PREP + "import FreeCAD, Part\n"
# 更严就绪校验：连 vibecad.server（连带 mcp）一起 import，确保 re-exec 进去后 server 真能起（m-4）
_VERIFY_SNIPPET = _PREP + "import FreeCAD, Part; import vibecad.server\n"
_STALE_SECONDS = 3600


class Phase(str, Enum):
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
        return {"phase": self.phase.value, "percent": self.percent, "message": self.message, "error": self.error}


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
        return subprocess.run([str(py), "-c", snippet], capture_output=True, timeout=120).returncode == 0
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

        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
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
            stale = not _pid_alive(meta.get("pid")) or (time.time() - meta.get("ts", 0) > _STALE_SECONDS)
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
            raise RuntimeError(f"安装已在进行（锁 {self.path}）；若确认无安装进程，请手动删除该目录")
        try:
            yield
        finally:
            self._force_remove()
```

- [ ] **Step 4: 验证通过** — PASS（8 passed）
- [ ] **Step 5: 提交** — `git add src/vibecad/runtime/status.py tests/test_status.py && git commit -m "feat(runtime): sentinel readiness, cross-proc status, stale-aware lock, win dll prep"`

---

## Task 5: 安装编排 `runtime/installer.py`

**Files:** Create `src/vibecad/runtime/installer.py`, `tests/test_installer.py`

> 修 **B5**（去别名 `status`）、**B6**（测试分别 mock）、**B2**（`_run` 重定向 stdout 写 log）、**M2**（写哨兵）、**M3/m5**（删 numpy/SERVER_PIP_DEPS，只装 pip_spec）、**M5**（进度落盘）、**M8**（pip_spec 绝对路径）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_installer.py
import pytest
from vibecad.runtime import installer as inst
from vibecad.runtime.status import Phase


def test_install_happy_path(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(inst.RuntimeInstaller, "is_ready", lambda self: False)   # 不短路
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", lambda dest, **k: dest)
    ran = []
    monkeypatch.setattr(inst, "_run", lambda cmd, **k: ran.append(cmd))
    monkeypatch.setattr(inst.status, "verify_runtime", lambda *a, **k: True)      # VERIFYING 过
    seen = []
    inst.RuntimeInstaller(on_progress=lambda s: seen.append(s.phase)).install()
    assert Phase.CREATING_ENV in seen and Phase.INSTALLING_PIP in seen and seen[-1] is Phase.READY
    create = " ".join(map(str, ran[0]))
    assert "create" in create and "python=3.12" in create and "freecad=1.1.0" in create
    assert inst.paths.ready_sentinel().exists()  # 写了哨兵


def test_is_ready_uses_sentinel(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(inst.status, "runtime_ready", lambda: True)
    assert inst.RuntimeInstaller().is_ready() is True


def test_install_failed_on_smoke(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(inst.RuntimeInstaller, "is_ready", lambda self: False)
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", lambda dest, **k: dest)
    monkeypatch.setattr(inst, "_run", lambda cmd, **k: None)
    monkeypatch.setattr(inst.status, "verify_runtime", lambda *a, **k: False)
    seen = []
    with pytest.raises(inst.InstallError):
        inst.RuntimeInstaller(on_progress=lambda s: seen.append(s)).install()
    assert seen[-1].phase is Phase.FAILED


def test_run_redirects_stdout(monkeypatch, tmp_path):
    # B2: 子进程绝不继承 fd1
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    captured = {}

    class P:
        returncode = 0
        stdout = "ok"
    def fake_run(cmd, **kw):
        captured.update(kw)
        return P()
    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    inst._run(["echo", "hi"])
    assert captured["stdout"] is inst.subprocess.PIPE
    assert captured["stderr"] is inst.subprocess.STDOUT


def test_run_raises_on_nonzero(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))  # m-7：非零返回码须抛 InstallError

    class P:
        returncode = 1
        stdout = "boom"
    monkeypatch.setattr(inst.subprocess, "run", lambda cmd, **kw: P())
    with pytest.raises(inst.InstallError):
        inst._run(["false"])
```

- [ ] **Step 2: 运行验证失败** — FAIL

- [ ] **Step 3: 实现**

```python
# src/vibecad/runtime/installer.py
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
        raise InstallError(f"命令失败({proc.returncode}): {' '.join(map(str, cmd))}\n{(proc.stdout or '')[-2000:]}")


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
                s = RuntimeStatus(phase=Phase.FAILED, percent=0.0, message="安装失败", error=str(exc))
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
                raise InstallError("VIBECAD_FREECAD_ENV 指定的 env 缺 FreeCAD/mcp/vibecad 或不可 import（详见 install.log）")
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
        _run([str(mm), "run", "-r", str(root), "-p", str(env), "python", "-m", "pip", "install", _pip_spec()])
        self._emit(Phase.VERIFYING, 95.0, "冒烟验证 import FreeCAD + vibecad.server")
        if not status.verify_runtime(paths.active_runtime_python()):  # M-B/m-4：统一 active + 验 server 可起
            raise InstallError("env 已建但 import FreeCAD/vibecad.server 失败（详见 install.log）")
        self._write_sentinel()
        self._emit(Phase.READY, 100.0, "运行时就绪")
```

- [ ] **Step 4: 验证通过** — PASS（5 passed）
- [ ] **Step 5: 提交** — `git add src/vibecad/runtime/installer.py tests/test_installer.py && git commit -m "feat(runtime): installer (stdio-safe run, sentinel, cross-proc status)"`

---

## Task 6: 引导 launcher `launcher.py` + `__main__.py`

**Files:** Create `src/vibecad/launcher.py`, `src/vibecad/__main__.py`, `tests/test_launcher.py`

> 修 **M2/M7**：用 `active_runtime_python()` + 哨兵 `runtime_ready()` 决策（不每次重型 import）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_launcher.py
from pathlib import Path
from vibecad import launcher


def test_already_in_runtime_runs_server(monkeypatch, tmp_path):
    py = tmp_path / "bin" / "python"; py.parent.mkdir(parents=True); py.write_text("")
    monkeypatch.setattr(launcher.paths, "active_runtime_python", lambda: py)
    monkeypatch.setattr(launcher.sys, "executable", str(py))
    started = {}
    monkeypatch.setattr(launcher, "_run_server", lambda: started.setdefault("server", True))
    launcher.main()
    assert started["server"]


def test_ready_reexecs(monkeypatch, tmp_path):
    py = tmp_path / "bin" / "python"; py.parent.mkdir(parents=True); py.write_text("")
    monkeypatch.setattr(launcher.paths, "active_runtime_python", lambda: py)
    monkeypatch.setattr(launcher.sys, "executable", "/uv/tmp/python")
    monkeypatch.setattr(launcher.status, "runtime_ready", lambda: True)
    reexec = {}
    monkeypatch.setattr(launcher, "_reexec_into", lambda p: reexec.setdefault("py", Path(p)))
    launcher.main()
    assert reexec["py"] == py


def test_not_ready_bootstraps(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.paths, "active_runtime_python", lambda: tmp_path / "nope")
    monkeypatch.setattr(launcher.sys, "executable", "/uv/tmp/python")
    monkeypatch.setattr(launcher.status, "runtime_ready", lambda: False)
    started = {}
    monkeypatch.setattr(launcher, "_run_server", lambda: started.setdefault("bootstrap", True))
    launcher.main()
    assert started["bootstrap"]


def test_reexec_posix_uses_execv(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    called = {}
    monkeypatch.setattr(launcher.os, "execv", lambda p, a: called.setdefault("execv", (p, a)))
    launcher._reexec_into(tmp_path / "bin" / "python")
    assert called["execv"][1][1:] == ["-m", "vibecad.server"]


def test_reexec_windows_uses_subprocess(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.sys, "platform", "win32")

    class R:
        returncode = 0
    monkeypatch.setattr(launcher.subprocess, "run", lambda a: R())
    raised = False
    try:
        launcher._reexec_into(tmp_path / "python.exe")
    except SystemExit as e:
        raised = e.code == 0
    assert raised
```

- [ ] **Step 2: 运行验证失败** — FAIL

- [ ] **Step 3: 实现**

```python
# src/vibecad/launcher.py
"""A3 引导壳：决定在哪个 python 跑 server。纯 stdlib，禁 import mcp/trimesh/FreeCAD。"""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path
from vibecad.runtime import paths, status


def _run_server() -> None:
    from vibecad.server import main as server_main  # 延迟 import（两 env 均已装 mcp）
    server_main()


def _reexec_into(env_py: Path) -> None:
    args = [str(env_py), "-m", "vibecad.server"]
    if sys.platform == "win32":
        sys.exit(subprocess.run(args).returncode)  # Windows 无真 exec
    os.execv(str(env_py), args)


def main() -> None:
    runtime_py = paths.active_runtime_python()
    try:
        in_runtime = Path(sys.executable).resolve() == Path(runtime_py).resolve()
    except OSError:
        in_runtime = False
    if in_runtime:
        _run_server()                          # 已在 conda python（re-exec 后二次进入）
    elif status.runtime_ready() and Path(runtime_py).exists():
        _reexec_into(runtime_py)               # 哨兵就绪 → 交棒
    else:
        _run_server()                          # bootstrap：未就绪，只起轻量 server
```

```python
# src/vibecad/__main__.py
"""`python -m vibecad` 入口。"""
from vibecad.launcher import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 验证通过** — PASS（5 passed）
- [ ] **Step 5: 提交** — `git add src/vibecad/launcher.py src/vibecad/__main__.py tests/test_launcher.py && git commit -m "feat: launcher re-exec via sentinel readiness (A3)"`

---

## Task 7: server 工具 `server.py`

**Files:** Modify `src/vibecad/server.py`; Create `tests/test_server_tools.py`; remove `tests/test_smoke.py`

> 修 **B3**（`_in_conda_runtime` 守卫 + 重连提示）、**M4**（`_prepare_freecad_import` PATH 兜底）、**M5**（status 读盘）、**m3**（`dict[str, Any]`）、**m9**（去死代码 + `__version__` 测试）、**m10**（offscreen）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_server_tools.py
import vibecad.server as srv


def test_ping_has_version():
    from vibecad import __version__
    assert __version__ in srv.ping()


def test_status_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))  # m-6：hermetic，不读真实 home
    d = srv.get_runtime_status()
    assert {"phase", "percent", "message", "error", "needs_reconnect"} <= set(d)


def test_ensure_ready(monkeypatch):
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    assert srv._ensure_runtime_impl()["status"] == "ready"


def test_ensure_starts_bg(monkeypatch):
    monkeypatch.setattr(srv._installer, "is_ready", lambda: False)
    started = {}
    monkeypatch.setattr(srv, "_spawn_install", lambda: started.setdefault("bg", True))
    assert srv._ensure_runtime_impl()["status"] == "started"
    assert started["bg"]


def test_smoke_guard_not_ready(monkeypatch):
    monkeypatch.setattr(srv._installer, "is_ready", lambda: False)
    out = srv.smoke_cad()
    assert out["ok"] is False and "未就绪" in out["message"]


def test_smoke_guard_needs_reconnect(monkeypatch):
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: False)
    out = srv.smoke_cad()
    assert out["ok"] is False and "重连" in out["message"]
```

- [ ] **Step 2: 运行验证失败** — FAIL

- [ ] **Step 3: 实现（改写 server.py）**

```python
# src/vibecad/server.py
"""VibeCAD MCP server（FastMCP, stdio）。握手必须秒回：模块级不 import FreeCAD、不下载。"""
from __future__ import annotations
import contextlib
import os
import sys
import threading
from typing import Any
from mcp.server.fastmcp import FastMCP
from vibecad import __version__
from vibecad.runtime import paths, status
from vibecad.runtime.installer import RuntimeInstaller

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # m10：杜绝隐式拉起 GUI

mcp = FastMCP("vibecad")
_installer = RuntimeInstaller()  # 进度由 installer 落 status.json，server 读盘
_install_thread: threading.Thread | None = None


def _in_conda_runtime() -> bool:
    """当前进程是否就是 conda 运行时 python（决定能否进程内 import FreeCAD）。"""
    try:
        return os.path.realpath(sys.executable) == os.path.realpath(paths.active_runtime_python())
    except OSError:
        return False


@mcp.tool()
def ping() -> str:
    """连通性自检。"""
    return f"vibecad ok (v{__version__})"


@mcp.tool()
def get_runtime_status() -> dict[str, Any]:
    """查询 FreeCAD 运行时安装进度（跨进程读 status.json）。"""
    d = status.read_status().to_dict()
    d["needs_reconnect"] = status.runtime_ready() and not _in_conda_runtime()
    return d


def _spawn_install() -> None:
    global _install_thread
    if _install_thread and _install_thread.is_alive():
        return
    _install_thread = threading.Thread(target=_safe_install, name="vibecad-install", daemon=True)
    _install_thread.start()


def _safe_install() -> None:
    try:
        _installer.install()
    except Exception:  # noqa: BLE001 - 失败态已落 status.json
        pass


def _ensure_runtime_impl() -> dict[str, Any]:
    if _installer.is_ready():
        msg = "FreeCAD 运行时已就绪"
        if not _in_conda_runtime():
            msg += "；当前会话运行在引导解释器，请重连本 MCP server 后即可使用 CAD 能力"
        return {"status": "ready", "message": msg}
    if _install_thread and _install_thread.is_alive():
        return {"status": "in_progress", "message": "安装进行中，请轮询 get_runtime_status"}
    _spawn_install()
    return {"status": "started", "message": "已开始后台安装 FreeCAD 运行时（约 2-3GB），请轮询 get_runtime_status"}


@mcp.tool()
def ensure_runtime() -> dict[str, Any]:
    """确保 FreeCAD 运行时就绪：未就绪则后台开始安装并立即返回，用 get_runtime_status 轮询。"""
    return _ensure_runtime_impl()


def _prepare_freecad_import() -> None:
    """M4：Windows 把 conda Library/bin 注入 PATH（add_dll_directory 作双保险，可能被 conda 补丁拦截）。"""
    if sys.platform == "win32":
        libbin = os.path.join(sys.prefix, "Library", "bin")
        os.environ["PATH"] = libbin + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(libbin)
        except (OSError, AttributeError):
            pass


@contextlib.contextmanager
def _silence_fd1():
    """M-A：FreeCAD/OCCT 会向 fd1 写初始化/进度，污染 MCP JSON-RPC 通道。
    dup2 把 fd1 临时指向 fd2（stderr）保护协议帧（redirect_stdout 拦不住 C++ 层直写 fd1）。"""
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        yield
    finally:
        os.dup2(saved, 1)
        os.close(saved)


def _build_box_and_export() -> dict[str, Any]:
    import tempfile
    _prepare_freecad_import()
    out = os.path.join(tempfile.gettempdir(), "vibecad_smoke.step")
    with _silence_fd1():
        import FreeCAD  # noqa: PLC0415 - 懒加载：仅 conda runtime 进程内 import
        import Part  # noqa: PLC0415
        box = Part.makeBox(10, 10, 10)
        box.exportStep(out)
        bb = box.BoundBox
        result = {"ok": True, "volume": box.Volume, "bbox": [bb.XLength, bb.YLength, bb.ZLength],
                  "step": out, "freecad_version": list(FreeCAD.Version())}
    return result


@mcp.tool()
def smoke_cad() -> dict[str, Any]:
    """地基验证：进程内造 10×10×10 Box，导出 STEP，返回体积/包围盒/路径。"""
    if not _installer.is_ready():
        return {"ok": False, "message": "FreeCAD 运行时未就绪，请先调用 ensure_runtime"}
    if not _in_conda_runtime():
        return {"ok": False, "message": "运行时已就绪，但当前会话运行在引导解释器中，请重连本 MCP server 后再调用 smoke_cad"}
    return _build_box_and_export()


def main() -> None:
    if _auto_install_enabled():
        _spawn_install()
    mcp.run()


def _auto_install_enabled() -> bool:
    return os.environ.get("VIBECAD_AUTO_INSTALL", "") not in ("", "0", "false", "False")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 验证通过 + 删旧冒烟测试**
`uv run pytest tests/test_server_tools.py -v` → PASS（6 passed）；然后 `git rm tests/test_smoke.py`（其覆盖已并入新文件）。

- [ ] **Step 5: 提交** — `git add src/vibecad/server.py tests/test_server_tools.py && git commit -m "feat(server): runtime tools + _in_conda_runtime guard + win dll prep"`

---

## Task 8: 打包与依赖 `pyproject.toml`

**Files:** Modify `pyproject.toml`

- [ ] **Step 1: 改 4 处**（其余不动）

```toml
requires-python = ">=3.12"
```
```toml
dependencies = [
    "mcp>=1.12",          # 结构化 dict 输出需 ≥1.10，取 1.12 稳
    "pygltflib>=1.16.0",
    "trimesh>=4.0.0",
    "numpy>=1.26",        # 松约束：让 conda 的 numpy 满足，pip 不重装（M3）
    "pyyaml>=6.0",
]
```
```toml
[project.scripts]
vibecad = "vibecad.launcher:main"
```
```toml
[tool.ruff]
line-length = 100
target-version = "py312"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]   # m-5：整段替换勿丢失现有规则集

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra -m 'not slow'"
markers = ["slow: 真实下载 2-3GB 装 FreeCAD 的集成测试（需 VIBECAD_RUN_INTEGRATION=1）"]
```

- [ ] **Step 2: 同步并跑全部快测**
`UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv sync && uv run ruff check . && uv run pytest -q`
Expected: ruff 全过；pytest 全 PASS（slow 跳过）。dev venv 用系统 3.14（满足 >=3.12）——这是预期，单测不 import FreeCAD。

- [ ] **Step 3: 提交（含 uv.lock，m8）** — `git add pyproject.toml uv.lock && git commit -m "build: python>=3.12, entry=launcher, mcp>=1.12, slow marker"`

---

## Task 9: 本机集成验证 A1/A2/A3 `tests/test_runtime_integration.py`

**Files:** Create `tests/test_runtime_integration.py`

> 修 **B4**：A1 进程内 import 经 conda env python 子进程验证后回读（绝不在 pytest 解释器 import FreeCAD）；**M8**：PIP_SPEC 绝对路径。

- [ ] **Step 1: 写集成测试（@slow）**

```python
# tests/test_runtime_integration.py
"""真实安装并验证 A1/A2/A3（慢，下载 2-3GB）。
运行：VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow tests/test_runtime_integration.py -v -s
"""
import os
import subprocess
import pytest
from vibecad.runtime import paths, status
from vibecad.runtime.installer import RuntimeInstaller

pytestmark = pytest.mark.slow
_RUN = os.environ.get("VIBECAD_RUN_INTEGRATION") == "1"
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.skipif(not _RUN, reason="set VIBECAD_RUN_INTEGRATION=1")
def test_install_and_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "vchome"))
    monkeypatch.setenv("VIBECAD_PIP_SPEC", _REPO)  # 绝对路径装本地源（M8）
    phases = []
    RuntimeInstaller(on_progress=lambda s: phases.append(s.phase.value)).install()  # A2
    assert status.runtime_ready() is True
    assert status.health_check(paths.env_python()) is True  # A1（subprocess 级）

    # A1 进程内 import 经 conda env python 子进程执行后回读（不在 pytest 进程 import）
    step = str(tmp_path / "vc_smoke.step")
    code = (
        status._PREP +  # M-C：Windows DLL 兜底，否则 win CI 集成 import 必红
        "import FreeCAD, Part; b=Part.makeBox(10,10,10);"
        f"assert abs(b.Volume-1000.0)<1e-6; b.exportStep({step!r}); print('OK')"
    )
    r = subprocess.run([str(paths.env_python()), "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert os.path.exists(step)
    assert phases[-1] == "ready"
```

- [ ] **Step 2: 本机实跑（macOS arm64）——整份计划验收核心**
```bash
VIBECAD_RUN_INTEGRATION=1 UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
  uv run pytest -m slow tests/test_runtime_integration.py -v -s
```
Expected: PASS（首次约 5-15 分钟）。证明 **A1（进程内 import）+ A2（micromamba 装 freecad）+ A3（隔离 env 可用）** 在本机成立。

- [ ] **Step 3: 提交** — `git add tests/test_runtime_integration.py && git commit -m "test: slow integration validating A1/A2/A3 on real FreeCAD env"`

---

## Task 10: 平台矩阵 CI `.github/workflows/ci.yml`

**Files:** Create `.github/workflows/ci.yml`

> 修 **M4**：runtime-integration 增 `windows-latest`，真正验证 Windows import 链路。

- [ ] **Step 1: 写 CI**

```yaml
name: CI
on:
  push: { branches: [main] }
  pull_request: {}
jobs:
  lint-unit:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, ubuntu-24.04-arm, macos-latest, macos-13, windows-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync
      - run: uv run ruff check .
      - run: uv run pytest -q            # slow 默认跳过
  runtime-integration:
    needs: lint-unit
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]   # M4: 含 Windows 真验 DLL 链路
    runs-on: ${{ matrix.os }}
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync
      - name: install runtime + smoke (A1/A2/A3)
        env: { VIBECAD_RUN_INTEGRATION: "1" }
        run: uv run pytest -m slow tests/test_runtime_integration.py -v -s
```
> linux-aarch64 运行时集成成本高，本轮先靠本机/后续补（unit 矩阵已含）。

- [ ] **Step 2: 提交** — `git add .github/workflows/ci.yml && git commit -m "ci: five-platform unit matrix + win/mac/linux runtime integration"`

---

## Task 11: runtime 纯净 + 收尾（README、docs/plans、飞书）

**Files:** Modify `src/vibecad/runtime/__init__.py`, `README.md`; Create `tests/test_runtime_purity.py`, `docs/superpowers/plans/2026-06-08-runtime-installer.md`

> 修 **m7**：保证 `vibecad.runtime.*` 不拉 mcp（launcher 纯 stdlib 地基）。

- [ ] **Step 1: 改 `runtime/__init__.py`** 为只含 docstring（不 re-export 任何会触发 mcp 的符号），并写纯净回归测试：

```python
# tests/test_runtime_purity.py
import subprocess
import sys


def test_runtime_imports_without_mcp():
    # 模拟 launcher 在无 mcp 的临时 env：import runtime 子模块不得拉 mcp
    code = (
        "import sys, importlib;"
        "import vibecad.runtime.paths, vibecad.runtime.status, vibecad.runtime.platform,"
        " vibecad.runtime.micromamba, vibecad.runtime.installer;"
        "assert 'mcp' not in sys.modules, 'runtime 不应拉起 mcp';"
        "print('pure-stdlib OK')"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "pure-stdlib OK" in r.stdout
```

- [ ] **Step 2: 更新 README**「开发」段：`uvx vibecad` 启动 → 客户端调 `ensure_runtime` → 轮询 `get_runtime_status`（含 `needs_reconnect`）→ 重连 → `smoke_cad`；环境变量 `VIBECAD_HOME`/`VIBECAD_AUTO_INSTALL`/`VIBECAD_FREECAD_ENV`/`VIBECAD_PIP_SPEC`；陈旧锁手动清理路径 `<VIBECAD_HOME>/.install.lock`。
- [ ] **Step 3: 把本计划写入** `docs/superpowers/plans/2026-06-08-runtime-installer.md`。
- [ ] **Step 4: 同步飞书**（「过程文档」`DwYlfjYTelFG1RdTiFhc3NfWnAh`）：
```bash
lark-cli docs +create --api-version v2 --parent-token DwYlfjYTelFG1RdTiFhc3NfWnAh \
  --content @docs/superpowers/plans/2026-06-08-runtime-installer.md --doc-format markdown --format json
```
- [ ] **Step 5: 提交** — `git add src/vibecad/runtime/__init__.py tests/test_runtime_purity.py README.md docs/superpowers/plans/ && git commit -m "chore: runtime purity guard + plan/README docs"`

---

## Verification（端到端验收）

1. **单元层（快，全平台）**：`uv run ruff check . && uv run pytest -q` 全绿（platform/paths/micromamba/status/installer/launcher/server/purity，均不依赖真实 FreeCAD）。
2. **地基层（慢，本机 macOS arm64 必跑）**：`VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow tests/test_runtime_integration.py -v -s` → 真实装 FreeCAD，哨兵就绪，经 conda env python 子进程造 Box `Volume==1000.0` 导出 STEP。**验证 A1+A2+A3**。
3. **连接器层（手动，真实 MCP 客户端）**：
   ```bash
   claude mcp add --env VIBECAD_PIP_SPEC=/Users/wangtao/DevProject/VibeCAD \
     --transport stdio vibecad \
     -- uvx --from /Users/wangtao/DevProject/VibeCAD vibecad
   ```
   在 Claude Code 内：`ping`→秒回（证握手未被安装阻塞）；`ensure_runtime`→`started`，轮询 `get_runtime_status` 至 `ready` 且 `needs_reconnect:true`；按提示重连后 `smoke_cad`→`volume:1000`、STEP 路径（证 re-exec 进 conda python、进程内 FreeCAD 生效）。
4. **CI**：五平台 lint+unit 绿；ubuntu+macos+windows 运行时集成绿。

满足 1–3（尤其 2）即证明本轮达成、地基假设全部落地，可进入下一轮（语义 CAD 工具层 + 四 agent 接入）。

---

## 自审记录（v3）
- **两轮审查纳入**：第一轮 6 blocker+8 major、第二轮 1 blocker(B-1)+5 major(M-A~M-E)+7 minor 全部落到具体 Task；第二轮裁决 v2 已 12/14 修对、无误伤第一轮 verified_ok。
- **verified_ok 守护**：未改 `.sha256` 解析、120s 超时、`python=3.12/freecad=1.1.0` pin、micromamba 命令、`@mcp.tool()`/`mcp.run()`、re-exec 无循环。
- **类型一致**：`active_runtime_python/ready_sentinel/status_file`（paths）、`runtime_ready/write_status/read_status/health_check/FileLock`（status）、`RuntimeInstaller.is_ready/install/_run/_pip_spec`（installer）、`_in_conda_runtime/_prepare_freecad_import/_build_box_and_export`（server）跨 Task 一致。
- **范围纪律**：仅 4 工具 + 单零件 smoke，无装配/完整 CAD/agent 接入。

---

## 实施与验收记录（2026-06-08 · macOS arm64 实机）

本计划已按 11 个 TDD 任务全部实施（分支 `feat/runtime-installer`），单元测试 **38 passed**、ruff clean、launcher 纯净性回归通过。

**地基假设 A1/A2/A3 已端到端验证通过**（`VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow`，真实下载安装 `freecad=1.1.0`，耗时约 3:50）：

- **A2 成立**：micromamba 引导 + `micromamba create -c conda-forge --override-channels python=3.12 freecad=1.1.0` 成功（230 个 conda 包，含 occt 7.9.3）。
- **A1 成立（含一处真机修正）**：conda-forge 把 FreeCAD 的 Python 扩展模块装在 `<env_prefix>/lib`（Windows 为 `Library/bin` / `Library/lib`）而非 `site-packages`，故 `import FreeCAD` 默认 `ModuleNotFoundError`。**修复**：import 前把该目录注入 `sys.path`（`status._PREP` 与 `server._prepare_freecad_import` 对称实现）。修复后经 conda env python 进程内 `import FreeCAD, Part` 成功，`Part.makeBox(10,10,10).Volume ≈ 1000`、导出 STEP、`FreeCAD.Version()==1.1.0`。
- **A3 成立**：`pip install <vibecad 源>` 装入 conda env 后，`import vibecad.server`（连带 mcp 1.27.2）可起、`verify_runtime` 通过——证明 launcher re-exec 进 conda python 后 server 真能运行。

CI（`.github/workflows/ci.yml`）以五平台 unit 矩阵 + ubuntu/macos/windows runtime-integration 持续校验（Windows 真验 DLL / sys.path 链路）。
