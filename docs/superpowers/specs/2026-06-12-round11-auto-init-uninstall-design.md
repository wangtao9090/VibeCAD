# Round 11 设计：安装初始化全自动 + 干净卸载

> 状态：已与用户确认方向与卸载形态（2026-06-12）。
> 前置：R10 已交付 .mcpb 一键安装（manifest v0.4 uv 类型，0.2.0）。
> 本轮目标：把"装好扩展之后"的体验从"手动触发安装 + needs_reconnect 死循环"修成"零手工、零重连、可干净卸载"。

## 1. 问题（真机复盘，2026-06-12 测试 Mac）

R10 真机安装暴露三个摩擦点：

1. **安装不自动开始**：扩展装好后无事发生，须用户开口"帮我准备 CAD 环境"才触发
   `ensure_runtime` 下载 2-3GB。
2. **装好后的"重连"是死路（核心）**：`launcher.py` 只在进程启动时判断一次跑哪个解释器；
   bootstrap 模式起来后永不换芯。运行时装好 → `needs_reconnect: true` → 但 Claude
   Desktop/Cowork 的"断开重连"**不杀旧 server 进程** → 死循环。真机上最终靠 Desktop
   Commander 手动 kill PID 才解围——普通用户会永久卡死。
3. **引导文案误导**："请重连本 MCP server"用户无法有效执行，引发"你说的是 FreeCAD
   这个 app 吗"级别的混乱。

另有用户新需求：**卸载功能**——把安装的东西全删除，且不要求用户在对话里操作。

## 2. 方案选型（已确认）

| 方案 | 思路 | 结论 |
|---|---|---|
| A worker 子进程 RPC | server 永跑引导解释器，FreeCAD 走 conda 子进程 RPC | 否决：22 工具全过 RPC 边界，大手术 |
| B 裸 execv 换芯 | ready 后直接 execv | 否决：新进程丢 MCP 握手态，客户端请求被拒 |
| **C 监督代理 + 握手重放** | 入口起纯 stdlib 监督进程，子进程换芯 + 重放 initialize | **备选主力**，spike 验证后上 |
| **D 自退 + 宿主重启** | bootstrap 检测 ready 自退，赌宿主自动重启 | **spike 先验**，达标则免去 C |

**Spike 硬门判定标准（D 合格线）**：bootstrap server 自退后，真机 Claude Desktop 上用户
**同一会话继续调工具，无任何手工操作即成功**（宿主自动重启进程 → launcher re-exec 进
conda → 客户端自动重握手）。达标 → D 收工（约 20 行）；不达标（connector 显示断开/需
手点重连/工具调用报错）→ 上 C。

## 3. 设计明细

### 3.1 自动开始安装

manifest.json `mcp_config.env` 加 `"VIBECAD_AUTO_INSTALL": "1"`（v0.4 schema 已核实
`env` 字段合法）。效果：宿主一拉起 server 即后台开始装运行时。`uvx vibecad` 等其他
客户端不受影响（默认仍需显式 `ensure_runtime`，不偷偷下 2-3GB）。

### 3.2 自动换芯

**自退时机**（D/C 通用）：server 在 `get_runtime_status` / `ensure_runtime` 返回 ready
响应后延迟 ~1s 自退（保证响应 flush 完）。自退退出码约定 **75**（区别于崩溃）。

**方案 C 形态（如 spike 判 D 不达标）**：

- `launcher` 升级为**监督进程**（纯 stdlib，禁重依赖）：spawn 真 server 为子进程，
  按行透传 stdio（MCP stdio 为 newline-delimited JSON，逐行分帧）。
- bootstrap 子进程 exit(75) → 监督进程换 conda python 重启子进程 →
  **重放 initialize 请求 + notifications/initialized**（丢弃重放的响应）→ 继续透传。
- 换芯窗口内客户端新请求先缓存，新子进程握手重放完再转发；旧子进程未及响应的请求
  由监督进程向新子进程重发（窗口内只会有只读轮询类请求，幂等安全）。
- 子进程非 75 退出 → 监督进程透传 EOF 退出（宿主看到断开，与现状一致，不掩盖崩溃）。
- **全平台统一监督进程**（用户拍板）：macOS/Windows/Linux 同一套代码路径，CI 三平台
  可测；launcher 现有的 Windows `subprocess.run` 包装顺势统一掉。Windows 注意管道
  二进制模式。
- `uvx vibecad` console script 走同一监督逻辑——Claude Code/Codex 用户同样受益。

### 3.3 卸载

**主机制：运行时跟着扩展走（用户拍板：不要求用户在对话里删除）。**

- manifest `mcp_config.env` 加 `"VIBECAD_HOME": "${__dirname}/runtime"` →
  FreeCAD 运行时落盘到扩展目录内部。
- 用户在 Claude Desktop 设置里 **Remove extension → 宿主删整个扩展目录 → 运行时连带
  全删**，一步到位零残留。
- 用户若曾在对话里删过运行时，Remove 时目录里本来就没有 runtime——自动判断天然成立。
- mcpb 无卸载钩子，此机制是唯一能做到"Remove 即全删"的路径。

**辅助工具 `uninstall_runtime`**（降级为辅助：释放空间但保留扩展的场景）：

- `destructiveHint=True`；**两段式确认**：不带 `confirm` → 只返回"将删除 X.X GB，
  位于 <路径>"；带 `confirm=true` 才真删。
- 删除范围 = `vibecad_home()` 整目录（conda env + micromamba + status.json + 日志）。
- **护栏**：`VIBECAD_FREECAD_ENV` 用户自带 env 绝不删；用户导出的 STEP/STL 在用户
  自己的目录，不在范围内。
- **运行中删除**：macOS/Linux 可直接删运行中文件；Windows 文件锁 → 统一为：写卸载
  标记 → server 自退（复用换芯机制）→ 重启后的 bootstrap 进程见标记执行实际删除。
- **CLI 救援通道**：`uvx vibecad --uninstall`（终端用户/扩展已删但全局运行时残留的
  场景，删全局 `vibecad_home()`）。

**风险（spike 增验两项）**：

1. `${__dirname}` 在 `mcp_config.env` 值中的变量展开是否真机生效（spike_ping 回显
   `VIBECAD_HOME` 即可验）。
2. **扩展升级行为**：若 Claude Desktop 升级扩展（装新版 .mcpb）是删目录重建而非原地
   覆盖，运行时被清，用户每次升级重新下载 2-3GB。spike 装两个版本测一次。若真删重建，
   再权衡退回全局目录方案（卸载体验降级为两步式）。

### 3.4 文案与 AI 引导

- `needs_reconnect` 场景消亡：字段保留恒 `false`（兼容旧文档），换芯全自动。
- 安装中的 guard 返回带进度百分比与阶段（"正在准备 CAD 环境：creating_env 40%"），
  AI 可自然转述。
- manifest 工具描述、USER_GUIDE、ACCEPTANCE_TESTS 同步更新。

### 3.5 测试与验收

- **单元**：监督代理分帧/重放用假子进程脚本测（不依赖 FreeCAD）；卸载标记/护栏纯单测。
- **慢测**：真机换芯端到端（bootstrap 起 → 装好 → 自动换芯 → smoke_cad 过）；
  卸载后目录不存在。
- **真机验收**：测试 Mac 全新安装 → 自动下载 → 零重连画图 → Remove extension 全删。
- **发布**：0.2.0 正式发布合并到 R11 完成后一起做（R10-T6 暂缓，避免发布两次）。

## 4. 终态体验

> 双击安装 .mcpb → 运行时后台自动下载（用户随口问进度）→ 就绪瞬间自动换芯 →
> 直接画图；不要了 → 设置里 Remove extension，一步全删。
> 全程零终端、零配置文件、零重连、零残留。

## 5. 模型分配（执行期）

- Spike + 监督代理核心（协议分帧/握手重放/跨平台管道）：opus/fable 级
- manifest/文案/测试样板/文档：sonnet 级
