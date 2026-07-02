# R11 Spike 真机三验——用户操作卡（约 10 分钟）

> 背景：Round 11 的换芯（Task 3）与 manifest 定稿（Task 5）原被 Task 0 spike 硬门阻塞。
> **2026-07-02 夜更新：已用文档级调研（Claude Desktop 1.17377.2 源码反编译）形成预判并按其推进实现——
> Q1=是、Q2=否（C 分支监督进程已锁定）、Q3=否（运行时不放扩展目录内）。**
> 真机三验因此变为**确认性验证**：重点确认 Q2（若真机发现宿主居然会自动重启，C 分支仍正确工作只是白拿了保险，不用返工）；
> Q3 判读注意升级后 `${__dirname}` 可能指向新目录（spike_env 看 VIBECAD_HOME 实际指向，区分"目录漂移"与"数据被清"）。
> spike 扩展包已打好且已通过本地协议级预验（4 工具全部正常、自退机制坐实）。做完把三个结论告诉 Claude（代为核对预判并在计划文档确认）。

## 前置

- 测试机：装有 Claude Desktop 的 Mac（R10 验收用过的第二台 Mac 即可；本机也行，spike 扩展与正式 VibeCAD 扩展互不影响，验完可删）。
- 两个包（在**本仓库**目录里，如测试机是另一台，先把这两个文件传过去）：
  - `.vibecad/spike-r11/SpikeR11-0.0.1.mcpb`
  - `.vibecad/spike-r11/SpikeR11-0.0.2.mcpb`

## 步骤 1：装 0.0.1，验 Q1（env 展开）

1. 双击 `SpikeR11-0.0.1.mcpb` → Claude Desktop 弹窗 → Install。
2. 在 Claude Desktop 新对话里说：**"调用 spike_env 工具，把结果原样贴出来"**。
3. 记录：
   - `VIBECAD_HOME` 的值——**是扩展目录的绝对路径**（形如 `/Users/你/Library/Application Support/Claude/Claude Extensions/local.mcpb.…/runtime`）→ **Q1 = 展开成功**；显示 `${__dirname}/runtime` 字面量或 `<未设置>` → **Q1 = 失败**。
   - `pid` 数值（下一步对比用）。

## 步骤 2：验 Q2（自退后宿主是否自动重启）——硬门核心

1. 同一对话继续：**"调用 spike_mark"**（顺手为 Q3 埋标记）。
2. 然后：**"调用 spike_exit"**（server 会在 1 秒后自杀退出）。
3. **等 5 秒**，不要动任何设置、不要重启 Claude Desktop。
4. 继续说：**"再调用 spike_env"**。
5. 记录：
   - 调用**无任何手工操作**直接成功，且返回的 `pid` 与步骤 1 不同 → **Q2 = 是**（Task 3 走 D 分支，自退换芯 ~20 行）。
   - 报错 / 工具不可用 / 需要重启对话或 app 才恢复 → **Q2 = 否**（Task 3 走 C 分支，监督进程）。把具体现象记下来（报什么错、做了什么才恢复）。

## 步骤 3：装 0.0.2 升级，验 Q3（升级是否保留数据）

1. 双击 `SpikeR11-0.0.2.mcpb` → Install（覆盖升级，不要先卸载 0.0.1）。
2. 新对话里说：**"调用 spike_env 和 spike_check_mark"**。
3. 记录：
   - `spike_env` 的 `version` 应为 `0.0.2`（确认升级真的发生了）。
   - `spike_check_mark` 的 `exists` 为 `true` → **Q3 = 保留**；`false` → **Q3 = 清空**（升级即重下 2-3GB，手册需警示）。

## 收尾

- Claude Desktop → Settings → Extensions → 移除 "VibeCAD Spike R11"。
- 把三个结论（Q1/Q2/Q3 + 异常现象截图或描述）发给 Claude，会回填到
  `docs/superpowers/plans/2026-06-12-round11-auto-init-uninstall.md` 的"Spike 结果"节并按结论锁定 Task 3 分支，继续推进 R11。
