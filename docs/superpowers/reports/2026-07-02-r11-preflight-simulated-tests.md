# R11 前夜模拟测试报告（2026-07-02 夜）

> 三条线全部 PASS。目的：把真机测试前能模拟的链路全部预演一遍，确保次日真机三验/验收不因包或代码本身的问题白跑。
> 环境：本机 macOS arm64（开发机），全部在临时目录隔离执行，未触碰任何真实用户数据目录。

## 结论一览

| 测试线 | 结论 | 覆盖 | 真机仍需验证 |
|---|---|---|---|
| A. Release .mcpb 冒烟 | ✅ PASS | v0.2.0 发布包下载→解包→按 manifest mcp_config 启动→MCP 握手→工具调用 | ensure_runtime 2-3GB 真下载、装完换芯链路 |
| B. Spike 包预验 | ✅ PASS | 两包差异、4 工具协议行为、自退机制、升级保留判定逻辑 | Q1/Q2/Q3 的宿主端行为（Claude Desktop） |
| C. uninstall CLI e2e | ✅ PASS | 直删/护栏拒删/TTY 取消/非 TTY 直删/目录不存在 5 场景（真实进程真删 tmp） | 运行时文件被占用时的部分删除分支 |

## A. Release .mcpb 冒烟（模拟双击安装后的启动）

- `gh release download v0.2.0` → VibeCAD.mcpb 111,106 字节，解包内容完整。
- manifest：version=0.2.0、22 工具、`server.mcp_config` 无 env（0.3.0 才加）。
- 按 mcp_config 启动（`${__dirname}` 替换为解包目录，模拟宿主展开）：uv 建 venv 装 49 包约 20s，stdio 正常拉起。
- initialize 成功；**serverInfo.version=0.2.0（R10 版本上报修复在发布包上生效）**；protocolVersion 2025-11-25。
- list_tools 22 个，与 manifest.tools 双向对比差集为空。
- `ping` → "vibecad ok (v0.2.0)"；`get_runtime_status`（runtime 未装）→ 结构化 `{phase: not_started, percent: 0, needs_reconnect: false}`，不炸。

## B. Spike 包预验（保障真机三验）

- 两包 diff 干净：仅 manifest version 与 spike_env version 两处 0.0.1→0.0.2；包内无 .venv 杂物。
- 0.0.1：spike_env 的 VIBECAD_HOME 与展开路径逐字符相等、AUTO="1"、pid 正常；spike_mark 落盘 persist.mark 实测存在；spike_check_mark exists=true。
- **自退机制坐实**：spike_exit 返回提示文本后 1 秒进程完全消失（非僵尸）、连接关闭——Q2 的"自退端"没有问题，真机只剩验证宿主是否自动拉起新进程。
- 0.0.2：version="0.0.2"；指向 0.0.1 同一 runtime 目录时 spike_check_mark exists=true——"升级保留"判定的工具逻辑面正确。

## C. uninstall CLI 端到端（真实进程、真删 tmp 目录）

5 场景全过：直删（freed_mb=5.2 与实际吻合、目录真删）、护栏拒删（"目录不含 VibeCAD 安装产物"、用户文件完好）、伪 TTY 输 n 取消、非 TTY 无 --yes 直删（计划语义）、目录不存在（ok:true "无需卸载"）。

**逮到 3 个真实缺陷，已当夜修复（commit `6d691a0`）：**
1. TTY 确认按 Ctrl-D → `input()` EOFError 未捕获打印 traceback → 已改为视为取消；
2. 护栏拒删 ok:false 时退出码仍 0 → 已改为 `sys.exit(1)`；
3. 删 5MB 显示"释放约 0.0 GB" → 已改为 MB/GB 量级自适应。

## 给明天真机测试的提醒（汇总）

1. **首启耐心**：干净真机 uv 要下载 Python/依赖，等 30-60s+ 再判定失败；Claude Desktop 报启动超时直接重试一次（uv 缓存续传）。国内网络拉 PyPI 慢时，可在系统环境设 `UV_DEFAULT_INDEX`（清华镜像）。
2. **server 起不来先怀疑 PATH**：GUI 启动的宿主 PATH 通常不含 `/opt/homebrew/bin`，找不到 `uv` 不是包的问题（包本身本地已跑通；且 R10 真机装过说明该机环境 OK）。
3. **Q3 判定注意 `${__dirname}` 漂移**：升级后宿主的扩展目录可能换新路径，`VIBECAD_HOME=${__dirname}/runtime` 随之漂移——若 spike_check_mark 返回 false，先看 spike_env 的 VIBECAD_HOME 实际指向，区分"目录漂移"与"数据被清"两种结论（对 R11 方案含义不同）。
4. **Q2 操作要点**：spike_exit 后等 5 秒直接再调 spike_env，pid 变化且无手工操作 = Q2 通过（D 分支）。
5. TTY 确认卸载时按 n 或回车取消即可（Ctrl-D 的 traceback 已修，但真机装的是 0.2.0 旧包，别用 Ctrl-D）。
6. 真实运行时 GB 级，卸载删除前的体积统计会有可感知停顿，勿误判卡死。

## 测试产物（scratchpad，复用/复查用）

- `mcpb-smoke/smoke_test.py` — 可复用于 0.2.x/0.3.x 发布回归
- `spike-preflight/preflight_client.py` + v001/v002 解包目录
- `uninstall-e2e/` — 已清理
