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
