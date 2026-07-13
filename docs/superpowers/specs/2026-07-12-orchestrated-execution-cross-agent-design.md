# Orchestrated Execution 跨 Agent Skill 设计

> 状态：Approved（2026-07-12；同日修订：本机不安装 OpenClaw）
> 日期：2026-07-12  
> 范围：先完成通用编排 Skill，再继续 VibeCAD Agent Harness  
> 原始 Skill：`~/.claude/skills/orchestrated-execution`

## 1. 背景与问题

现有 `orchestrated-execution` 已形成完整的长任务工程方法：计划拍板、subagent 委派、逐 commit 门禁、台账销账和跨会话快照。它的 `SKILL.md` 与四份 reference 已通过 Codex `quick_validate.py`，说明文件结构符合 Agent Skills 的基本约束。

问题不在 Markdown 格式，而在执行语义仍绑定 Claude：

- 计划固定指向 `~/.claude/plans` 与 Claude Plan Mode；
- 委派使用 Claude 的 `SendMessage`、模型名称和后台通知心智模型；
- `memory` 被假定为所有宿主都具备的原生能力；
- 长命令处置围绕 Claude 前台墙钟和 `run_in_background`；
- Skill 只安装在 `~/.claude/skills`，Codex、WorkBuddy、OpenClaw 无法共同发现。

用户的目标是：Claude、Codex、WorkBuddy、OpenClaw（“龙虾”）以及其他兼容 Agent Skills 的宿主，都能直接使用同一套编排方法，且不会因工具名称不同而幻觉调用不存在的能力。

## 2. 客观成功标准

本阶段完成时必须同时满足：

1. 只有一份 canonical Skill 内容，不维护四份独立 fork。
2. 根 `SKILL.md` 仅使用 Agent Skills 共同格式：`name`、`description`、Markdown 指令和一层 references。
3. Claude、Codex、WorkBuddy、OpenClaw 能发现同一个 canonical Skill。
4. Skill 启动后先识别宿主能力，再选择适配器；不得直接假定 Claude 工具存在。
5. 有原生 Plan/Agent/Memory/Process Session 时使用原生能力；没有时有确定性的串行降级路径。
6. 不论宿主能力如何，用户批准门禁、每 commit 验证、残差登记和状态快照不得降级消失。
7. Codex 完成 RED→GREEN 前向测试；Claude 与 WorkBuddy 完成发现测试和编排 smoke；OpenClaw 完成官方发现路径与静态兼容验证，runtime smoke 作为外部待验残差，不在本机安装 runtime。
8. Skill 本体通过结构校验、引用完整性检查和平台专属术语越界扫描。

## 3. 非目标

- 本阶段不实现 VibeCAD Agent Harness。
- 不重写各宿主自身的 agent runtime。
- 不保证所有宿主都支持并行 subagent；缺失能力时允许串行执行。
- 不把平台安装说明、历史复盘或用户文档塞进 `SKILL.md`。
- 不在本阶段发布公共 ClawHub/SkillHub/Codex Plugin；先完成本机通用版本与验证。
- 不在本机安装 OpenClaw；不得为了 smoke 扩大宿主安装范围。

## 4. 平台事实与共同基础

| 平台 | 已核实的个人 Skill 位置 | 共同格式 | 结论 |
|---|---|---|---|
| Claude | `~/.claude/skills/<name>/SKILL.md` | Agent Skills | 需从 Claude 路径链接到 canonical |
| Codex | `~/.agents/skills/<name>/SKILL.md` | Agent Skills；可选 `agents/openai.yaml` | 可直接读取 canonical，并支持 symlink |
| WorkBuddy | `~/.workbuddy/skills/<name>/SKILL.md` | `SKILL.md`；官方 find-skills 支持链接 `~/.agents/skills` | 需从 WorkBuddy 路径链接到 canonical |
| OpenClaw | `~/.openclaw/skills`、`~/.agents/skills`、workspace skills | AgentSkills-compatible | 可直接读取 canonical |

依据：

- [OpenAI：Build skills](https://learn.chatgpt.com/docs/build-skills)
- [Anthropic：Agent Skills in the SDK](https://code.claude.com/docs/en/agent-sdk/skills)
- [OpenClaw：Skills](https://docs.openclaw.ai/skills)
- 本机 WorkBuddy 官方 `find-skills` 插件：`~/.workbuddy/plugins/marketplaces/codebuddy-plugins-official/plugins/find-skills/skills/find-skills/SKILL.md`

## 5. 编号裁决

### D-1：canonical source = `~/.agents/skills/orchestrated-execution`

Codex 与 OpenClaw 原生扫描该目录，WorkBuddy 官方安装流程已支持从该目录建立链接。Claude 通过 `~/.claude/skills/orchestrated-execution` symlink 接入。

本机目标拓扑：

```text
~/.agents/skills/orchestrated-execution           # 唯一真实目录
~/.claude/skills/orchestrated-execution           # symlink → canonical
~/.workbuddy/skills/orchestrated-execution        # symlink → canonical
Codex                                              # 直接扫描 canonical
OpenClaw                                           # 直接扫描 canonical
```

安装切换必须先保留原 Claude Skill 的可恢复备份，并验证 symlink 后 Claude 仍可发现；不得直接删除唯一可用副本。

### D-2：共同核心与平台适配分离

根 `SKILL.md` 只定义编排不变量和能力探测流程。工具名、目录名、原生模式差异进入直接引用的 `platform-*.md`。

根 Skill 不得出现 `SendMessage`、`haiku/sonnet/opus`、`~/.claude/plans`、`run_in_background` 等平台专属执行指令；这些词只允许出现在对应 adapter 内。

### D-3：按能力选择路径，不只按产品名判断

激活后建立四项 capability profile：

```text
approval: native-plan | artifact-approval
delegation: spawn-send-wait | spawn-return-only | serial
persistence: native-memory | repo-artifact
process: native-session-poll | blocking-command | marker-poll
```

平台 adapter 提供已知映射；未知宿主读取 `platform-generic.md`，通过可用工具清单选择能力。不得通过猜测调用工具。

### D-4：“Plan Mode”改为“批准门禁”

核心契约是“任何写操作前必须存在用户批准的计划”，不是某个平台叫作 Plan Mode 的 UI 状态。

- 宿主提供原生 Plan Mode：进入该模式并同步计划。
- 宿主没有或当前无法切换：创建计划 artifact、向用户展示并等待明确批准。
- subagent 接到已批准任务书后不重复申请同一批准。

### D-5：repo artifact 是跨平台状态真源

原生 plan、todo、memory 只作为 UI 投影或缓存。跨宿主可恢复的真源为项目内滚动计划/台账：

```text
<repo>/docs/orchestrated/<campaign>.md
```

若项目已有计划目录（例如 VibeCAD 的 `docs/superpowers/plans`），服从项目约定。无仓库任务才回退到宿主个人 plan 目录。

长期结论优先写入仓库契约文档；原生 memory 仅保存指向该文档的短指针，避免不同宿主 memory 分叉。

### D-6：主循环“优先编排”，而非无条件禁止执行

- 支持 subagent：主循环负责计划、拍板、验收、故障处置和快照，执行/调研/审查优先委派。
- 不支持 subagent：主循环可串行执行，但必须保持任务书边界、独立门禁、残差登记和阶段关账。
- 只支持 spawn-and-return：任务拆成互不依赖单元，禁止设计需要 agent 间通信的流程。

因此“无多 Agent 能力”是性能降级，不是正确性降级。

### D-7：模型分级使用能力级名称

核心只使用 `fast / standard / deep`：

- fast：机械录入、格式转换；
- standard：常规实现与调研；
- deep：架构裁决、对抗审查、关键故障。

adapter 可映射到具体模型。宿主不允许选模型时，忽略分级而不报错。

### D-8：长任务优先宿主原生 session/poll

优先级：

1. 原生进程 session + poll/wait；
2. 可控的同步阻塞命令与合理 timeout；
3. 脚本落盘 + 唯一成功/失败标记轮询。

不得在支持 session/poll 的平台仍强制 `nohup`，也不得等待当前 agent 永远收不到的后台通知。

### D-9：平台 UI 元数据是可选附加层

`agents/openai.yaml` 用于 Codex/ChatGPT UI 展示与默认 prompt，其他平台应安全忽略。共同行为不得依赖该文件。

根 frontmatter 保持最小共同集合；不加入某一平台独占字段。

### G-1/G-2 实施补充

以下是对实施阶段的授权补充，不改写 D-1 至 D-9：

- **G-1 备份授权：** 迁移 Claude Skill 前，允许使用 UTC `STAMP` 创建唯一可恢复备份路径 `~/.claude/skill-backups/orchestrated-execution-${STAMP}`。备份必须先完成并校验，失败时按计划原位恢复。
- **G-2 私有仓库授权：** 允许创建私有仓库 `wangtao9090/orchestrated-execution`，并以 `~/.agents/skills/orchestrated-execution` 作为 canonical checkout；Skill 本体的 commit/push 在该私有仓库中完成。
- **OpenClaw 边界：** 本机不得安装 OpenClaw。仅进行官方发现路径、canonical package 和 adapter 的静态兼容验证，并将 runtime smoke 保留为外部待验项；不得改动 D-1 至 D-9 或扩大本机宿主安装范围。

## 6. 目标包结构

```text
orchestrated-execution/
├── SKILL.md
├── agents/
│   └── openai.yaml
├── references/
│   ├── planning.md
│   ├── delegation.md
│   ├── gates.md
│   ├── ledger.md
│   ├── platform-claude.md
│   ├── platform-codex.md
│   ├── platform-workbuddy.md
│   ├── platform-openclaw.md
│   └── platform-generic.md
└── scripts/
    └── validate-portability.py
```

现有 `planning.md`、`gates.md`、`ledger.md` 以最小改写为原则；`delegation.md` 是主要语义改造点。

`validate-portability.py` 只做确定性静态检查：

- 根 frontmatter 与名称合法；
- 所有直接引用存在且不逃逸 Skill 目录；
- 平台专属词只出现在允许的 adapter；
- `SKILL.md` 未超出约定长度；
- `agents/openai.yaml` 的名称与默认 prompt 对齐。

## 7. 运行流程

```text
触发 Skill
  → 读取根 SKILL.md
  → 盘点当前宿主可用工具与项目指令
  → 选择平台 adapter / generic adapter
  → 估算任务规模，选择最小子集或完整框架
  → 形成编号裁决 + commit 序列 + 门禁 + 文件白名单
  → 用户批准门禁
  → 按 capability profile 委派或串行执行
  → 每 commit 验证、commit、push、台账销账
  → 人工/实机验收
  → 状态快照与阶段关账
```

平台 adapter 只改变“如何调用能力”，不得改变“哪些门禁必须存在”。

## 8. 故障与降级

### 未识别宿主

读取 `platform-generic.md`，只使用已明确出现在工具清单中的能力。没有多 Agent 工具时走串行路径，不编造工具名。

### adapter 声明能力但实际缺失

立即回退 generic profile，登记一条环境残差；不得连续尝试同名不存在工具。

### subagent 无回报或通道异常

遵循三叉分诊，但故障计数以宿主实际状态为准：一次提醒、一次收敛、随后重派或主循环接管。重派前检查工作区副作用，所有门禁重跑。

### 无原生 memory

不视为阻塞。将快照完整写入 repo artifact；下一宿主从该文件恢复。

### 无法 push

保留已验证 commit，记录远程失败证据并请求用户处理授权/网络。不得把“本地已 commit”表述成“已 push”。

## 9. 安全边界

- Skill 不授予新权限，只编排宿主已有权限。
- 平台探测只能读取工具清单、环境标识和公开配置，不读取 token、密钥或私密 memory 内容。
- 安装脚本不得覆盖现有 Skill；存在冲突时先备份、校验、再原子切换。
- 对外部仓库、ClawHub 或 SkillHub 发布属于后续独立阶段，需要供应链审查与版本签名策略。
- subagent 任务书始终继承上级系统指令、项目 `AGENTS.md`/`CLAUDE.md` 与文件白名单，不用 Skill 绕过宿主权限模型。

## 10. 验证策略

### RED：无适配基线

至少记录以下失败：

1. Codex 直接读取旧 Skill 时引用 `SendMessage`、Claude Plan/Memory 或具体 Claude 模型。
2. 无 multi-agent 的假宿主面对“全部委派”要求时停机或编造工具。
3. 原生支持 session/poll 的宿主仍选择后台通知路径。

基线必须保存实际输出与失败分类，不能只写“预计会失败”。

### GREEN：同场景复测

适配后同一组场景应满足：

- Codex 使用实际可用的 plan、collaboration 和进程等待工具；
- Claude 继续使用其原生 Skill 路径与委派能力；
- WorkBuddy 能从 symlink 发现 Skill，不依赖 Claude 路径；
- OpenClaw 从 `~/.agents/skills` 发现 Skill；
- generic 宿主明确串行降级，不产生不存在的工具调用。

### REFACTOR：关闭新漏洞

对前向测试暴露的新幻觉工具名、重复审批、无限等待和状态分叉逐条加入 adapter 或静态检查，再运行相同场景。

### 验收门禁

1. Codex `quick_validate.py` 通过。
2. `validate-portability.py` 通过。
3. 所有 reference 路径解析通过。
4. Codex 压力场景全部通过。
5. Claude、WorkBuddy 发现测试通过；OpenClaw 官方扫描路径与 package 静态兼容检查通过，runtime smoke 明确登记为外部待验。
6. 原 Claude Skill 有可恢复备份；Claude、WorkBuddy symlink 与 Codex/OpenClaw 扫描目标均收敛到同一 canonical path/hash。

## 11. 修改边界

实施阶段仅允许修改或创建：

- `~/.agents/skills/orchestrated-execution/**`
- `~/.claude/skills/orchestrated-execution`（只做安全迁移/symlink）
- `~/.workbuddy/skills/orchestrated-execution`（只做 symlink）
- 本仓库的设计、计划和阶段台账文档

禁止顺手修改 VibeCAD 产品代码、其他个人 Skill、宿主配置或插件市场缓存。

## 12. 风险与熔断条件

- 原 Claude Skill 迁移后 Claude 无法发现：立即恢复备份，停止其他平台安装。
- canonical Skill 在任一宿主触发后调用不存在工具：计为语义红，不得以“宿主不兼容”销账。
- 计划外修复出现第 2 个：冻结实施，回到计划重排。
- 需要维护平台 fork 才能继续：熔断并复核共同核心边界。
- symlink realpath 被宿主安全策略拒绝：改用由 canonical 构建出的机械复制，并增加内容哈希等值门禁，不人工维护副本。

## 13. 后续阶段

本规格获书面批准后，下一步使用 `superpowers:writing-plans` 产出逐 commit 实施计划。Skill 完成 RED→GREEN→REFACTOR、安装和四端 smoke 后关账，随后才恢复 VibeCAD Agent Harness 的设计与实现。
