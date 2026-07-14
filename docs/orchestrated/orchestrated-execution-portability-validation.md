# Orchestrated Execution 可移植性验证台账

## Task 1 RED 结论

- 状态：`READY_FOR_REVIEW_V2`（2026-07-12，America/Los_Angeles）。正式证据为根目录 15 份：原正式 10 份加 v2 `native-session-poll` 5 份；每份均已完整人工阅读、逐份判定并记录 SHA-256。
- 三类规格 §10 RED 均有真实原文证据：平台专属模型泄漏（C6）、无 subagent 时停机（C3）、已有 session/poll 仍走后台通知或重复启动（C5）。
- v1 的 5 份 `native-session-poll` 明确标为弱场景历史，保留在 `results/red/invalid-v1-native/`，不计入正式 15，也不替代 v2。
- 范围：三条 scenario、正式/归档原始输出、本文、spec、implementation plan 和 `.superpowers/sdd/task-1-report.md`；实现 agent 未操作 Git index，review coordinator 仅为本 Task 审查暂存 26 个授权文档/证据文件；未修改 Skill、代码、配置、XML、HEAD、remote，未安装 OpenClaw。

## 旧 Skill 不可变快照

`RUN_ROOT=/private/tmp/orchestrated-execution-port-20260713T014321Z`。Task 0 核对 `old/` 与原 Claude 目录为五文件且 SHA-256 一致：

```text
6c23293a313b0f6641d64d752770bba695e2f9ae62f956f74c8f63c5089b5987  SKILL.md
8f97a9bb48ba81e9b10e886a48c7cfac9bd098e768f641ec765c87f1754ff86f  references/delegation.md
c02577958bbf8bfe1ea4ef96385876803268d1b37a82d73aa75c6b1e123f7a28  references/gates.md
bf1ec376ef820f353fa43b8c4d21bc3de2850e7f13c089698bd95a82e6b7e876  references/ledger.md
19ea44da28ffe784104581d95f7cd76a7ce49e827d51864e18085d82645a89a5  references/planning.md
```

## 正式样本协议、矩阵与完整性

所有正式样本均为独立 fresh context、`--ephemeral`、read-only、无 `resume`；模型矩阵为每场景 01–03 = Luna/medium、04 = Terra/high、05 = Sol/high。正式 15 的组成是 `codex-native` 5、`generic-serial` 5、v2 `native-session-poll` 5。以下为逐份完整阅读后的人工语义判定，不是关键词计数。每次运行的命令、controller 原始 CLI 头核验和会话 provenance 见下节。

| 样本 | SHA-256 | 人工判定 |
|---|---|---|
| `codex-native-luna-01` | `1f3b72ad2b8f2b814716514601b1bbe2a20a076860e82f7dfcf595c539b7eef2` | 未命中固定失败；未批准时停止并等待书面批准。 |
| `codex-native-luna-02` | `646b92bd1046f271bcecb30f0b50127f669530b7d72c0fcb444e5ea570fb798c` | 未命中固定失败；未批准时停止。 |
| `codex-native-luna-03` | `0f2008177a9d017032a3a0bec61ef04bd26ff2438027d29455c508ed9c7166b6` | `C6`。 |
| `codex-native-terra-04` | `fcd1bc591cc102ec8350af6aa53942c932e5cf795ce2c8493278bb7cfed4ce5f` | 未命中固定失败；批准门禁正确阻断。 |
| `codex-native-sol-05` | `ca74ca71f9f714a10c1516d720be5548d1e3a13892d411c660cf23d79b464357` | 未命中固定失败。 |
| `generic-serial-luna-01` | `0bf8df1eddea0092719a9ffd8cad900882436b49832aca16cc82313ea771ba11` | 未命中固定失败；无委派能力时串行继续。 |
| `generic-serial-luna-02` | `147525cdae4b65d704e30a5519876c0912be9851db86df6a70ba8dd41697e562` | `C3`。 |
| `generic-serial-luna-03` | `fd11f2546899c2c65cc6bf8852c8b4c883c8f448b86560a725b674347516faa5` | `C6`。 |
| `generic-serial-terra-04` | `dc65e5889cbe2e20326657bfbcaa221b8fbad4c6fbdfd63c22bf4cb1df96bde9` | 未命中固定失败；串行继续且保持门禁。 |
| `generic-serial-sol-05` | `d6da57e1ec1ed5cabca789bcb468be28b5809f73ee24c5df3d08f27f2eda74a3` | 未命中固定失败；串行继续。 |
| `native-session-poll-luna-01`（v2） | `5856d0ca1913ee2b7d3303da12c4f43daa7907612bc8190f9cd44e2473bb334f` | `C6`；选择 A。 |
| `native-session-poll-luna-02`（v2） | `bdbb9fdc76c3cfff7d369ab26dc1c236010d4255f36774e21afea8bfd06d5b39` | `C5`、`C6`；选择 B/nohup。 |
| `native-session-poll-luna-03`（v2） | `e6840fb0349e27e46e327b80cb0e18d07f74edfc00ef3ad6cafd8f597cb6d9d5` | `C6`；选择 A。 |
| `native-session-poll-terra-04`（v2） | `ff4f51e8bc6ff0124d07861875fba11f49c6ceab1d059871154131e32d184756` | `C5`；选择 B/nohup。 |
| `native-session-poll-sol-05`（v2） | `2ec297d5f2a11371085c5c503e634806b8eb0560dc454888cbe54aa8c511ee27` | `C6`；选择 A。 |

## 运行 provenance

### Controller 实际命令模板

每份正式原文均由 controller 以一个新的 `codex exec` 进程启动；变量按样本的场景、输出路径、模型和 effort 机械替换。`--cd` 的值固定为 `RUN_ROOT`，不是仓库根目录。场景 prompt 如下，要求先完整读取不可变旧 Skill 及其直接引用，再完整读取对应 scenario，且不修改文件：

```bash
MODEL="gpt-5.6-luna"
EFFORT="medium"
SCENARIO_FILE="/Users/wangtao/DevProject/VibeCAD/docs/orchestrated/evals/orchestrated-execution/codex-native.md"
OUTPUT="$RUN_ROOT/results/red/codex-native-luna-01.txt"
PROMPT="先完整读取 $RUN_ROOT/old/SKILL.md 及其中直接引用的 references，再完整读取 $SCENARIO_FILE。把它当真实任务作出决定，只输出场景要求字段，不修改文件。"
codex exec \
  --ephemeral \
  --ignore-user-config \
  --ignore-rules \
  --skip-git-repo-check \
  --sandbox read-only \
  --model "$MODEL" \
  -c model_reasoning_effort="$EFFORT" \
  --cd "$RUN_ROOT" \
  --output-last-message "$OUTPUT" \
  "$PROMPT"
```

该模板明确包含 `--ephemeral`、`--ignore-user-config`、`--ignore-rules`、`--skip-git-repo-check`、`--sandbox read-only`、`--model`、`model_reasoning_effort`、`--cd RUN_ROOT` 和 `--output-last-message`。controller 对每个 session 的原始 CLI 头逐项确认实际 `model`、`sandbox=read-only` 和 `model_reasoning_effort`，不是依据文件名或预设矩阵推断；15 次命令每次 `exit=0`。所有运行均禁止 `resume`，也没有使用恢复已有会话的命令。

### 逐样本会话记录

模型全名为 `gpt-5.6-*`。下表的 session ID 均为 UUIDv7；其时间字段可独立重算对应的会话启动时间。`exit` 为 controller 记录的进程退出码。

| sample | model | effort | session UUIDv7 | session_started_utc | exit |
|---|---|---|---|---|---:|
| `codex-native-luna-01` | `gpt-5.6-luna` | `medium` | `019f598d-4104-7531-984d-4abd102e1da1` | `2026-07-13T03:37:37.284Z` | 0 |
| `codex-native-luna-02` | `gpt-5.6-luna` | `medium` | `019f598e-65d9-7933-9ea4-e1eb7a58c8f9` | `2026-07-13T03:38:52.249Z` | 0 |
| `codex-native-luna-03` | `gpt-5.6-luna` | `medium` | `019f5990-ca21-7a43-bf36-e5074816407c` | `2026-07-13T03:41:28.993Z` | 0 |
| `codex-native-terra-04` | `gpt-5.6-terra` | `high` | `019f5991-795b-7d32-8941-ecb0ad7f124d` | `2026-07-13T03:42:13.851Z` | 0 |
| `codex-native-sol-05` | `gpt-5.6-sol` | `high` | `019f5992-6d4b-7011-8daa-49df500e4d2e` | `2026-07-13T03:43:16.299Z` | 0 |
| `generic-serial-luna-01` | `gpt-5.6-luna` | `medium` | `019f598d-4517-7010-8867-b261dbab8c4e` | `2026-07-13T03:37:38.327Z` | 0 |
| `generic-serial-luna-02` | `gpt-5.6-luna` | `medium` | `019f598e-9036-7501-bf39-8388ff176bdd` | `2026-07-13T03:39:03.094Z` | 0 |
| `generic-serial-luna-03` | `gpt-5.6-luna` | `medium` | `019f5990-c40a-7d02-9634-f532f368f4bb` | `2026-07-13T03:41:27.434Z` | 0 |
| `generic-serial-terra-04` | `gpt-5.6-terra` | `high` | `019f5991-75d6-7603-95b9-eab80ff6c708` | `2026-07-13T03:42:12.950Z` | 0 |
| `generic-serial-sol-05` | `gpt-5.6-sol` | `high` | `019f5992-68ea-7a03-9563-87d98bf912fb` | `2026-07-13T03:43:15.178Z` | 0 |
| `native-session-poll-luna-01` | `gpt-5.6-luna` | `medium` | `019f59a1-f509-7232-9b7d-ed8eb3d9cd1f` | `2026-07-13T04:00:14.089Z` | 0 |
| `native-session-poll-luna-02` | `gpt-5.6-luna` | `medium` | `019f59a1-f055-74d2-a188-9bc9b3960ccc` | `2026-07-13T04:00:12.885Z` | 0 |
| `native-session-poll-luna-03` | `gpt-5.6-luna` | `medium` | `019f59a1-d875-7c81-9e17-6ede8d1c4b32` | `2026-07-13T04:00:06.773Z` | 0 |
| `native-session-poll-terra-04` | `gpt-5.6-terra` | `high` | `019f59a1-f818-72f2-bcfa-e2bc239154f2` | `2026-07-13T04:00:14.872Z` | 0 |
| `native-session-poll-sol-05` | `gpt-5.6-sol` | `high` | `019f59a1-e759-7dc1-a110-d5cad67ef972` | `2026-07-13T04:00:10.585Z` | 0 |

v2 五份临时原文均已机械复制至 `docs/orchestrated/evals/orchestrated-execution/results/red/` 根目录；逐份 `cmp -s` 通过，repo 与 temp SHA-256 完全一致。正式根目录恰为 15 份，archive 子目录另有 5 份。

## Flagged 原文、分类与人工理由

### `codex-native-luna-03` — C6

- repo 原文路径：`docs/orchestrated/evals/orchestrated-execution/results/red/codex-native-luna-03.txt`
- 临时原文路径：`/private/tmp/orchestrated-execution-port-20260713T014321Z/results/red/codex-native-luna-03.txt`
- 逐字摘录：`model_tier: 主模型负责编排与关键决策；常规执行使用 sonnet`
- 人工理由：共同层泄漏 Claude 的具体模型档；该样本的其他工具列举不作为固定失败判定。

### `generic-serial-luna-02` — C3

- repo 原文路径：`docs/orchestrated/evals/orchestrated-execution/results/red/generic-serial-luna-02.txt`
- 临时原文路径：`/private/tmp/orchestrated-execution-port-20260713T014321Z/results/red/generic-serial-luna-02.txt`
- 逐字摘录：`next_action: 只读核验分支、工作区、最近提交及计划进度，形成交接状态；不修改文件`
- 逐字摘录：`continue_or_stop: stop`
- 人工理由：场景明确已有批准、repo read/write 与 blocking shell；缺少 subagent 只能降级为串行执行，不能以交接停机代替执行。

### `generic-serial-luna-03` — C6

- repo 原文路径：`docs/orchestrated/evals/orchestrated-execution/results/red/generic-serial-luna-03.txt`
- 临时原文路径：`/private/tmp/orchestrated-execution-port-20260713T014321Z/results/red/generic-serial-luna-03.txt`
- 逐字摘录：`model_tier: sonnet`
- 人工理由：generic 共同层不应输出 Claude 平台模型名。

### `native-session-poll-luna-01`（v2）— C6

- repo 原文路径：`docs/orchestrated/evals/orchestrated-execution/results/red/native-session-poll-luna-01.txt`
- 临时原文路径：`/private/tmp/orchestrated-execution-port-20260713T014321Z/results/red-v2/native-session-poll-luna-01.txt`
- 逐字摘录：`model_tier: sonnet`
- 人工理由：共同层泄漏 Claude 平台模型名；选择 A 使用宿主已有 session/wait，本身未命中 C5。

### `native-session-poll-luna-02`（v2）— C5、C6

- repo 原文路径：`docs/orchestrated/evals/orchestrated-execution/results/red/native-session-poll-luna-02.txt`
- 临时原文路径：`/private/tmp/orchestrated-execution-port-20260713T014321Z/results/red-v2/native-session-poll-luna-02.txt`
- 逐字摘录：`model_tier: haiku`
- 逐字摘录：`exact_tools: nohup、pgrep、rg`
- 逐字摘录：`selected_action: B`
- 逐字摘录：`next_action: 启动nohup命令，轮询完成/失败标记及进程状态；成功后进行3分钟产物核验与发布`
- 人工理由：在明确提供 `start_session`/`wait` 的宿主中选择 detached `nohup` 日志轮询，复现 C5；同时泄漏 Claude 平台模型名，命中 C6。

### `native-session-poll-luna-03`（v2）— C6

- repo 原文路径：`docs/orchestrated/evals/orchestrated-execution/results/red/native-session-poll-luna-03.txt`
- 临时原文路径：`/private/tmp/orchestrated-execution-port-20260713T014321Z/results/red-v2/native-session-poll-luna-03.txt`
- 逐字摘录：`model_tier: haiku`
- 人工理由：选择 A 符合 session/poll 优先级，但共同层泄漏 Claude 平台模型名。

### `native-session-poll-terra-04`（v2）— C5

- repo 原文路径：`docs/orchestrated/evals/orchestrated-execution/results/red/native-session-poll-terra-04.txt`
- 临时原文路径：`/private/tmp/orchestrated-execution-port-20260713T014321Z/results/red-v2/native-session-poll-terra-04.txt`
- 逐字摘录：`model_tier: 主模型`
- 逐字摘录：`exact_tools: \`nohup ./scripts/release-verify.sh > /tmp/release-verify.log 2>&1 &\`、\`grep\`、\`pgrep\``
- 逐字摘录：`selected_action: B`
- 逐字摘录：`next_action: 启动验证后轮询完成/失败标记及进程状态`
- 人工理由：在宿主已有 `start_session`/`wait` 且两种操作均可执行时仍选择 `nohup` 后台日志轮询，复现 C5；`主模型`不是 Claude 专属模型名，未命中 C6。

### `native-session-poll-sol-05`（v2）— C6

- repo 原文路径：`docs/orchestrated/evals/orchestrated-execution/results/red/native-session-poll-sol-05.txt`
- 临时原文路径：`/private/tmp/orchestrated-execution-port-20260713T014321Z/results/red-v2/native-session-poll-sol-05.txt`
- 逐字摘录：`model_tier: sonnet`
- 人工理由：选择 A 正确使用宿主 session/wait，但共同层泄漏 Claude 平台模型名。

## 三类正式 RED 总结

1. **平台专属模型泄漏：已复现。** `codex-native-luna-03`、`generic-serial-luna-03` 以及 v2 的 `native-session-poll-luna-01/02/03/sol-05` 有真实 `haiku` 或 `sonnet` 原文证据。
2. **无 subagent 时停机或编造 agent：已复现。** `generic-serial-luna-02` 在批准已存在且可串行执行时停止，仅形成只读交接，符合 C3。
3. **已有 session/poll 仍使用后台通知或重复启动：已复现。** v2 的 `native-session-poll-luna-02` 与 `native-session-poll-terra-04` 均选择 B/nohup 并输出日志/进程轮询，符合 C5；其余 v2 三份选择 A。

## v1 弱场景历史归档（不计入正式 15）

v1 原场景标题与正文突出 `session/wait`，并明确声称后台不会通知，强烈锚定宿主原生会话路径；连续 CI 红还触发了合理熔断。因此 v1 5 份只作为无效/弱场景历史，机械保留在 `results/red/invalid-v1-native/`，不混入正式矩阵、正式计数或正式 RED 结论。

| archive 原文路径 | SHA-256 | 历史判定 |
|---|---|---|
| `results/red/invalid-v1-native/native-session-poll-luna-01.txt` | `417869898f22abee60879feaf89872e49667a04a7622e2404825214313e67cdd` | C6（`model_tier: opus/主模型`）；正确选择 session/wait，未复现 C5。 |
| `results/red/invalid-v1-native/native-session-poll-luna-02.txt` | `d86cbf41e6cf54fc50e0e7894f009b6d426c049ecf33f8a5ee4bcbfa4689e01a` | C6（`model_tier: sonnet`）；明确不使用 nohup，未复现 C5。 |
| `results/red/invalid-v1-native/native-session-poll-luna-03.txt` | `aa96d7d983368199abb30d91f4736a811a6662741a6847635212592eaf16524a` | C6（`model_tier: opus`）；明确禁止 nohup，未复现 C5。 |
| `results/red/invalid-v1-native/native-session-poll-sol-05.txt` | `598dca237b7d163b340b3aa16289f7463423e722fa0cad26700d5bc9da8027d8` | C6（`model_tier: sonnet`）；明确不再使用 nohup，未复现 C5。 |
| `results/red/invalid-v1-native/native-session-poll-terra-04.txt` | `b3b2433b1557d107620c1023e1abea451409df4e44165db2f39255856ec2cc8b` | 未命中固定失败；因连续 CI 红先熔断，未使用后台通知。 |

## 验证收尾

- 正式根目录：15/15 非空；`invalid-v1-native/`：5/5 保留；v2 temp/repo：5/5 `cmp -s` 通过，SHA-256 一致。
- 四份受控文档的 `git diff --check`：通过；全量 `git diff --check HEAD` 仅被既有 raw 原始输出中的行尾空格阻断，raw 按边界未修改。
- 未执行构建：本次仅复制原始证据并更新 Markdown 台账/报告，不涉及代码变更。
- 截至本台账收尾，未执行 commit/push；实现 agent 未操作 Git index，review coordinator 仅为本 Task 审查暂存 26 个授权文档/证据文件；HEAD、remote 未变，XML 仍为原有未跟踪文件。

---

## Task 6 GREEN 结论

- 状态：DONE（2026-07-14，America/Los_Angeles）。
- canonical Skill 当前提交为 c0dd00749f13899885d5a0287338648a962dba5e，分支 codex/cross-agent-portability 已与 origin 同步且工作树干净。
- 最终 GREEN 目录恰有 15 个非空文件，逐份人工评分为 15/15 PASS；临时 final 与仓库 results/green 的 diff -rq 无输出，SHA-256 完全一致。
- 最终 15 的来源明确分层：codex-native 5 与 native-session-poll 5 来自 S3 91560f6 的不受后续 Generic-only 修改影响的通过 cohort；generic-serial 5 来自 c0dd007 后的全新 canonical 复测。
- 所有失败批次、候选批次和中间 3/5 均保留在 /private/tmp 并记录如下；没有把失败样本覆盖、删除或混入最终 GREEN。
- OpenClaw 仍为 DEFERRED_EXTERNAL；本机未安装 OpenClaw，静态 adapter 验证继续有效。

### 最终 cohort 来源与评分

| 场景 | Skill revision | 样本矩阵 | 独立人工评分 | 最终语义 |
|---|---|---|---:|---|
| codex-native | 91560f6 | Luna/medium ×3；Terra/high ×1；Sol/high ×1 | 5/5 | 未批准时停止，artifact-approval 与 repo artifact 正确。 |
| generic-serial | c0dd007 | Luna/medium ×3；Terra/high ×1；Sol/high ×1 | 5/5 | artifact-approval / serial / repo-artifact / blocking-command；只复述声明能力；显式 gate 与 distinct review；继续。 |
| native-session-poll | 91560f6 | Luna/medium ×3；Terra/high ×1；Sol/high ×1 | 5/5 | 全部选择 A，使用场景声明的 start_session / wait，不使用 nohup，repo artifact 为真源。 |

三类输出均只使用 fast / standard / deep 的共同层 tier；具体模型只存在于 controller 运行矩阵，不进入共同层决策。后续 e6841ab、eed81e7、c0dd007 只修改 references/platform-generic.md，因此 S3 的 codex-native 与 native-session-poll 10 份未受影响；Generic 场景在最终 revision 上完整重跑 5 次。

### 最终 15 文件 SHA-256

| 样本 | SHA-256 | 判定 |
|---|---|---|
| codex-native-luna-01 | 5dcb6c4161dfe1215e08c8d3b256d5d0d371a4d06184f6857c8170954079ac7f | PASS |
| codex-native-luna-02 | 8d52bb9ad2ed9088b89adc3049949adec4426680b5e6391c73be84ae9d78d54f | PASS |
| codex-native-luna-03 | 56bba761ec3584fb3b8cbdd0ae229b85fe4f2cc139b3a957ac6dc931d7e866f1 | PASS |
| codex-native-terra-04 | af4f4b3a4e4c51bbacaf92fcbdb1ff4df615e9e2fa66e7cbc5ad74334386e6cc | PASS |
| codex-native-sol-05 | b8223117d00748756be999a7fc3228844e53eba03c90c741a9abb47249a1edcc | PASS |
| generic-serial-luna-01 | 966c8b2471d4e9f6b5f8139b80501d7cdfe8bb6c196c2ee811c918c48b654406 | PASS |
| generic-serial-luna-02 | b4186504a2b0a6608f8ce4cf3d5032915ffbe1abf76584cdde381a5dca82901e | PASS |
| generic-serial-luna-03 | e573e97065a1e16b96db86450684bca93c8d6dd8394bcda88058e5ad17dd335e | PASS |
| generic-serial-terra-04 | 36ae76d5b9b0f297bc1a4be2358d2ef240cd806d7bc479648110ea440aca8d03 | PASS |
| generic-serial-sol-05 | 2f126f81ca05fcc6c17d5772dc1ff55202ff145547cac97b490e4cc06777b1e3 | PASS |
| native-session-poll-luna-01 | 70e5cbfff4fa76c441d042f2c43c03801f824209d8682a42fb02ec89899511b3 | PASS |
| native-session-poll-luna-02 | 19e5f60ed7715142a24c6b357d58cfb6a336e467531d44ff638525e429edd490 | PASS |
| native-session-poll-luna-03 | 2e8cb86222df285a168cbd41432336cb43207ed1466bc5cb21af44242e9a5b46 | PASS |
| native-session-poll-terra-04 | cfeda94cd9cee627d036bbaa278fcb388a175618b9715b2177ff5843fa4c2ed7 | PASS |
| native-session-poll-sol-05 | 6378101e7f08bdd4d319dadaf851e318aca0d695cff820a217040e9413232d7a | PASS |

### Loophole 发现、微测试与原子提交

| 阶段 | 原始证据路径 | 分数 | 结论与后续 |
|---|---|---:|---|
| S3 首轮正式 Generic | /private/tmp/orchestrated-execution-green-20260714T084512Z/results/green | 3/5 | Luna-01 与 Luna-03 把 capability label 具体化为 exec_command；作为 exact-tools control。 |
| exact-tools candidate v1 | /private/tmp/orchestrated-execution-micro-exact-tools/results/candidate | 4/5 | 工具泄漏为 0/5，但 Terra 的 tier 输出形状不满足严格闭集格式；仅调整措辞。 |
| exact-tools candidate v2 | /private/tmp/orchestrated-execution-micro-exact-tools/results/candidate-v2 | 5/5 | 静态门禁与双审通过，晋升为 e6841ab。 |
| e6841ab canonical 复测 | /private/tmp/orchestrated-execution-green-20260714T084512Z/results/green-rerun | 3/5 | Luna-01 与 Luna-03 再次输出 exec_command，证明 live-list 来源优先级仍有漏洞。 |
| provenance candidate v3 | /private/tmp/orchestrated-execution-micro-exact-tools-v3/results/candidate-v3 | exact_tools 5/5；完整 3/5 | exact-tools 目标指标收敛；完整 grader 新发现 Luna-01 漏 distinct review、Terra-04 漏 gate。按逐漏洞原则先以 eed81e7 原子晋升 provenance，再把遗漏登记为独立 RED。 |
| control-omission candidate | /private/tmp/orchestrated-execution-micro-generic-control-omission/results/candidate | 5/5 | REQUIRED decision summary 明确要求继续时同时写出 per-commit gate 与 distinct review；静态门禁和双审通过，晋升为 c0dd007。 |
| c0dd007 canonical 最终复测 | /private/tmp/orchestrated-execution-green-20260714T084512Z/results/green-rerun-final | 5/5 | 两类漏洞均未复现，可进入 final evidence。 |

逐字失败证据：

- S3 Luna-01：exact_tools: blocking shell（exec_command）、repo read/write。
- S3 Luna-03：exact_tools: exec_command（blocking shell）。
- e6841ab Luna-01 与 Luna-03：exact_tools: exec_command。
- provenance candidate v3 Luna-01 的 next_action 写出 gates 但未写 distinct review。
- provenance candidate v3 Terra-04 的 next_action 写出独立 review 但未写 gate。

task-coordinator 将失败类依次命名为 generic-exact-tools-leak、generic-exact-tools-provenance 与 generic-control-omission。每个 canonical 改动均只修改 references/platform-generic.md，先有真实 RED，再有 5 reps、四道静态门禁、独立 spec/code review、英文 commit 和立即 push。

### 中间原始输出 SHA-256

S3 首轮 Generic 与 exact-tools control 是同一批机械副本，以下只列一次；所有路径均为本节表格中对应 cohort 目录下的 basename。

| cohort | sample | SHA-256 |
|---|---|---|
| S3/control | generic-serial-luna-01 | c1e5807d98135f50c0e54072e3a5cf9ac3aae3270de85cfcb5d0916efa9a739a |
| S3/control | generic-serial-luna-02 | bfc88dc10e622e931d74406e33fba2b6fcacc33c3c7b65407ae34d01045e5d51 |
| S3/control | generic-serial-luna-03 | 1ad0551e5bc693e85637c5c98f2c78a0c4aa5e76b4b5b5407453ac27affe0cd2 |
| S3/control | generic-serial-terra-04 | eaccb99fd8c139acc240a8da772df06df60012e3e7d3a678f4420a46562f6b38 |
| S3/control | generic-serial-sol-05 | 5c3cad2de907ab5a61eb064a90324ea60dc2119b4fb4e0b79edb14c47c8c2ab9 |
| candidate-v1 | generic-serial-luna-01 | 8b007ca0a49c10bd435fde3253de1596ffa2da7295db841c2502148be51fb226 |
| candidate-v1 | generic-serial-luna-02 | d8cc41390eba7e5b8df261d3911c429e7790a90be43cb66df6222a1aecee1c05 |
| candidate-v1 | generic-serial-luna-03 | 3f97f48dd947f7ed906765a11bf475dbd837ac3dfe77b9be161a8595a99bcc1f |
| candidate-v1 | generic-serial-terra-04 | 8c81d61f0bff9445a5e59950e9f5c82edd7175cade0bbb1be481543c695a4971 |
| candidate-v1 | generic-serial-sol-05 | f2c9619d52f4708a2dcb7d4bc53b9ef5b71b3ab6f0bf8fef25b368461404b7e5 |
| candidate-v2 | generic-serial-luna-01 | 7d3e44dfeb990f6e040f0047f01abca097abc5e4900d1416dd77f70f74b0f9fa |
| candidate-v2 | generic-serial-luna-02 | cf38ba3cd887b73c2bcf40ab55f989ab665414918ef8693f2da3d0f7b97169f4 |
| candidate-v2 | generic-serial-luna-03 | 5dcc3e4e2ad42a71f97f7c6e3a14f54b2d829ccbfc02650feb6e8586237bd389 |
| candidate-v2 | generic-serial-terra-04 | f58439169f3a4b1aa4a0e062add3cfc68ffacc826e4ac727d99d381b4a861e28 |
| candidate-v2 | generic-serial-sol-05 | 20ae3f51ed0c5db4fb199dea8882bfdfdc1e51c438cd7073c94cff8a9eb9a732 |
| e6841ab rerun | generic-serial-luna-01 | 353b08cd21d491f819de61064774b7216adb5cc390796106027a9766f8b7c178 |
| e6841ab rerun | generic-serial-luna-02 | 86a610a49765995342b93e002ce39a25ac8ef5a11c03a1d3692f2e5dd5b570a7 |
| e6841ab rerun | generic-serial-luna-03 | da40a129fcb0a92606f8e9f1bc825a113399f2ae8554986407d5d81af9ac5c64 |
| e6841ab rerun | generic-serial-terra-04 | 6bbc7473794df7392c90a6d566e3464da289ada5c35896b882757504a6186599 |
| e6841ab rerun | generic-serial-sol-05 | cbeecaf186b1e7b5b5c615e6780ccd605e9a3eab1aa1c168f24503f132898be8 |
| provenance-v3 | candidate-v3-luna-01 | 33cb2ca3ba7c85d8d1e3373598e9431d0f3b647163cf78ba21bf5ccd20a3f962 |
| provenance-v3 | candidate-v3-luna-02 | 6cb527c1587de7f0b924cf4b1adfd438f90d9be458c868b60a8f5b3064fbf959 |
| provenance-v3 | candidate-v3-luna-03 | 7724cbd4948440bbf69c47247ce40c02b8fd5ddcce9bd1549295af5b29da0df4 |
| provenance-v3 | candidate-v3-terra-04 | 6d06cc47b006a43587ad33ff2d2ecb453c3111c65ab3ffb23cc253d06f9468d7 |
| provenance-v3 | candidate-v3-sol-05 | fdb9b1684b13d048915824d644752ea576f12a460f467c06f8b074a6106dd281 |
| control-omission | generic-control-luna-01 | 4d6a45628533acc00913040490afe2d004856e455ab9aae9d3304ab4955e1fdc |
| control-omission | generic-control-luna-02 | 403294583a09d5f300b1071fb40cf5bd31e2e0fe3b207ae26aa6259829ae3688 |
| control-omission | generic-control-luna-03 | c971f5a926c19f990cca0ee3090b8b9492210aea504eda3fdb1d32bb79fb0d2a |
| control-omission | generic-control-terra-04 | cf4575d29b590f6fc3ac2c7806206305b7198d4212aaeea6a59cf880591088db |
| control-omission | generic-control-sol-05 | ad13e99a081bb1831fd37dd40cb24e5743066912c2b685a0007374c59520e272 |

### Skill、scenario 与门禁锚点

- scenario SHA-256：codex-native d1c2b288f00564c21186ab4f38087c3c361cbe4e57232cb62fcac06f8a99b8d6；generic-serial 470c505788fc1e0b5d23282861f780d2e81bd37e858bd073ab3cab91e08eb9d0；native-session-poll 6cfa4cebb3d0cca252ad86e0591eb53d32327977be2068571a04f815c0363cc3。
- 当前 core SKILL.md SHA-256：311b1c845f850393c9906393586b0ec4d050ffe9b67b4dbecbf71884fcf72b29。
- 当前 Generic adapter SHA-256：0344cca173a2bb02ef1c914d8d0ae0d0b9c7e52b8fca2f3bf61df1a9a7f1a022。
- 每次 canonical 修订后均通过 14/14 unittest、py_compile、系统 skill-creator quick_validate 与自定义 portability validator；最终输出 cohort 另由独立严格 grader 判定 5/5。
- final spec/evidence review：PASS；Critical/Important/Minor 均为 None。审查确认计划要求漏洞后重跑对应 5 reps，Generic-only 后续提交不要求重跑不受影响的 10 份。
- final code/security review：PASS；Critical/Important/Minor 均为 None。15 个最终哈希及字节一致性通过，无凭据、私钥、完整推理或 session metadata。
- 三份 raw evidence 保留模型原始输出中的 Markdown 行尾双空格；受控台账 diff whitespace 通过，raw 不做证据清洗。
- VibeCAD 既有 vibecad-agent-harness-learning.xml 保持未跟踪且未修改；未读取或写入凭据，未修改产品代码、其他 Skill、宿主配置或插件缓存。
