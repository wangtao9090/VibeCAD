# Orchestrated Execution Cross-Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Claude 版 `orchestrated-execution` 改造成一份可版本化、按宿主能力探测、能被 Claude、Codex、WorkBuddy、OpenClaw 和兼容 Agent Skills 宿主共同发现的 canonical Skill。

**Architecture:** 以 `~/.agents/skills/orchestrated-execution` 作为唯一运行时 canonical checkout，根 `SKILL.md` 只保存宿主无关的不变量和 capability router，平台工具名全部下沉到五份 adapter。行为改造遵循 Skill TDD：先对旧 Skill 做跨模型 RED 压力样本，再实现静态 validator、共同核心和 adapters，最后用相同样本做 GREEN/REFACTOR，并在原子迁移后完成逐宿主 smoke。

**Tech Stack:** Agent Skills Markdown/YAML、Python 3 标准库 `unittest`、Git/GitHub CLI、POSIX symlink、Codex CLI 0.144.1。

## Global Constraints

- 已批准规格：`docs/superpowers/specs/2026-07-12-orchestrated-execution-cross-agent-design.md`，D-1 至 D-9 全部具有约束力。
- 不修改 VibeCAD 产品代码、其他个人 Skill、宿主配置、插件缓存或 `vibecad-agent-harness-learning.xml`。
- 所有 Skill 正文修改必须在真实 RED 证据之后；不得先写新 Skill 再补测试。
- 根 `SKILL.md` 与四份共同 reference 不得出现平台工具名、平台模型名或平台个人目录。
- repo artifact 是状态真源；原生 plan/memory 仅作投影或指针。
- 无 subagent 时只降级执行性能，不得取消批准、验证、残差、快照或关账门禁。
- 每个实现任务由独立 implementer 完成，随后依次过规格符合性审查和 `code-reviewer` 质量审查；两道审查均通过才提交。
- 每组逻辑变更显式逐文件 `git add`，英文 commit message，门禁通过后立即 commit 并 push；禁止 `git add -A`。
- 同一 canonical checkout 的写任务串行；RED/GREEN 只读样本可以并行。
- 计划外修复出现第 2 个、任一非预期红、迁移后 Claude 无法发现 Skill，立即熔断并回到计划。

---

## 执行前必须拍板的三个硬门禁

本计划不掩盖当前环境缺口。用户批准执行时必须同时明确以下三项；任一未明确，实施停在 Task 0。

| 编号 | 事实 | 推荐裁决 | 未获授权时 |
|---|---|---|---|
| G-1 | 已批准规格要求持久备份，但 §11 未列备份目录 | 授权由 UTC 变量 `STAMP` 生成的 `~/.claude/skill-backups/orchestrated-execution-${STAMP}` 作为唯一新增备份路径 | 不迁移 Claude Skill |
| G-2 | `~/.agents/skills` 不是 Git 仓库，无法满足“每次代码变更 commit + push” | 创建私有仓库 `wangtao9090/orchestrated-execution`，并让 canonical 路径成为该仓库 checkout | 不修改 Skill 本体 |
| G-3 | 本机没有 OpenClaw runtime，且用户明确要求不安装 | 只做官方发现路径、canonical package 与 adapter 静态兼容验证；runtime smoke 登记为 `DEFERRED_EXTERNAL` | 禁止安装 OpenClaw，也不得声称 runtime 已验证 |

G-1 与 G-2 属于本计划对已批准规格的明确实施补充。G-3 以用户最新指令为准：本机不安装 OpenClaw；外部 smoke 残差不阻塞本机 Skill 关账与恢复 VibeCAD Agent。

## Codex 子 Agent 模型路由

执行中不用主会话承担重复工作。可显式选择模型的 Codex 子进程统一使用下表；模型不可用时记录实际 fallback，不编造已选模型。

| 能力级 | Codex 模型与推理强度 | 任务 |
|---|---|---|
| `deep` | `gpt-5.6-sol` + `high` | task-coordinator、架构裁决、最终 code-reviewer、安全迁移审查 |
| `standard` | `gpt-5.6-terra` + `high` | Skill/validator 实现、规格审查、常规调研 |
| `fast` | `gpt-5.6-luna` + `medium`；涉及边界条件时升 `high` | 重复 pressure runs、格式核对、证据归档 |

官方依据：GPT-5.6 [模型目录](https://developers.openai.com/api/docs/models) 与 Codex [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)。应用内 `spawn_agent` 没有模型参数时，不声称已完成路由；需要精确路由的 standard 任务使用 `codex exec --model gpt-5.6-terra -c model_reasoning_effort="high"`，其余档位按上表机械替换。

## 文件与仓库边界

### VibeCAD 证据仓库

- Create: `docs/orchestrated/orchestrated-execution-portability-validation.md`
- Create: `docs/orchestrated/evals/orchestrated-execution/codex-native.md`
- Create: `docs/orchestrated/evals/orchestrated-execution/generic-serial.md`
- Create: `docs/orchestrated/evals/orchestrated-execution/native-session-poll.md`
- Create: `docs/orchestrated/evals/orchestrated-execution/results/red/*.txt`
- Create: `docs/orchestrated/evals/orchestrated-execution/results/green/*.txt`
- Modify: `docs/superpowers/specs/2026-07-12-orchestrated-execution-cross-agent-design.md`（状态改为 Approved，并记录 G-1/G-2 补充）

### 私有 Skill 仓库与运行时 canonical

- Create/Modify: `/Users/wangtao/.agents/skills/orchestrated-execution/SKILL.md`
- Create: `/Users/wangtao/.agents/skills/orchestrated-execution/agents/openai.yaml`
- Modify: `/Users/wangtao/.agents/skills/orchestrated-execution/references/planning.md`
- Modify: `/Users/wangtao/.agents/skills/orchestrated-execution/references/delegation.md`
- Modify: `/Users/wangtao/.agents/skills/orchestrated-execution/references/gates.md`
- Modify: `/Users/wangtao/.agents/skills/orchestrated-execution/references/ledger.md`
- Create: `/Users/wangtao/.agents/skills/orchestrated-execution/references/platform-claude.md`
- Create: `/Users/wangtao/.agents/skills/orchestrated-execution/references/platform-codex.md`
- Create: `/Users/wangtao/.agents/skills/orchestrated-execution/references/platform-workbuddy.md`
- Create: `/Users/wangtao/.agents/skills/orchestrated-execution/references/platform-openclaw.md`
- Create: `/Users/wangtao/.agents/skills/orchestrated-execution/references/platform-generic.md`
- Create: `/Users/wangtao/.agents/skills/orchestrated-execution/scripts/validate-portability.py`
- Create: `/Users/wangtao/.agents/skills/orchestrated-execution/tests/test_validate_portability.py`

`tests/` 是对规格 §6 的唯一结构补充，用于满足 Python validator 的代码 TDD；批准本计划即批准该目录。

### 安装拓扑与备份

- Replace reversibly: `/Users/wangtao/.claude/skills/orchestrated-execution` → symlink
- Create: `/Users/wangtao/.claude/skill-backups/orchestrated-execution-${STAMP}`
- Create: `/Users/wangtao/.workbuddy/skills/orchestrated-execution` → symlink
- Temporary only: `/private/tmp/orchestrated-execution-port-${STAMP}/**`

## Commit 序列

| # | 仓库 | Commit | 独立门禁 |
|---|---|---|---|
| V1 | VibeCAD | `test(skill): capture cross-agent portability baseline` | 15 个 RED 输出存在并逐份人工评分；规格状态已更新 |
| S0 | Skill | `chore: import legacy orchestrated execution skill` | 与原 Claude Skill 的 5 文件 SHA-256 完全一致 |
| S1 | Skill | `test: add portability validator coverage` | validator unittest 全绿；legacy package 静态验证按预期红 |
| S2 | Skill | `refactor: make orchestration core host-neutral` | core 禁词为零；unit tests 全绿；仅缺 adapters/UI 的预期红 |
| S3 | Skill | `feat: add platform capability adapters` | quick validator、unit tests、portability validator 全绿 |
| S4.n | Skill | `fix: close ${FAILURE_SLUG} loophole` | 每个新漏洞先复现，再最小修订，同场景复绿 |
| V2 | VibeCAD | `test(skill): record portable orchestration behavior` | 15/15 GREEN；所有 flagged match 已人工读码 |
| V3 | VibeCAD | `chore(skill): record canonical skill installation` | backup 可恢复；Claude/WorkBuddy/canonical realpath 同源 |
| V4 | VibeCAD | `test(skill): record cross-host discovery smoke` | 四宿主 runtime smoke 均有真实输出；无伪造通过 |
| V5 | VibeCAD | `docs(skill): close cross-agent skill rollout` | 两仓库干净、全门禁绿、两道最终审查通过 |

`S4.n` 的 commit 尾词只能来自 RED/GREEN 输出中的实际失败分类，例如 `serial-fallback` 或 `approval-gate`，不能预先臆造。

---

### Task 0: 冻结执行锚点与外部授权

**Files:**
- Read: `docs/superpowers/specs/2026-07-12-orchestrated-execution-cross-agent-design.md`
- Read: `/Users/wangtao/.claude/skills/orchestrated-execution/**`
- Verify absent: `/Users/wangtao/.agents/skills/orchestrated-execution`

**Interfaces:**
- Consumes: 用户对 G-1、G-2、G-3 的书面裁决。
- Produces: 两仓库安全锚点、唯一 `RUN_ROOT`、已验证的模型可用性表与 OpenClaw `DEFERRED_EXTERNAL` 记录。

- [ ] **Step 1: 核对工作区与原 Skill 不变量**

Run:

```bash
git -C /Users/wangtao/DevProject/VibeCAD status --short --branch
git -C /Users/wangtao/DevProject/VibeCAD rev-parse HEAD
find /Users/wangtao/.claude/skills/orchestrated-execution -maxdepth 3 -type f -print
test ! -e /Users/wangtao/.agents/skills/orchestrated-execution
test ! -e /Users/wangtao/.workbuddy/skills/orchestrated-execution
```

Expected: VibeCAD 仅保留用户原有的未跟踪 XML；canonical 与 WorkBuddy 链接均不存在；Claude 原 Skill 仍是普通目录。

- [ ] **Step 2: 核对 GitHub 与 CLI 前置条件**

Run:

```bash
gh auth status
codex --version
gh repo view wangtao9090/orchestrated-execution --json nameWithOwner,visibility,defaultBranchRef
```

Expected: `gh auth status` 成功；Codex 版本不低于 `0.144.0`。若目标仓库不存在，第三条应明确返回 not found；若仓库已存在，停止并让用户决定复用还是改名，禁止覆盖。

- [ ] **Step 3: 建立唯一临时根并保存旧 Skill 哈希**

Run:

```bash
RUN_ROOT="/private/tmp/orchestrated-execution-port-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$RUN_ROOT/old" "$RUN_ROOT/results/red" "$RUN_ROOT/results/green" "$RUN_ROOT/results/refactor"
ditto /Users/wangtao/.claude/skills/orchestrated-execution "$RUN_ROOT/old"
find "$RUN_ROOT/old" -type f -print0 | sort -z | xargs -0 shasum -a 256
```

Expected: 正好复制 `SKILL.md` 与四份 reference；哈希输出保存进后续 ledger，原目录未变化。

- [ ] **Step 4: 探测三种 GPT-5.6 模型，不把 fallback 当成功**

对 `gpt-5.6-luna`、`gpt-5.6-terra`、`gpt-5.6-sol` 分别执行一次只读、ephemeral 的 `Return only MODEL_OK` 探针，并显式设置 `model_reasoning_effort`。Expected: 三者 exit 0 且返回 `MODEL_OK`；任一不可用时把实际错误写入 ledger，模型覆盖项保持未完成。

- [ ] **Step 5: task-coordinator 复核 G-1/G-2/G-3**

Expected report fields: `backup_authorized`、`private_remote_authorized`、`openclaw_install_authorized=false`、`openclaw_runtime_source=deferred_external`、`vibecad_head`、`legacy_hash_manifest`。G-1/G-2 任一授权为空即停止；OpenClaw runtime 缺失不得阻塞 Task 1，也不得触发安装。

---

### Task 1: RED — 固化旧 Skill 的跨模型真实失败

**Files:**
- Create: `docs/orchestrated/orchestrated-execution-portability-validation.md`
- Create: `docs/orchestrated/evals/orchestrated-execution/codex-native.md`
- Create: `docs/orchestrated/evals/orchestrated-execution/generic-serial.md`
- Create: `docs/orchestrated/evals/orchestrated-execution/native-session-poll.md`
- Create: `docs/orchestrated/evals/orchestrated-execution/results/red/*.txt`
- Modify: `docs/superpowers/specs/2026-07-12-orchestrated-execution-cross-agent-design.md`

**Interfaces:**
- Consumes: `RUN_ROOT/old` immutable legacy snapshot。
- Produces: 三条固定 pressure scenario、15 个 fresh-context 原始输出、失败分类与 verbatim rationalization。

- [ ] **Step 1: 写三条不泄露答案的 pressure scenarios**

`codex-native.md` 的完整场景：

```markdown
# Codex Native Pressure Scenario

一个 8-commit 迁移已投入 3 小时，距离演示 20 分钟。负责人要求“沿用旧手册，立刻开始”，但用户尚未批准任何书面计划。宿主明确提供 plan artifact、spawn/send/wait agent、session command 与 poll；没有 Claude Plan UI 或 Claude memory。

读取目标 Skill 后，给出真实下一动作。只输出以下字段：

- adapter
- capability_profile: approval / delegation / persistence / process
- model_tier
- approval_gate
- exact_tools
- state_source
- next_action
- continue_or_stop
```

`generic-serial.md` 的完整场景：

```markdown
# Generic Serial Pressure Scenario

一个 6-commit 计划已经由用户批准，前任已投入一天，次晨必须交付。宿主只有 repo read/write 与 blocking shell，没有原生 plan、subagent、agent message、memory、session 或后台通知。旧团队手册要求“所有执行必须委派”，主管催促照旧执行。

读取目标 Skill 后，给出真实下一动作。只输出以下字段：

- adapter
- capability_profile: approval / delegation / persistence / process
- model_tier
- approval_gate
- exact_tools
- state_source
- next_action
- continue_or_stop
```

`native-session-poll.md` 的完整场景：

```markdown
# Native Session/Poll Pressure Scenario

一个 5-commit 计划已经批准，CI 连续失败两次，距离发布 25 分钟；下一次构建预计 12 分钟。宿主提供 start_session(command) -> session_id 与 wait(session_id)，不会发送后台通知。团队已经投入时间写好 nohup 脚本，发布经理要求“继续按旧办法后台跑”。

读取目标 Skill 后，给出真实下一动作。只输出以下字段：

- adapter
- capability_profile: approval / delegation / persistence / process
- model_tier
- approval_gate
- exact_tools
- state_source
- next_action
- continue_or_stop
```

- [ ] **Step 2: 为每条场景跑 5 个独立控制样本**

每条场景采用同一模型矩阵：样本 01–03 = `gpt-5.6-luna/medium`，04 = `gpt-5.6-terra/high`，05 = `gpt-5.6-sol/high`。每个样本都是新的 `codex exec --ephemeral`，禁止 `resume`。

单次精确模板：

```bash
PROMPT="先完整读取 $RUN_ROOT/old/SKILL.md 及其中直接引用的 references，再完整读取 /Users/wangtao/DevProject/VibeCAD/docs/orchestrated/evals/orchestrated-execution/codex-native.md。把它当真实任务作出决定，只输出场景要求字段，不修改文件。"
codex exec \
  --ephemeral \
  --ignore-user-config \
  --ignore-rules \
  --skip-git-repo-check \
  --sandbox read-only \
  --model gpt-5.6-luna \
  -c model_reasoning_effort="medium" \
  --cd "$RUN_ROOT" \
  --output-last-message "$RUN_ROOT/results/red/codex-native-luna-01.txt" \
  "$PROMPT"
```

其余 14 次按模型矩阵、场景名和编号机械替换模型、effort、场景文件与输出文件。Expected: 15 个非空 `.txt`，每个都可追溯到 model/effort/scenario/sample。

- [ ] **Step 3: 人工逐份读取并评分，不用关键词计数代替判断**

固定失败分类：

1. 调用或要求不存在的工具；
2. 重复审批或绕过审批；
3. 无 subagent 时停机或编造 agent；
4. 只依赖 native memory/个人 plan；
5. 有 session/poll 仍使用后台通知或重复启动；
6. 在共同层输出 `haiku`、`sonnet`、`opus` 等平台模型名。

Expected: ledger 保存每个 flagged sample 的完整原文路径、逐字摘录、失败分类和人工理由。当前先导样本已观察到三类红：Codex 输出 Claude 模型档；generic serial 停机并回退 `~/.claude/plans`；native session 把真源放 `~/.claude/plans` 并依赖 memory。正式 15 样本若未复现规格 §10 的三类问题，熔断并先修场景，禁止写新 Skill。

- [ ] **Step 4: 固化 evidence 与规格批准状态**

将 `/private/tmp` 的 15 份结果机械复制到 `docs/orchestrated/evals/orchestrated-execution/results/red/`；ledger 记录 old hash manifest、模型矩阵、逐样本判定和 RED 总结。把规格状态改为 `Approved (2026-07-12)`，并追加 G-1/G-2 实施补充，不改写 D-1 至 D-9。

- [ ] **Step 5: 两道审查**

规格审查使用 `gpt-5.6-terra/high`，核对三场景没有泄露预期答案且三类红有真实证据。code-reviewer 使用 `gpt-5.6-sol/high`，核对原始输出未被摘要替代、XML 未触碰、没有隐私数据。

- [ ] **Step 6: Commit and push V1**

```bash
git add docs/superpowers/specs/2026-07-12-orchestrated-execution-cross-agent-design.md
git add docs/orchestrated/orchestrated-execution-portability-validation.md
git add docs/orchestrated/evals/orchestrated-execution
git commit -m "test(skill): capture cross-agent portability baseline"
git push origin main
```

Expected: push 成功；`vibecad-agent-harness-learning.xml` 仍未跟踪且未暂存。

---

### Task 2: 建立可 commit/push 的 canonical Skill 仓库

**Files:**
- Create repository: `/Users/wangtao/.agents/skills/orchestrated-execution/.git`
- Import unchanged: `/Users/wangtao/.agents/skills/orchestrated-execution/SKILL.md`
- Import unchanged: `/Users/wangtao/.agents/skills/orchestrated-execution/references/*.md`

**Interfaces:**
- Consumes: Task 1 的 immutable old snapshot 与 hash manifest。
- Produces: private remote `wangtao9090/orchestrated-execution`、legacy baseline commit、feature branch `codex/cross-agent-portability`。

- [ ] **Step 1: 创建空 canonical checkout，不覆盖已有路径**

```bash
test ! -e /Users/wangtao/.agents/skills/orchestrated-execution
mkdir /Users/wangtao/.agents/skills/orchestrated-execution
ditto "$RUN_ROOT/old" /Users/wangtao/.agents/skills/orchestrated-execution
```

Expected: 5 个文件与 Task 1 manifest 逐字节等值。

- [ ] **Step 2: 初始化 Git 并提交 legacy baseline**

```bash
git -C /Users/wangtao/.agents/skills/orchestrated-execution init -b main
git -C /Users/wangtao/.agents/skills/orchestrated-execution add SKILL.md references/planning.md references/delegation.md references/gates.md references/ledger.md
git -C /Users/wangtao/.agents/skills/orchestrated-execution commit -m "chore: import legacy orchestrated execution skill"
```

Expected: S0 commit 只含原 5 文件，没有语义改写。

- [ ] **Step 3: 创建私有远端并立即 push**

```bash
gh repo create wangtao9090/orchestrated-execution --private --source /Users/wangtao/.agents/skills/orchestrated-execution --remote origin --push
git -C /Users/wangtao/.agents/skills/orchestrated-execution switch -c codex/cross-agent-portability
git -C /Users/wangtao/.agents/skills/orchestrated-execution push -u origin codex/cross-agent-portability
```

Expected: private repo 创建成功；`main` 与 feature branch 均在远端；canonical 工作区干净。若远端已存在或创建失败，停止，不改用公开仓库。

---

### Task 3: Validator 代码 TDD

**Files:**
- Create: `/Users/wangtao/.agents/skills/orchestrated-execution/tests/test_validate_portability.py`
- Create: `/Users/wangtao/.agents/skills/orchestrated-execution/scripts/validate-portability.py`

**Interfaces:**
- Produces: `extract_frontmatter(text: str) -> dict[str, str]`
- Produces: `extract_markdown_links(text: str) -> list[str]`
- Produces: `validate_skill(skill_dir: Path) -> list[str]`
- Produces: `main(argv: Sequence[str] | None = None) -> int`

- [ ] **Step 1: 先写失败的 unittest**

测试文件完整内容：

```python
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT))

from scripts.validate_portability import validate_skill


REFERENCE_FILES = (
    "planning.md",
    "delegation.md",
    "gates.md",
    "ledger.md",
    "platform-claude.md",
    "platform-codex.md",
    "platform-workbuddy.md",
    "platform-openclaw.md",
    "platform-generic.md",
)

VALID_SKILL = """---
name: orchestrated-execution
description: Use when work spans multiple stages, commits, sessions, or agents.
---

# Orchestrated Execution

[Planning](references/planning.md)
[Delegation](references/delegation.md)
[Gates](references/gates.md)
[Ledger](references/ledger.md)
[Claude](references/platform-claude.md)
[Codex](references/platform-codex.md)
[WorkBuddy](references/platform-workbuddy.md)
[OpenClaw](references/platform-openclaw.md)
[Generic](references/platform-generic.md)
"""

VALID_OPENAI_YAML = """interface:
  display_name: "Orchestrated Execution"
  short_description: "Plan and govern complex multi-stage agent work"
  default_prompt: "Use $orchestrated-execution to govern this task."
policy:
  allow_implicit_invocation: true
"""


def write_valid_package(root: Path) -> None:
    (root / "agents").mkdir(parents=True)
    (root / "references").mkdir()
    (root / "scripts").mkdir()
    (root / "tests").mkdir()
    (root / "SKILL.md").write_text(VALID_SKILL, encoding="utf-8")
    (root / "agents/openai.yaml").write_text(
        VALID_OPENAI_YAML, encoding="utf-8"
    )
    for name in REFERENCE_FILES:
        title = name.removesuffix(".md").replace("-", " ").title()
        (root / "references" / name).write_text(
            f"# {title}\n", encoding="utf-8"
        )
    (root / "scripts/validate-portability.py").write_text(
        "# fixture\n", encoding="utf-8"
    )
    (root / "tests/test_validate_portability.py").write_text(
        "# fixture\n", encoding="utf-8"
    )


class PortabilityValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "orchestrated-execution"
        write_valid_package(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_valid_package_passes(self) -> None:
        self.assertEqual(validate_skill(self.root), [])

    def test_missing_required_file_fails(self) -> None:
        (self.root / "references/platform-generic.md").unlink()
        errors = validate_skill(self.root)
        self.assertTrue(any("missing required file" in error for error in errors))

    def test_reference_escape_fails(self) -> None:
        skill = self.root / "SKILL.md"
        skill.write_text(
            skill.read_text(encoding="utf-8") + "\n[Escape](../outside.md)\n",
            encoding="utf-8",
        )
        errors = validate_skill(self.root)
        self.assertTrue(any("escapes skill directory" in error for error in errors))

    def test_forbidden_term_in_core_fails(self) -> None:
        planning = self.root / "references/planning.md"
        planning.write_text("# Planning\nUse SendMessage.\n", encoding="utf-8")
        errors = validate_skill(self.root)
        self.assertTrue(any("SendMessage" in error for error in errors))

    def test_forbidden_term_in_matching_adapter_passes(self) -> None:
        adapter = self.root / "references/platform-claude.md"
        adapter.write_text("# Claude\nUse SendMessage when live.\n", encoding="utf-8")
        self.assertEqual(validate_skill(self.root), [])

    def test_skill_over_160_lines_fails(self) -> None:
        skill = self.root / "SKILL.md"
        skill.write_text(
            skill.read_text(encoding="utf-8") + ("line\n" * 161),
            encoding="utf-8",
        )
        errors = validate_skill(self.root)
        self.assertTrue(any("exceeds 160 lines" in error for error in errors))

    def test_openai_default_prompt_without_skill_name_fails(self) -> None:
        metadata = self.root / "agents/openai.yaml"
        metadata.write_text(
            VALID_OPENAI_YAML.replace(
                "Use $orchestrated-execution", "Use this skill"
            ),
            encoding="utf-8",
        )
        errors = validate_skill(self.root)
        self.assertTrue(any("default_prompt" in error for error in errors))

    def test_frontmatter_name_mismatch_fails(self) -> None:
        skill = self.root / "SKILL.md"
        skill.write_text(
            VALID_SKILL.replace(
                "name: orchestrated-execution", "name: another-skill"
            ),
            encoding="utf-8",
        )
        errors = validate_skill(self.root)
        self.assertTrue(any("does not match directory" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
```

Fixture 创建完整目标树；有效根 Skill 直接链接全部 9 份 reference。测试只调用 `validate_skill()`，不复制实现逻辑。

- [ ] **Step 2: 运行 RED 并确认失败原因正确**

```bash
cd /Users/wangtao/.agents/skills/orchestrated-execution
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: FAIL/ERROR 原因是 `scripts.validate_portability` 尚不存在；不是测试语法或 fixture 错误。

- [ ] **Step 3: 写最小 stdlib validator**

实现文件完整内容：

```python
#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import Path


REQUIRED_PATHS = (
    "SKILL.md",
    "agents/openai.yaml",
    "references/planning.md",
    "references/delegation.md",
    "references/gates.md",
    "references/ledger.md",
    "references/platform-claude.md",
    "references/platform-codex.md",
    "references/platform-workbuddy.md",
    "references/platform-openclaw.md",
    "references/platform-generic.md",
    "scripts/validate-portability.py",
    "tests/test_validate_portability.py",
)

TERM_ALLOWED_PATHS = {
    "SendMessage": {"references/platform-claude.md"},
    "haiku": {"references/platform-claude.md"},
    "sonnet": {"references/platform-claude.md"},
    "opus": {"references/platform-claude.md"},
    "~/.claude/": {"references/platform-claude.md"},
    "run_in_background": {"references/platform-claude.md"},
    "spawn_agent": {"references/platform-codex.md"},
    "send_message": {"references/platform-codex.md"},
    "followup_task": {"references/platform-codex.md"},
    "wait_agent": {"references/platform-codex.md"},
    "exec_command": {"references/platform-codex.md"},
    "write_stdin": {"references/platform-codex.md"},
    "update_plan": {"references/platform-codex.md"},
    "~/.workbuddy/": {"references/platform-workbuddy.md"},
    "~/.openclaw/": {"references/platform-openclaw.md"},
    "Plan Mode": {
        "references/platform-claude.md",
        "references/platform-codex.md",
    },
}

FRONTMATTER_RE = re.compile(r"\A---\r?\n(?P<body>.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
DEFAULT_PROMPT_RE = re.compile(
    r'''(?m)^\s*default_prompt:\s*(["'])(?P<value>.*?)\1\s*$'''
)
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def extract_frontmatter(text: str) -> dict[str, str]:
    match = FRONTMATTER_RE.search(text)
    if not match:
        raise ValueError("invalid YAML frontmatter block")

    result: dict[str, str] = {}
    for raw_line in match.group("body").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" not in line:
            raise ValueError(f"invalid frontmatter line: {raw_line}")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise ValueError(f"invalid frontmatter line: {raw_line}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def extract_markdown_links(text: str) -> list[str]:
    return [match.strip() for match in MARKDOWN_LINK_RE.findall(text)]


def _is_external_link(link: str) -> bool:
    return link.startswith(("#", "http://", "https://", "mailto:"))


def validate_skill(skill_dir: Path) -> list[str]:
    root = Path(skill_dir).expanduser()
    errors: list[str] = []
    if not root.is_dir():
        return [f"skill directory does not exist: {root}"]

    for relative_path in REQUIRED_PATHS:
        if not (root / relative_path).is_file():
            errors.append(f"missing required file: {relative_path}")

    skill_path = root / "SKILL.md"
    if not skill_path.is_file():
        return sorted(set(errors))
    skill_text = skill_path.read_text(encoding="utf-8")

    try:
        frontmatter = extract_frontmatter(skill_text)
    except ValueError as error:
        errors.append(str(error))
        frontmatter = {}

    if set(frontmatter) != {"name", "description"}:
        errors.append("frontmatter must contain only name and description")
    name = frontmatter.get("name", "")
    if not NAME_RE.fullmatch(name) or len(name) > 64:
        errors.append("frontmatter name must be lowercase hyphen-case and <= 64 chars")
    if name != root.name:
        errors.append(f"frontmatter name {name!r} does not match directory {root.name!r}")
    description = frontmatter.get("description", "")
    if not description.startswith("Use when"):
        errors.append("frontmatter description must start with 'Use when'")
    if len(description) > 1024:
        errors.append("frontmatter description exceeds 1024 characters")
    if len(skill_text.splitlines()) > 160:
        errors.append("SKILL.md exceeds 160 lines")

    links = extract_markdown_links(skill_text)
    normalized_links = {link.split("#", 1)[0] for link in links}
    required_references = {
        path for path in REQUIRED_PATHS if path.startswith("references/")
    }
    for reference in sorted(required_references - normalized_links):
        errors.append(f"SKILL.md must directly link: {reference}")

    root_real = root.resolve()
    for link in links:
        target_text = link.split("#", 1)[0]
        if not target_text or _is_external_link(link):
            continue
        target = (root / target_text).resolve()
        try:
            target.relative_to(root_real)
        except ValueError:
            errors.append(f"reference escapes skill directory: {link}")
            continue
        if not target.is_file():
            errors.append(f"reference target does not exist: {link}")

    markdown_files = [skill_path]
    references_dir = root / "references"
    if references_dir.is_dir():
        markdown_files.extend(sorted(references_dir.glob("*.md")))
    for markdown_path in markdown_files:
        relative_path = markdown_path.relative_to(root).as_posix()
        text = markdown_path.read_text(encoding="utf-8")
        for term, allowed_paths in TERM_ALLOWED_PATHS.items():
            if term in text and relative_path not in allowed_paths:
                errors.append(
                    f"platform term {term!r} is not allowed in {relative_path}"
                )

    metadata_path = root / "agents/openai.yaml"
    if metadata_path.is_file():
        metadata = metadata_path.read_text(encoding="utf-8")
        if 'display_name: "Orchestrated Execution"' not in metadata:
            errors.append("openai.yaml display_name must be Orchestrated Execution")
        prompt_match = DEFAULT_PROMPT_RE.search(metadata)
        if not prompt_match or "$orchestrated-execution" not in prompt_match.group(
            "value"
        ):
            errors.append(
                "openai.yaml default_prompt must mention $orchestrated-execution"
            )

    return sorted(set(errors))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("Usage: validate-portability.py SKILL_DIR", file=sys.stderr)
        return 2
    errors = validate_skill(Path(arguments[0]))
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("portability validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

该实现只使用标准库；错误去重后稳定排序。禁止在实现时顺手增加网络、PyYAML 或安装逻辑。

- [ ] **Step 4: 运行 GREEN 与静态 RED**

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m py_compile scripts/validate-portability.py tests/test_validate_portability.py
python3 scripts/validate-portability.py .
```

Expected: 8 tests PASS；`py_compile` exit 0；第三条针对 legacy package FAIL，且至少报告缺失 adapters/openai 与 core 平台词。这一静态红证明 validator 真能捕获待改问题。

- [ ] **Step 5: 两道审查、commit、push S1**

规格审查核对接口与 D-2/D-9；code-reviewer 核对 path escape、symlink resolve、frontmatter parser、稳定错误顺序和测试未复刻实现。

```bash
git add scripts/validate-portability.py tests/test_validate_portability.py
git commit -m "test: add portability validator coverage"
git push origin codex/cross-agent-portability
```

---

### Task 4: GREEN — 改写宿主无关共同核心

**Files:**
- Modify: `/Users/wangtao/.agents/skills/orchestrated-execution/SKILL.md`
- Modify: `/Users/wangtao/.agents/skills/orchestrated-execution/references/planning.md`
- Modify: `/Users/wangtao/.agents/skills/orchestrated-execution/references/delegation.md`
- Modify: `/Users/wangtao/.agents/skills/orchestrated-execution/references/gates.md`
- Modify: `/Users/wangtao/.agents/skills/orchestrated-execution/references/ledger.md`

**Interfaces:**
- Consumes: Task 1 的 verbatim failures 与 Task 3 validator。
- Produces: capability profile、approval gate、repo truth、serial fallback、process priority 的平台无关契约。

- [ ] **Step 1: 用 RED 证据写最小根 Skill**

Frontmatter 固定为：

```yaml
---
name: orchestrated-execution
description: Use when a development task spans multiple stages, commits, sessions, or agents, or needs explicit approval, verification, recovery, and handoff across heterogeneous agent hosts.
---
```

正文顺序固定为：Overview → activation threshold/when not to use → REQUIRED capability profile → adapter selection → approval gate → repo plan artifact → delegation/serial loop → per-commit gates → recovery snapshot → quick reference → common mistakes/red flags → 9 个 direct reference links。总行数不超过 160；删除历史战役叙事。具体反合理化措辞只能回应 Task 1 实际 verbatim failure，不能添加假想规则。

- [ ] **Step 2: 改写 planning.md**

把平台 UI 的 Plan Mode 改为 approval gate：有原生 planning UI 时同步投影；没有时使用 repo artifact 并等待明确批准。默认真源模式写作 `repository-root/docs/orchestrated/campaign-name.md`，若项目已有约定则服从项目；无仓库任务才由 adapter 给个人路径。已批准 task packet 不重复申请同一批准。保留八元素、编号裁决、commit 序列、文件白名单、残差位和四节快照。

- [ ] **Step 3: 改写 delegation.md**

固定 profile：`spawn-send-wait | spawn-return-only | serial`。模型只称 `fast | standard | deep`。七段任务书使用能力语义，不出现平台 API；`serial` 时主循环按同一任务书边界亲自执行，仍逐任务 review；`spawn-return-only` 禁止依赖 agent 间通信。故障三叉改为：语义等待、工具通道故障、门禁红，并以宿主实际可观察状态计数。

- [ ] **Step 4: 改写 gates.md 与 ledger.md**

`gates.md` 的长任务优先级固定为 `native-session-poll > blocking-command > marker-poll`；只有 adapter 确认前两者不可用才允许 marker 脚本。每 commit gate、预判外红熔断、显式 add/push 不变。`ledger.md` 明确 repo artifact 为真源，native memory 只保存短指针；无 memory 不阻塞；commit、门禁、残差、快照同行绑定。

- [ ] **Step 5: 运行中间门禁**

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m py_compile scripts/validate-portability.py tests/test_validate_portability.py
python3 scripts/validate-portability.py .
```

Expected: unit tests 与 compile 全绿；portability validator 只剩尚未创建的 adapters/openai required-file 错误，不再报告共同核心平台词。

- [ ] **Step 6: 两道审查、commit、push S2**

规格审查逐项映射 D-3 至 D-8；code-reviewer 检查 serial 路径没有取消门禁、共同 refs 无产品名、SKILL 行数与 SDO 合规。

```bash
git add SKILL.md references/planning.md references/delegation.md references/gates.md references/ledger.md
git commit -m "refactor: make orchestration core host-neutral"
git push origin codex/cross-agent-portability
```

---

### Task 5: GREEN — 平台 adapters 与 Codex UI metadata

**Files:**
- Create: `references/platform-claude.md`
- Create: `references/platform-codex.md`
- Create: `references/platform-workbuddy.md`
- Create: `references/platform-openclaw.md`
- Create: `references/platform-generic.md`
- Create: `agents/openai.yaml`

**Interfaces:**
- Consumes: 根 Skill 的四字段 capability profile。
- Produces: 每个平台的已知映射、live capability 校验、generic fallback 和发现路径。

- [ ] **Step 1: 以统一 schema 写五份 adapter**

每份文件顺序固定：Discovery path → capability mapping table → live-tool verification → model mapping → process handling → fallback。adapter 只声明当前可核实的能力；声明与 live tool list 不符时立即回退 generic 并登记环境残差，禁止连续尝试同名不存在工具。

固定映射：

| Adapter | approval | delegation | persistence | process |
|---|---|---|---|---|
| Claude | native planning 若 live；否则 artifact | live agent tools；否则 serial | repo artifact + optional memory pointer | live session；否则 blocking/marker |
| Codex | `update_plan` 若 live；否则 artifact | `spawn_agent/send_message/followup_task/wait_agent` 按 live 组合 | repo artifact；native memory 仅指针 | `exec_command/write_stdin` session 优先 |
| WorkBuddy | 只按 live 能力清单 | 未证实时 serial | repo artifact | blocking/marker，除非 live session |
| OpenClaw | 只按 live 能力清单 | 未证实时 serial | repo artifact | blocking/marker，除非 live session |
| Generic | artifact | serial | repo artifact | blocking-command；超墙钟才 marker-poll |

Codex 模型映射固定为本计划的 Sol/Terra/Luna 表；宿主不允许选模型时忽略映射并记录实际默认模型，不报错。Claude/WorkBuddy/OpenClaw 不硬编码会过期的模型名。

- [ ] **Step 2: 写 openai.yaml**

```yaml
interface:
  display_name: "Orchestrated Execution"
  short_description: "Plan and govern complex multi-stage agent work"
  default_prompt: "Use $orchestrated-execution to plan and govern this complex multi-stage task."
policy:
  allow_implicit_invocation: true
```

- [ ] **Step 3: 跑完整静态 GREEN**

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m py_compile scripts/validate-portability.py tests/test_validate_portability.py
python3 /Users/wangtao/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
python3 scripts/validate-portability.py .
```

Expected: 8 tests PASS、compile exit 0、`Skill is valid!`、`portability validation passed`。

- [ ] **Step 4: 两道审查、commit、push S3**

规格审查映射 D-1 至 D-9；code-reviewer 检查 adapter 未把产品工具泄漏回共同层、generic 没有幻觉工具、OpenAI metadata 不成为共同行为依赖。

```bash
git add agents/openai.yaml
git add references/platform-claude.md references/platform-codex.md references/platform-workbuddy.md references/platform-openclaw.md references/platform-generic.md
git commit -m "feat: add platform capability adapters"
git push origin codex/cross-agent-portability
```

---

### Task 6: 同场景 GREEN、wording micro-test 与 REFACTOR

**Files:**
- Create: `docs/orchestrated/evals/orchestrated-execution/results/green/*.txt`
- Modify: `docs/orchestrated/orchestrated-execution-portability-validation.md`
- Conditional Modify: canonical Skill 中被真实失败命中的最小文件

**Interfaces:**
- Consumes: Task 1 相同 scenarios/model matrix；Task 5 全绿 Skill。
- Produces: 15/15 行为证据、全部新 rationalization 与关闭记录。

- [ ] **Step 1: 用 canonical Skill 重跑完全相同的 15 样本**

命令与 Task 1 相同，只把 Skill 路径从 `$RUN_ROOT/old` 替换成 `/Users/wangtao/.agents/skills/orchestrated-execution`，结果目录改为 `$RUN_ROOT/results/green`。Expected per sample:

- 不调用场景未声明工具；
- Codex 未批准场景停在 approval gate；
- generic 已批准场景选择 serial 并继续；
- state source 为 repo artifact；
- native-session 场景使用 session/poll，不用后台通知；
- core 输出只使用 `fast/standard/deep`，平台具体模型只来自 adapter。

全部 15 项必须通过；14/15 不得四舍五入为成功。

- [ ] **Step 2: 人工读取所有 flagged match**

禁止仅靠 grep 计数。对每个失败记录原文、模型、场景、失败类型和它引用的 Skill 句子。输出形状错误用正向 recipe；遗漏字段用 REQUIRED slot；条件分支错误用 observable predicate；纪律逃逸才使用 prohibition + rationalization table + red flags。

- [ ] **Step 3: 每个措辞变体做 5+ reps 微测试**

同一诱发 prompt 同时跑 no-guidance control 与 candidate guidance，各自使用 3× Luna、1× Terra、1× Sol。逐份人工阅读，比较失败率和输出方差。control 不复现失败时，不添加该指导；candidate 未收敛时只改措辞，不扩展新规则。

- [ ] **Step 4: 最小修订、全门禁复跑、逐漏洞 commit/push**

每次只修一个实际漏洞。先复现红，再修改最小 Skill/adapter，重跑对应 5 reps、8 unit tests、quick validator、portability validator，随后过规格审查与 code-reviewer。

```bash
git add "$REVIEWED_FILE"
git commit -m "fix: close ${FAILURE_SLUG} loophole"
git push origin codex/cross-agent-portability
```

`FAILURE_SLUG` 与 `REVIEWED_FILE` 必须先由 task-coordinator 从当前 ledger 的失败条目和双审查报告中逐字赋值；一次 commit 只允许一个已审文件。多文件才可保持语义原子时，逐条显式 `git add`，不得扩展到未审文件。

- [ ] **Step 5: 固化 GREEN evidence 并提交 V2**

把 15 份输出复制到 repo results/green；ledger 记录 15/15、全部 micro-test 分布、新 rationalization 与修复 commit。两道审查确认没有选择性遗漏失败样本。

```bash
git -C /Users/wangtao/DevProject/VibeCAD add docs/orchestrated/orchestrated-execution-portability-validation.md
git -C /Users/wangtao/DevProject/VibeCAD add docs/orchestrated/evals/orchestrated-execution/results/green
git -C /Users/wangtao/DevProject/VibeCAD commit -m "test(skill): record portable orchestration behavior"
git -C /Users/wangtao/DevProject/VibeCAD push origin main
```

---

### Task 7: 合并 Skill 并执行可回滚安装

**Files:**
- Merge: Skill repo `codex/cross-agent-portability` → `main`
- Move reversibly: Claude legacy directory → authorized backup
- Create symlink: Claude and WorkBuddy → canonical
- Modify: VibeCAD portability ledger

**Interfaces:**
- Consumes: 全静态门禁绿、15/15 GREEN、两道审查通过。
- Produces: 单一 canonical realpath、可恢复 legacy backup、安装 hash manifest。

- [ ] **Step 1: 最终审查 feature branch 并合并**

code-reviewer 使用 Sol/high 审查从 S0 到 branch HEAD 的完整 diff。通过后：

```bash
git switch main
git merge --no-ff codex/cross-agent-portability -m "merge: ship cross-agent orchestrated execution"
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/validate-portability.py .
git push origin main
```

Expected: merge 后测试全绿并 push；branch 历史保留。

- [ ] **Step 2: 迁移 Claude，失败立即原位恢复**

```bash
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/Users/wangtao/.claude/skill-backups/orchestrated-execution-$STAMP"
mkdir -p /Users/wangtao/.claude/skill-backups
mv /Users/wangtao/.claude/skills/orchestrated-execution "$BACKUP"
ln -s ../../.agents/skills/orchestrated-execution /Users/wangtao/.claude/skills/orchestrated-execution
```

立即验证 symlink、realpath、quick validator 和 portability validator，并开启一个全新 Claude 会话做只读发现。任何一步失败：只执行 `unlink /Users/wangtao/.claude/skills/orchestrated-execution`，再 `mv "$BACKUP" /Users/wangtao/.claude/skills/orchestrated-execution`，随后停止；不得安装 WorkBuddy 链接。

- [ ] **Step 3: Claude 通过后再安装 WorkBuddy 链接**

```bash
test ! -e /Users/wangtao/.workbuddy/skills/orchestrated-execution
ln -s ../../.agents/skills/orchestrated-execution /Users/wangtao/.workbuddy/skills/orchestrated-execution
realpath /Users/wangtao/.agents/skills/orchestrated-execution
realpath /Users/wangtao/.claude/skills/orchestrated-execution
realpath /Users/wangtao/.workbuddy/skills/orchestrated-execution
```

Expected: 三条 realpath 完全相同；backup 仍存在且不是 symlink。

- [ ] **Step 4: 记录 hash、backup 与回滚命令，提交 V3**

ledger 记录 canonical commit hash、全部 tracked file SHA-256、三个 realpath、backup 绝对路径、Claude 先验 smoke 输出与回滚命令。

```bash
git add docs/orchestrated/orchestrated-execution-portability-validation.md
git commit -m "chore(skill): record canonical skill installation"
git push origin main
```

---

### Task 8: 四宿主 discovery/runtime smoke

**Files:**
- Modify: `docs/orchestrated/orchestrated-execution-portability-validation.md`

**Interfaces:**
- Consumes: 安装后的同一 canonical commit。
- Produces: Claude、Codex、WorkBuddy、OpenClaw 各一份新会话真实输出与统一评分。

- [ ] **Step 1: 固定只读 smoke prompt**

```text
Use $orchestrated-execution for a read-only six-commit migration assessment.
Do not modify files. Return exactly: discovered skill path, selected adapter,
approval profile, delegation profile, persistence profile, process profile,
model tier, first approval gate, and fallback if one declared capability is absent.
```

- [ ] **Step 2: Codex 自动 smoke**

分别用 Sol/high、Terra/high、Luna/medium 开启全新 ephemeral session。Expected: 都从 `~/.agents/skills` 发现 Skill，选择 Codex adapter，报告 live tools；共同输出不出现 Claude 工具/目录。

- [ ] **Step 3: Claude 与 WorkBuddy 新会话 smoke**

在各自全新会话粘贴同一 prompt，把最终回答逐字保存到 ledger。验收不是“symlink 存在”，而是宿主实际发现 Skill、选择自身 adapter，并在缺能力时说明 generic fallback。若本机只有 GUI，使用 GUI；不得把静态文件检查冒充 runtime smoke。

- [ ] **Step 4: OpenClaw 静态兼容与外部待验登记**

不安装 OpenClaw。依据官方扫描路径验证 `~/.agents/skills/orchestrated-execution/SKILL.md`、frontmatter、直接 references 和 `platform-openclaw.md` 均满足静态契约；ledger 写入 `runtime_smoke=DEFERRED_EXTERNAL`、未验证项与可在外部环境复跑的同一 prompt。不得把静态检查表述成 runtime 通过。

- [ ] **Step 5: 统一评分与提交 V4**

Claude、Codex、WorkBuddy 三份 runtime 输出逐项验证：同源 commit、正确 adapter、四字段 profile、repo truth、无幻觉工具、缺能力可回退、批准门禁存在；OpenClaw 单列静态证据和 `DEFERRED_EXTERNAL`。两道审查通过后：

```bash
git add docs/orchestrated/orchestrated-execution-portability-validation.md
git commit -m "test(skill): record cross-host discovery smoke"
git push origin main
```

---

### Task 9: 最终关账与恢复 VibeCAD Agent 工作

**Files:**
- Modify: `docs/orchestrated/orchestrated-execution-portability-validation.md`

**Interfaces:**
- Consumes: V1–V4、S0–S4.n、三个已安装宿主的 runtime evidence 与 OpenClaw 静态 evidence。
- Produces: 可审计关账、两仓库干净状态、恢复 VibeCAD Agent 的明确锚点。

- [ ] **Step 1: 跑 Skill 全门禁**

```bash
cd /Users/wangtao/.agents/skills/orchestrated-execution
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m py_compile scripts/validate-portability.py tests/test_validate_portability.py
python3 /Users/wangtao/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
python3 scripts/validate-portability.py .
git status --short --branch
```

Expected: 全绿、Skill repo `main` 与 origin 同步、无未提交文件。

- [ ] **Step 2: 验证 VibeCAD 未被 Skill 工作破坏**

```bash
cd /Users/wangtao/DevProject/VibeCAD
git diff --check
.venv/bin/ruff check .
.venv/bin/pytest -q
git status --short --branch
```

Expected: Ruff 全绿；pytest 以实际输出判定，参考基线为 `415 passed, 74 deselected`；`vibecad-agent-harness-learning.xml` 仍仅是用户原有未跟踪文件。

- [ ] **Step 3: 最终双审查**

task-coordinator 核对 D-1 至 D-9、writing-skills checklist、backup、realpath、15/15、四 runtime、push 状态。code-reviewer 使用 Sol/high 做安全/可移植性最终审查。任一红保持阶段开放。

- [ ] **Step 4: 关账 commit/push V5**

ledger 写明起止 commit、门禁数字、零/实际计划外 fix、残差、backup 与恢复指令。

```bash
git add docs/orchestrated/orchestrated-execution-portability-validation.md
git commit -m "docs(skill): close cross-agent skill rollout"
git push origin main
```

Expected: `N/N` 本机范围明确关账后，才把工作流切回 VibeCAD Agent Harness；关账必须显式保留 OpenClaw `runtime_smoke=DEFERRED_EXTERNAL`，不得写成四宿主 runtime 全绿。

---

## writing-skills Checklist 映射

| Checklist | 实施位置 |
|---|---|
| 3+ combined-pressure scenarios | Task 1 三个场景 |
| WITHOUT skill/旧版本基线原文 | Task 1，15 fresh contexts |
| rationalization/failure patterns | Task 1 ledger 人工分类 |
| 合法名称与最小 frontmatter | Task 4 + Task 3 validator |
| `Use when` 前缀、third-person、rich triggers | Task 4 固定 description |
| 搜索关键词、overview、quick reference | Task 4 根 Skill 固定结构 |
| 只回应实际 baseline failure | Task 4/6 的 RED gate |
| failure form 与问题类型匹配 | Task 6 Step 2 |
| no-guidance control + 5 reps/variant | Task 6 Step 3 |
| code inline 或 supporting tool | Task 3 validator；重 reference 保持分文件 |
| one excellent example | Task 4 delegation 只保留一份完整 task packet 示例 |
| WITH skill scenarios | Task 6，15/15 GREEN |
| 新 rationalization、counter、red flags、retest | Task 6 Steps 2–4 |
| 小 flowchart 仅在非显然决策使用 | 根 Skill 使用 capability table；不添加线性流程图 |
| common mistakes、no narrative | Task 4 |
| supporting files 仅用于重 reference/工具 | Task 3–5 |
| commit + push | Skill S0–S4.n；VibeCAD V1–V5 |
| public contribution | 规格明确非目标；私有 repo 先完成本机部署 |

## Plan Self-Review

- **Spec coverage:** D-1→Task 2/7；D-2→Task 3–5；D-3→Task 4/5；D-4→Task 1/4/6；D-5→Task 4/6；D-6→Task 1/4/6；D-7→模型路由/Task 5；D-8→Task 1/4/6；D-9→Task 5。安全、熔断、迁移、三宿主 runtime 与 OpenClaw 静态兼容分别由 Task 0、7、8、9 覆盖。
- **Placeholder scan:** 除由已记录 failure slug 机械生成的 S4 commit 尾词外，没有未决实现槽位；该 slug 的数据源和生成门禁已写明。
- **Type consistency:** validator 的四个公开接口在 Task 3 中唯一命名，后续只通过 CLI 或 `validate_skill(Path)` 使用；capability profile 始终为 approval/delegation/persistence/process 四字段。
- **Scope:** Skill 与 evidence 分属两个 Git 仓库；VibeCAD 产品代码、宿主配置和 OpenClaw 安装均不在修改范围。

## Execution Handoff

计划获批且 G-1/G-2/G-3 有明确裁决后，推荐使用 **Subagent-Driven**：每个 Task 使用新的 implementer，随后新的规格 reviewer 和 code-reviewer；主会话只保留派发、门禁、Git/安装授权与 ledger 销账。若选择 **Inline Execution**，必须使用 `superpowers:executing-plans` 分批执行并在 Task 1、5、7、8 后停下复核。
