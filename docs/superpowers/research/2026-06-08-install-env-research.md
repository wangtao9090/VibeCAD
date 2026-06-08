# VibeCAD 跨平台运行时安装器 + 多 Agent 接入落地设计

> 本文是综合 5 个维度(micromamba 引导 / conda-forge FreeCAD 平台矩阵 / A3 Python 对齐 / 四 Agent 接入 / uvx 分发与冷启动)研究+独立核验结果后的架构定稿。所有"高置信"事实均经一手核验,文中对存疑项做了显式标注。主工程师可据此直接拆解实现计划。

---

## 1. A3 最终推荐(全案地基):方案 (a) 的 re-exec 变体 —— 进程内 import,uvx 仅作引导器

**结论(果断):采用方案 (a) re-exec 变体。** `uvx` 只是零路径的引导壳,**真正的 MCP server 进程必须跑在 conda env 自带的 python 里**,从而在进程内 `import FreeCAD`,零序列化、零 IPC。方案 (b)(外部 python + 路径注入)判死刑,方案 (c)(freecadcmd 子进程)仅作崩溃隔离降级 **D1b**。

**为什么 (b) 不可行(核验确认):**
- conda-forge 的 `freecad` 对**每个 Python 版本单独编译**(osx-arm64 实测有 py310/py311/py312/py313/py314 五个 build),`.so` 写死 `python_abi 3.11.* *_cp311` 这类精确 CPython ABI,外部 python 版本差一位即无法加载。uvx 默认可能挑系统 3.14,必然不匹配。
- FreeCAD 还链入 OCCT 7.9 / Qt6 / VTK 9.6 / boost 1.88 等一大堆通过 conda prefix rpath 解析的原生库。即便强行对上 ABI,macOS **SIP 会在子进程启动时剥离 `DYLD_*` 继承**,运行期几乎无法补救。
- **而运行 env 自带的 python,Unix 下靠 RPATH/`$ORIGIN` 自动解析依赖库,无需 activate、无需任何 `LD_LIBRARY_PATH/DYLD_LIBRARY_PATH`** —— 这正是 (a) 优于 (b) 的根本原因。

**为什么 (a) 而非 (c):** headless `import FreeCAD`(非 Gui)完全可行(官方 `FreeCADCmd`/`FreeCAD -c` 即无 GUI 解释器),VibeCAD 只做几何建模 + glTF/STEP/STL 导出,不碰 `FreeCADGui`。(c) 同样要装 conda env(省不掉安装成本),却多了持久子进程 + RPC 开销并丢掉零序列化优势,故只配做降级。

### 进程模型与启动链路

```
┌─ Layer 0: uvx vibecad ────────────────────────────────────────────┐
│  uv 临时隔离 env(throwaway python,可能 3.14)                      │
│  执行 vibecad/__main__.py —— 瘦 launcher,纯 stdlib、零三方依赖     │
└───────────────────────────────┬───────────────────────────────────┘
                                │ launcher 决策(关键分叉)
        ┌───────────────────────┴────────────────────────┐
        │ 运行时已就绪?(conda env python 存在 + 健康检查)  │
        └───────────────────────┬────────────────────────┘
        YES(稳态,≈99% 启动)    │                NO(首次冷启动)
        ▼                        │                ▼
┌─────────────────────────┐      │      ┌──────────────────────────────────┐
│ 交棒到 conda python       │      │      │ 在【当前 uv python】上启动           │
│ POSIX: os.execv(condaPy, │      │      │ 【bootstrap server】               │
│   ['-m','vibecad.server']│      │      │ - 握手秒回                          │
│ Win:  subprocess + 透传   │      │      │ - 暴露 ensure_runtime /            │
│   stdio + 转发退出码       │      │      │   get_runtime_status + CAD 桩工具   │
│                          │      │      │ - ensure_runtime 后台装 env(进度)  │
│ → server 在 conda python │      │      │ - 装好后:衔接见下 §3 "首启桥接"      │
│ → import FreeCAD 懒加载   │      │      └──────────────────────────────────┘
│   (放在每个 CAD 工具内)    │
│ → 进程内、零序列化         │      稳态目标:所有后续启动都走左侧 in-process 快路径
└─────────────────────────┘
```

**要点:**
- **launcher 必须纯 stdlib**(它跑在 uv 临时 env,不能依赖 mcp/trimesh)。它只做:定位 VibeCAD home → 探测 env 健康 → execv 或起 bootstrap。
- **稳态握手为何快:** server 在 conda python 启动时**不** import FreeCAD;`import FreeCAD`(及 OCCT/Qt 加载,数秒)**懒放在每个 CAD 工具内部**,由工具调用超时(~60s)覆盖,首个 CAD 调用付一次冷加载代价即可。
- **Windows 不用 `os.execv`:** Windows 无真正 exec,会另起进程并可能闪退父进程 → 改用 `subprocess` + 继承 stdio 句柄 + 转发退出码,对 MCP client 同样透明。
- **Python 版本锁定 = `python=3.12`(定案)。** 五平台 freecad 1.1.0 均有 py312 build;MCP/FastMCP 依赖在 3.12 成熟稳定(3.14 上 mcp 传递依赖成熟度未核实);比 3.11 更前瞻。**必须显式 pin**,否则 conda solver 可能漂到 3.13/3.14 导致缓存 env 与预期 ABI 不一致。配 lockfile 锁定 `freecad` 与 `python`,定期人工升级。(保守可选 3.11;两者皆有完整 build。)
- **纯 pip 依赖(mcp/trimesh/pygltflib/vibecad)装进同一个 conda env**,复用 conda 的 numpy;**严禁**把 uv 临时 env(cp314)的 site-packages 注入 conda(cp312)python,否则 numpy 等 C 扩展 ABI 崩溃。

---

## 2. 跨平台安装器流程 + 五平台命令差异表

### 流程(检测 → 引导 → 建 env → 落盘)

1. **检测已有运行时(三级,按序):**
   1. **VibeCAD 托管 env**:`<HOME>/mamba/envs/vibecad/bin/python`(win: `...\python.exe`)存在 → 跑健康检查 `python -c "import FreeCAD, Part"` → 通过则**直接复用**(`micromamba create` 本身幂等,可安全重入)。
   2. **用户显式指定 env**(高级 opt-in):环境变量 `VIBECAD_FREECAD_ENV` 指向某 conda env → 校验其 python 版本 + `import FreeCAD` + 是否含我方 pip 依赖,通过才用。
   3. **系统 FreeCAD**(AppImage/.app/系统包):**仅作信息提示,默认不复用** —— 其内置 python 版本未知、无我方 pip 依赖、ABI 不可控,进程内 import 不可靠。坚持自建 pinned env 保证五平台可复现。
2. **下载 micromamba**:按平台选对应裸二进制(见下表),`chmod +x`(Unix)。**必须用 libcurl/命令行下载**(micromamba 走 libcurl,不打 `com.apple.quarantine`,Gatekeeper 不扫描);**严禁引导用户用浏览器手动下载**,否则 macOS 会被打 quarantine 触发拦截。
3. **建 env**:`micromamba create -y -r "$R" -p "$R/envs/vibecad" -c conda-forge --override-channels python=3.12 freecad=1.1.0`。`-r`(root prefix)与 `-p`(env prefix)**必须同卷**(pkgs 缓存硬链接到 env)。
4. **装 pip 依赖**:`micromamba run -r "$R" -p "$R/envs/vibecad" python -m pip install "mcp[cli]" trimesh pygltflib vibecad`。
5. **冒烟验证**:`micromamba run ... python -c "import FreeCAD, Part; print(FreeCAD.Version())"`(若用到装配再加 `import Assembly`)。
6. **落盘**:env 落在平台约定的应用数据目录(下表),**独立于 uv cache**(`uv cache clean` 不能误删这 2-3GB)。

### 表 A:micromamba 下载(2.8.0-0,五平台)

| 平台标识 | GitHub 裸二进制资源名(`releases/latest/download/<name>`) | 镜像(GET only)| 未压缩体积 |
|---|---|---|---|
| `linux-64` | `micromamba-linux-64` | `micro.mamba.pm/api/micromamba/linux-64/latest` | 18.05 MB |
| `linux-aarch64` | `micromamba-linux-aarch64` | `.../linux-aarch64/latest` | 21.76 MB |
| `osx-64` | `micromamba-osx-64` | `.../osx-64/latest` | 16.16 MB |
| `osx-arm64` | `micromamba-osx-arm64` | `.../osx-arm64/latest` | 14.45 MB |
| `win-64` | `micromamba-win-64.exe` | `.../win-64/latest`(tar.bz2 内 `Library/bin/micromamba.exe`)| ≈10.79 MB |

- 每个资源都有对应 `.sha256`,下载后**务必校验**。
- 镜像端点**只响应 GET**,用 `curl -L`;**勿用 HEAD 探活**(返回 405,会误判失败)。
- 实际 `.tar.bz2` 压缩包仅 ~4.5–8.3 MB。

### 表 B:落盘路径 / env 内可执行 / 动态库处理(五平台)

| 维度 | Windows (`win-64`) | macOS (`osx-arm64` / `osx-64`) | Linux (`linux-64` / `linux-aarch64`) |
|---|---|---|---|
| VibeCAD HOME / `MAMBA_ROOT_PREFIX` | `%LOCALAPPDATA%\VibeCAD\mamba` | `~/Library/Application Support/VibeCAD/mamba` | `${XDG_DATA_HOME:-~/.local/share}/VibeCAD/mamba` |
| env python | `<env>\python.exe` | `<env>/bin/python` | `<env>/bin/python` |
| freecadcmd(降级用)| `<env>\Library\bin\FreeCADCmd.exe` | `<env>/bin/freecadcmd` | `<env>/bin/freecadcmd` |
| import 前动态库处理 | **必须** `os.add_dll_directory(os.path.join(sys.prefix,'Library','bin'))`(Py3.8+ 不搜 PATH;必要时再加 `Library\lib`) | 无需(RPATH/`$ORIGIN` 自动解析;**勿设 `DYLD_*`**,SIP 会剥离) | 无需(RPATH/`$ORIGIN` 自动解析) |
| 离屏渲染(仅触及 Gui/OpenGL 时)| 一般不需要 | 一般不需要 | 需渲染才设 `QT_QPA_PLATFORM=offscreen` + `LIBGL_ALWAYS_SOFTWARE=1` |

- **平台支持边界(核验):** conda-forge freecad 仅这 5 个 subdir,**无 win-arm64、无 linux-ppc64le**。Windows on ARM 需走 x64 仿真(未验证)或暂不支持。
- **环境体积预期:** 完整 env(OCCT/Qt6/VTK/PySide6/numpy 等)实际约 **2–3GB**(研究给的 1.5–2.5GB 偏低),首次解析下载较重,冷启动 UX 必须按此设计。
- **`import FreeCAD` 开箱即用(核验):** 当前 feedstock `INSTALL_TO_SITEPACKAGES=ON`,FreeCAD/Part/Assembly 均在 site-packages,**不再需要旧 0.18 时代的 `.pth`/`sys.path` hack**。
- **防御性:** server 启动早期可统一设 `QT_QPA_PLATFORM=offscreen` 兜底,避免任何功能隐式拉起 GUI 在无显示环境崩溃;并避免调用任何 `FreeCADGui` 路径。

---

## 3. 冷启动 UX:不破坏 MCP 握手超时

**核心约束(核验,MCP 2025-06-18 Lifecycle):**
- `initialize` 必须是首个交互,server 必须回 capabilities;**在收到 `initialized` 通知前,server 只能发 ping/logging** → 握手阶段**根本发不出 progress**(progress 必须挂在带 `progressToken` 的某个请求上)。
- 规范不定死超时数值,但主流客户端实测约 **60s**(错误码 `-32001`,`data.timeout:60000`),可配但**不可依赖** → **握手必须秒回**。
- 因此:**FreeCAD 的 2-3GB 安装绝不能放在 import / FastMCP lifespan startup / initialize 处理路径上**,否则反复连接失败。

**设计(定案):**
1. **模块级保持轻量**:`FastMCP("vibecad")`,只注册工具、探测运行时是否就绪;不 import FreeCAD、不触发任何下载。
2. **`ensure_runtime` 工具 = 后台任务模型(默认)**:2-3GB 在慢网必超单次工具超时,故 `ensure_runtime` **启动后台线程**执行 micromamba 引导并**立即返回** `{"status":"started"}`;客户端轮询 **`get_runtime_status`**(返回 `phase`(resolving/downloading/extracting/verifying)+ `percent`)。同时对支持渲染进度的客户端额外 `await ctx.report_progress(progress, total, message)`(需 client 带 `progressToken` 才渲染,不能假定一定显示/续命)。
3. **CAD 桩工具**:运行时未就绪时,所有 CAD 工具返回结构化提示「FreeCAD 运行时未就绪,请先调用 `ensure_runtime` / 正在安装 xx%」。
4. **并发锁**:多客户端同时首启 → 对 env 构建目录加**文件锁**,避免重复下载/竞态;重入复用同一 env(create 幂等)。
5. **落盘独立于 uv cache**:env 目录在 §2 表 B 的 VibeCAD HOME 下,`uv cache clean` 不触发重下 2-3GB。
6. **预热策略(产品取舍,建议默认关闭静默下载)**:默认走显式 `ensure_runtime`(尊重带宽同意);提供 `VIBECAD_AUTO_INSTALL=1` opt-in,在 server 首次启动即**非阻塞**后台预热,使用户首次调 CAD 工具时大概率已就绪。

**首启桥接(env 装好后,本会话如何用上 FreeCAD):**
- **MVP(推荐先做,代码最少):** `ensure_runtime` 完成后,返回「运行时已就绪,请重启/重连本 MCP server」;客户端下次 spawn `uvx vibecad` 时,launcher 走 §1 in-process 快路径。诚实告知:同一 stdio 会话不能热切换解释器。
- **无缝桥接(后续打磨):** bootstrap server 在 env 就绪后**起一个常驻 conda-python worker**(`micromamba run ... python -m vibecad.worker`),本会话剩余的 CAD 调用通过本地管道/JSON-RPC 代理给它(临时进入 D1b 越进程模式);**下次启动**自动回到最优 in-process。

---

## 4. 四 Agent 接入矩阵

**通用前置(所有 stdio 方案):** 宿主进程 PATH 需有 `uv/uvx`。GUI 客户端 PATH 受限会报 `spawn uvx ENOENT` → **稳妥写法用 uvx 绝对路径**:macOS/Linux `~/.local/bin/uvx`,Windows `%USERPROFILE%\.local\bin\uvx.exe`(必要时 `cmd /c uvx`)。无 uv 用户回退:`curl -LsSf https://astral.sh/uv/install.sh | sh` / `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"` / `brew|pipx|pip install uv`,或 `pipx run vibecad` 等价替代。

| Agent | 身份核验 | MCP 支持 | 配置文件路径 | 需向用户确认 |
|---|---|---|---|---|
| **Claude Code** | 官方,高置信 | stdio,官方文档逐字证实 | local/user → `~/.claude.json`;project → 项目根 `.mcp.json` | 无(直接落地) |
| **Claude Cowork** | Anthropic 真实产品,高置信 | **已支持本地 MCP**(核验纠正:官方《How we contain Claude》已将本地 MCP 移出沙箱 VM、在宿主机运行,与 Desktop 一致)+ 远程连接器 | 大概率共用 Claude Desktop 配置:macOS `~/Library/Application Support/Claude/claude_desktop_config.json`;Win `%APPDATA%\Claude\claude_desktop_config.json`(**待实机确认是否独立 UI**) | 产品内注册本地 MCP 的确切 UI/路径需实机确认;第三方博客的 `type:"sdk"` 桥接/supergateway 说法已过时,**剔除** |
| **OpenClaw** | 高置信(GitHub API 实证:`openclaw/openclaw`,377,572 star,2025-11-24,TypeScript,homepage openclaw.ai)| stdio,docs 证实 `mcp.servers.<name>` 键控对象 | `~/.openclaw/openclaw.json`;或 CLI `openclaw mcp add` | schema 快速演进,以 `openclaw mcp add` CLI 实际产出为准;**注意它会拦截 `NODE_OPTIONS/PYTHONSTARTUP/PYTHONPATH` 等解释器启动类 env** |
| **WorkBuddy** | 中置信(腾讯 CodeBuddy 团队桌面 Agent,2026-03-09,codebuddy.cn/work,兼容 OpenClaw skills)| 中置信:支持 STDIO/SSE,设置→MCP→Add MCP 粘贴 JSON(CodeBuddy `mcpServers` 格式)| **低置信**:本地 stdio 配置文件确切磁盘路径无一手文档(`~/.workbuddy/` 仅证实 `models.json` 管模型;CodeBuddy 用户级 `~/.codebuddy.json`)| 桌面 UI 是否完整开放本地 stdio(部分中文教程偏重 SSE)、确切配置路径、面向国际用户可用性,均需**实机冒烟** |

### 配置片段(以 `uvx vibecad` 为例)

**Claude Code** — CLI(最稳)+ 项目级 `.mcp.json`
```bash
claude mcp add vibecad -- uvx vibecad                 # local(默认,仅当前项目)
claude mcp add --scope user vibecad -- uvx vibecad    # user(跨所有项目)
# 验证:claude mcp list / claude mcp get vibecad
```
```jsonc
// 项目根 .mcp.json(可提交 git 共享,首次使用弹审批)
{ "mcpServers": { "vibecad": { "command": "uvx", "args": ["vibecad"], "env": {} } } }
```

**Claude Cowork** — 经 Claude Desktop 配置(本地 MCP 现运行于宿主机)
```jsonc
// macOS: ~/Library/Application Support/Claude/claude_desktop_config.json
// Win:   %APPDATA%\Claude\claude_desktop_config.json
{ "mcpServers": { "vibecad": { "command": "uvx", "args": ["vibecad"] } } }
```
> 改完完全退出并重启;**确切产品内注册机制需实机确认**。仅当需让无本机环境的设备/云端访问时,才退回远程 HTTP MCP(`supergateway --stdio "uvx vibecad"` 转 HTTP,再加为自定义连接器)。

**OpenClaw** — `~/.openclaw/openclaw.json`
```jsonc
{ "mcp": { "servers": { "vibecad": { "command": "uvx", "args": ["vibecad"], "env": {} } } } }
```
```bash
openclaw mcp add vibecad --command uvx --arg vibecad   # 等价 CLI
openclaw mcp doctor vibecad --probe                     # 验证;改完重启 gateway
```

**WorkBuddy / CodeBuddy** — 设置 → MCP → Add MCP 粘贴 JSON
```jsonc
{ "mcpServers": { "vibecad": {
  "type": "stdio", "command": "uvx", "args": ["vibecad"], "env": {},
  "description": "VibeCAD FreeCAD MCP server" } } }
```
> 以 UI 内 JSON 编辑器为准(本地配置文件磁盘路径未确认)。

---

## 5. 风险与回退

| 高风险点 | 缓解 / 回退 |
|---|---|
| **A3 进程内 import 失败**(ABI 边界、OCCT 段错误拖垮 server)| 自动降级 **D1b**:`<env>/bin/freecadcmd` 或常驻 conda-python worker + JSON-RPC,进程隔离防段错误;同一 env、安装成本不变。开关 `VIBECAD_ISOLATION=subprocess` 或连续 import/segfault 后自动切换 |
| **Windows `os.execv` 不可靠**(无真 exec、闪退父进程)| Windows 改 `subprocess` + 继承 stdio + 转发退出码 |
| **Windows import 找不到 DLL** | import 前 `os.add_dll_directory(<prefix>/Library/bin)`,否则五平台一致性破裂 |
| **macOS 被 Gatekeeper 拦截** | 坚持 libcurl/micromamba 自动下载(不打 quarantine);**禁止引导浏览器手动下载**;干净 Apple Silicon 机实测一次 import 兜底 |
| **目标平台无 freecad**(win-arm64 / ppc64le)| 启动检测 arch,不支持则优雅报错;win-arm64 可尝试 x64 仿真(未验证,需实测)|
| **linux-aarch64 build 完整性**(qt6/vtk)| 五平台 CI 各跑 `import FreeCAD, Part, Assembly` 冒烟,arm64-linux 重点真机核实 |
| **conda solver ABI 漂移**(py3.11→3.14)| 显式 `python=3.12 freecad=1.1.0` + lockfile 锁定,定期人工升级 |
| **跨 env site-packages ABI 冲突** | 纯 pip 依赖只装进 conda env,绝不跨 env 复用 uv 临时 env 的 site-packages |
| **MCP 握手被 ~60s 超时杀掉** | 安装绝不放 import/lifespan/initialize;走 `ensure_runtime` 后台 + `get_runtime_status` 轮询 |
| **`uv cache clean` 误删 2-3GB env** | env 落盘目录独立于 uv cache(§2 表 B)|
| **多客户端并发首启竞态** | env 构建目录文件锁;create 幂等重入 |
| **`ensure_runtime` 单次调用超工具超时** | 默认后台任务 + 状态轮询,而非同步阻塞 |
| **Agent 不支持/受限 stdio**(Cowork 旧版/WorkBuddy 路径未知)| 回退远程 HTTP MCP(supergateway stdio→HTTP)或文档化 UI 粘贴;落地前对 Cowork/OpenClaw/WorkBuddy 各做实机冒烟 |
| **Assembly API 不稳/文档薄弱** | 若"对话式装配"为核心卖点,先做五平台端到端 PoC(`asm.solve()` 绑定签名、各 JointType 的 Reference1/2 subname 搭建)再排期 |
| **freecad 1.1.1 尚未上 conda-forge** | 版本 pin 预留升级口径,不硬编码假定 1.1.1 已有;待 feedstock 发布再跟进 |
| **`uvx --from git+...#subdirectory=` 子目录运行不可靠** | 不承诺 monorepo 子目录一行运行;优先发 PyPI(`uvx vibecad`),或 clone 后 `uv run` |

---

## 6. 需向用户澄清的开放问题(精炼)

1. **Python 版本锁定**:确认采用 `python=3.12`(本案推荐,兼顾成熟度与前瞻)?还是保守锁 3.11?是否需兼容仅 py3.9 的 1.0.0 老链路?
2. **首启 UX 策略**:2-3GB 首次下载,默认走**显式 `ensure_runtime`**(尊重带宽同意),还是允许 `VIBECAD_AUTO_INSTALL` 静默预热?首启桥接先做 MVP(提示重连)还是直接做无缝 worker 桥接?
3. **目标 Agent 范围与优先级**:首发是否仅官方支持 **Claude Code**,Cowork/OpenClaw/WorkBuddy 列为"实验性、待实机冒烟"?(三者的配置路径/UI 有不同程度存疑)
4. **"对话式装配"是否核心卖点**:若是,是否同意先排一个 Assembly 端到端 PoC 再决定排期(官方 Python 建模 API 文档薄弱)?
5. **是否复用用户已装的 FreeCAD**:默认**自建 pinned env**(可复现);是否需要保留 `VIBECAD_FREECAD_ENV` 高级 opt-in 覆盖?
6. **分发渠道**:确认以 **PyPI 发布 `vibecad`**(`uvx vibecad` 零路径叙事)为主?包名 `vibecad` 是否已确认可用?预发布期是否需 git 入口兜底?
