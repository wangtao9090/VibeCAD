# VibeCAD Revision durable-v2 迁移合同与 MR1-prep 编排

> 状态：MR1-P00-R1，合同冻结候选；durable-v2 **尚未实现或激活**
>
> 授权：`MRG1-A04`，绑定
> [`vibecad-multi-runtime-g1.md`](vibecad-multi-runtime-g1.md) 的
> MRG1-R2、§28.5、§28.7 与 `MRG1-D19..D22`；授权持久化并推送于
> `4d92d04eff11213a9c539c316451427a51f4dc6b`
>
> 当前产品事实：Revision/Candidate writer 仍固定写 durable v1
> `model.FCStd` + `model.step`。本文件只冻结迁移顺序、数据不变量、readiness
> 语义、恢复边界和后续 gate，不创建 v2 byte、migration marker、writer fence
> 或第二 CAD 支持。

架构摘要见 [`../ARCHITECTURE.md`](../ARCHITECTURE.md)，验收口径见
[`../ACCEPTANCE_TESTS.md`](../ACCEPTANCE_TESTS.md)，内部 adapter 与 durable
边界见
[`../CAD_RUNTIME_ADAPTER_GUIDE.md`](../CAD_RUNTIME_ADAPTER_GUIDE.md)。

## 1. 目标、成功条件与排除项

MR1 的目标是在任何 v2 writer 获准前，先把现有 durable v1 变成可复现、可双读、
可完整盘点、可备份恢复且可安全回退的兼容基线。成功必须同时满足：

1. 已存在的 v1 record、manifest、payload、digest 和 ancestry 永不被原地改写；
2. reader 先能严格读取 v1 和 future v2，writer 才可能在未来阶段切到 v2；
3. 同一 committed ancestry 可以包含 v1 ancestor 和 v2 descendant；
4. 已激活 v2 的 root 对 v1-only/downgrade writer fail closed；
5. activation 前完整盘点整个 durable data root，而不只扫描 revision；
6. backup、restore、abort 和 post-activation rollback 都有无歧义的恢复分支；
7. `structurally_ready` 只描述一次只读观察，`activation_ready` 只属于未来在
   全局 writer/maintenance fence 内的第二次完整扫描；
8. G1 的 disposable/exportable alpha 与非 disposable beta 保持分离。

本合同明确排除：

- 在 P00..P03 写 durable-v2 byte、切换 writer 或创建 activation marker；
- eager migration、历史 revision 重编码或任何 in-place rewrite；
- 把内部 `CadArtifactProfile`、`RuntimeDescriptor` 或其 Python 序列化形状当成
  durable/public schema；
- 修改 28-tool MCP surface、六 operation、`SelectorV1` wire contract 或
  `vibecad://artifact/{materialization_id}/{artifact_id}`；
- 添加第二 CAD、第二 native format、runtime auto-discovery 或产品支持声明；
- 启动/安装/探测 FreeCAD，或接触动态 user-owned course-script 文件；
- 把 P03、文档 gate 或 alpha 证据表述成 durable-v2 已实现、已激活或 beta-ready。

## 2. 版本域与当前代码事实

`schema_version` 只在所属 record family 内有意义，不能被解释成全局 data-root
版本。特别是：

- Revision manifest、`RevisionRef`、HEAD、journal 和相关 durable record 当前固定
  schema v1；Revision writer 仍固定输出 FCStd/STEP。
- Managed checkout open/tombstone record 已经各自具有 v1/v2 dual-reader，并由当前
  writer 写其本地 schema v2。这是 reader-before-writer 的已有先例，但**不是**
  Revision durable-v2，也不表示 data root 已迁移。
- Future Revision v2 必须拥有自己的明确 codec/checksum domain。它不能借用 checkout
  v2 的版本号、checksum domain 或兼容结论。

当前受审代码证据：

| 事实 | 当前证据 |
|---|---|
| Revision schema 与 writer 固定 v1 | `src/vibecad/execution/revisions.py` 的 `_SCHEMA_VERSION = 1`、`_manifest_body()` 和 strict v1 decoders |
| v1 Revision 只接受固定 payload | 同文件要求 `model.FCStd` / `fcstd` 与 `model.step` / `step` |
| checkout reader 已接受 v1/v2 | `src/vibecad/interaction/checkouts.py` 的 `_decode_open()` 与 `_load_tombstone_name()` |
| checkout writer 当前写 v2 | 同文件的 `_OPEN_SCHEMA_VERSION = 2`、`_TOMBSTONE_SCHEMA_VERSION = 2`、`_encode_open()` 与 `_write_tombstone()` |
| P00 前没有 golden corpus | `tests/fixtures/durable_v1/` 不存在 |
| P00 前没有 Revision codec seam | `src/vibecad/execution/revision_codec.py` 不存在 |
| P00 前没有 full-root inventory | `src/vibecad/application/durable_migration.py` 不存在 |

这张表是 P00 的 prewrite documentation-gap audit。P00 是 G0 文档合同，不制造
pytest RED；P01 才冻结 byte-exact v1 corpus，P02 才引入 reader dispatch，P03
才实现只读 inventory。

## 3. 授权、能力 profile 与本 packet 边界

当前能力 profile：

```text
approval: artifact-approval
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
model tier: deep -> gpt-5.6-sol / max
```

adapter 选择只使用以下四类证据：

- `live capability declarations`: 当前 Codex desktop session 声明 local
  patch/command、`spawn_agent`/`send_message`/`wait_agent` 与可恢复
  `exec_command`/`write_stdin`；native Plan approval state 未被声明。
- `observable behavior`: 本 campaign 已从 pushed repo artifact 恢复 A04，
  已 spawn/send/wait 独立只读审查，并可在原 command session 上 poll；这些观察
  支持 artifact-approval、spawn-send-wait、repo-artifact 和
  native-session-poll。
- `environment identity`: 当前 host 被动标识为 Codex desktop，repository 为
  `/Users/wangtao/Documents/DevProject/vibecad`，branch 为
  `codex/agent-stage3`。
- `public configuration`: 当前公开 session configuration 提供 unrestricted
  workspace filesystem、无 command approval prompt、sol/terra model selector；
  它不改变 packet allowlist，也不授予外部凭据、安装、发布或用户文件访问。

本 P00 implementation subagent 只可修改：

```text
M docs/ARCHITECTURE.md
M docs/ACCEPTANCE_TESTS.md
M docs/CAD_RUNTIME_ADAPTER_GUIDE.md
A docs/orchestrated/vibecad-durable-v2.md
```

rolling artifact `vibecad-multi-runtime-g1.md` 的 ledger append、exact staging、
commit 和 push 只由 controller 执行。本 Skill、批准记录与 task packet 都不能
扩大高优先级指令、host permission model、sandbox、allowlist 或授权范围。

## 4. 冻结决策

### DV2-D01 — v1 byte 与语义不可变

所有 committed Revision manifest/payload 与其 digest-bound ancestry 都是 immutable
history。P01 还会冻结 HEAD、journal、Task、checkout、artifact/bootstrap 等
operational record family 的 **v1 encoding**，但这不把本来由既有状态机合法 CAS/
atomic replace 的 HEAD、Task 或 coordination record 误写成永不变化的 instance。

P01 只能从 anchored implementation 生成并审查一次 golden corpus；正常测试没有
update-golden 模式。后续 codec、inventory、activation 或 rollback 不得为了
“规范化”重新序列化 committed history，也不得绕过现有 lifecycle 原地转换 mutable
operational state。

### DV2-D02 — absent-profile 只有一个含义

当且仅当一个严格合法的 Revision v1 manifest 没有 profile 字段时，reader 才能在
内存中映射到固定 legacy profile：

```text
domain: cad
profile_id: cad.freecad.fcstd-step
profile_version: 1
native: model.FCStd / fcstd / application/vnd.freecad.fcstd
exchange: model.step / step / model/step
```

这个映射不接受其他文件名、format、media type、额外 artifact、runtime 猜测或
provider fallback。缺 profile 的 v2、未知版本、v1/v2 hybrid 或不满足 exact legacy
FCStd/STEP invariants 的 record 一律 fail closed。

### DV2-D03 — dual-reader 必须先于 new-write-v2

顺序固定为：

```text
freeze byte-exact v1 corpus
→ insert strict version-dispatch seam with reader/writer still pinned to v1
→ full-root observational inventory
→ future approved strict v1/v2 reader with writer still pinned to v1
→ future approved writer fence + second full scan
→ verified backup
→ future atomic new-write-v2 activation
```

P02 不接纳 v2；它只让 unknown v2/version/profile fail closed 并为 future codec
留 seam。Dual-reader 和 writer activation 都需要 P04 或后续的新批准，而且 writer
activation 不能与 v2 reader 首次交付在同一未验证步骤中发生。

Rolling artifact 中“P04 and later v2 reader/writer activation”的旧合并措辞不能被
解释成同一步首次读、首次写。Controller 在发出 future packet 前必须 append 明确的
reader-first / writer-later sequence（例如独立 P04a reader gate 后再 P04b/P05 fenced
writer gate；编号仅为说明，尚未授权）。

### DV2-D04 — mixed ancestry 是正常状态

每个 revision 独立携带或归一化自己的 durable profile。Reader 从 HEAD 沿
`base_revision` 对每个 manifest 独立 dispatch，因此允许 v1 ancestor → v2
descendant。Revert 始终创建新的 forward revision；它可以读取历史 v1 内容，但在
v2 激活后只能由 active v2 writer 创建 v2 descendant，不能把 HEAD 倒退或复制成
新的 v1 child。

### DV2-D05 — downgrade writer fail closed

一旦 root 完成 v2 activation：

- active root 必须有 future-approved、durable、身份绑定的 activation fact；
- v1-only writer 在任何 candidate、journal、HEAD 或 artifact mutation 前必须发现
  activation fact、v2 HEAD/ancestry 或未知 version/profile，并拒绝写入；
- v2 state 不得伪装成 v1-compatible record 来帮助旧 writer 继续；
- 任何看见 v2 后仍创建 v1 revision、journal 或 task mutation的路径都是 beta
  release breaker。

### DV2-D06 — 不 eager、不原地 rewrite

Activation 只改变**未来新 revision 的 writer**。已有 committed v1 manifest/payload
与 digest-bound Draft/Artifact history 保持原 byte。HEAD、Task、journal、checkout
等 operational records 在正常产品 lifecycle 中仍可按既有 CAS/atomic state machine
合法变化；migration 本身不能 eager rewrite、原地 schema-convert 或伪造这种业务
transition。需要升级的 control fact 只能在未来批准的 copy-on-write/atomic
publication 流程中产生明确的新版本。Inventory、preflight、read/list/compare/export
和 dry run 全部 mutation-negative。

### DV2-D07 — durable domain schema 不复制 internal runtime 对象

Future Revision v2 的 profile 是 CAD-domain durable value，最少表达：

- 独立的 profile schema version；
- 稳定 `domain`、`profile_id` 与 `profile_version`；
- artifact role、format、media type、required/cardinality 约束；
- manifest payload descriptor 到上述 role 的明确绑定；
- profile 与 manifest 自己的 checksum domain/version。

它不序列化 Python class/module 名，不包含 `RuntimeDescriptor.metadata`、
capability list、execution profile、install path、receipt、adapter instance 或
`CadArtifactProfile` dataclass 形状。生成时的 exact runtime identity 如需留存，
属于独立 provenance/evidence，不决定历史 byte 能否解码。

初始获准映射仍只有 `cad.freecad.fcstd-step@1`。这种 domain shape 为未来
versioned profile 保留扩展边界，但不添加第二 CAD、第二 native format 或公共
runtime selector。Future exact v2 JSON keyset、canonical byte encoding 与 writer
allowlist 必须在 P04 或后续新批准中冻结；P00 不假装已经实现 encoder。

### DV2-D08 — inventory 必须覆盖完整 durable root

P03 从 `data/` root identity 开始，观察 root topology 与 `locks/` state，再扫描所有
record-bearing store domain；它不是只遍历 `projects/*/revisions`：

```text
data/ root identity/topology
locks/ observation
projects/
tasks/
bootstrap/
checkouts/
artifacts/
```

`locks/` 是 control-plane namespace：P03 只观察其 directory identity、closed entry
allowlist 与 path-free file facts，不取得、释放、删除或改写 lock。现有
`<64hex>.lock` entry 在 lease release 后仍保留，因此“文件存在”不表示 active
lease，也不能证明或否定 quiescence。只有 future daemon quiescence + global fence
能证明 activation 所需的 writer exclusion。其余 store
覆盖 bootstrap request/staging/work/normalized residue、project HEAD/revision/
candidate/journal/reservation/seed、Task 与 Draft、checkout open/tombstone/temp、
artifact materialization/request/delivery/temp。任何未知 top-level 或 nested entry、
不安全 file kind、symlink/hardlink、损坏/重复 record、悬空引用、digest mismatch、
ambiguous recovery 或超预算集合都成为 blocker，不得忽略。

### DV2-D09 — readiness 分两种，且都不是永久证明

P03 report 只允许：

```text
structurally_ready: true | false
blockers: closed, bounded, deterministic set
start_change_tokens: path-free store tokens
end_change_tokens: path-free store tokens
```

`structurally_ready=true` 只表示这次 sequential read-only observation 中：

- 没有 unknown/corrupt/ambiguous/dangling entry；
- 没有观察到 active candidate/journal/reservation/temp；
- references、digests、counts 与 identity closure 完整；
- start/end token 没有显示可观察漂移。

P03 schema 不含 `ready` 或 `activation_ready`。它不能证明并发 writer 已静止。

Future activation stage 必须先 quiesce daemon、持有 approved global
writer/maintenance fence，并在 fence 内重新执行**完整**扫描。只有这次第二扫描
同时通过结构、引用、capacity、backup precondition 与 fence identity 检查时，future
versioned report 才能在 fence 仍有效期间给出 `activation_ready=true`。释放 fence、
token 漂移或启动新 writer 会使该结论失效。

### DV2-D10 — backup、restore、abort 与 rollback 是 activation 的组成部分

Activation 前必须：

1. 计算 path-free inventory 与 worst-case copy/temporary/free-space budget；
2. 在 fence 内取得完整 durable-root snapshot；
3. 把 backup 放在 live root 外的 owner-private、身份绑定目标；
4. 记录 path set、file kind、mode、uid、size、mtime、ctime、device/inode identity
   和每个 regular file SHA-256；
5. fsync/close 后从 backup 独立重读并验证 manifest、hash 和 cross-reference；
6. 在 activation 前实际演练 restore 到隔离 root；比较 logical relative path set、
   file kind、mode、uid、size、content hash、canonical record 与 reference closure，
   同时要求 restored file 与 live/backup file 不是同一 inode/hardlink。

同一 live tree 的 preflight before/after 比较必须保持 mtime、ctime、device/inode
identity 不变，以证明 scan 无 mutation。Backup/restore equivalence 则不能要求
device/inode/ctime 相同：restore 必然产生新的 object identity/ctime；mtime 是否保留
必须由 future backup format 明确，不能拿 object-identity equality 当 restore 成功
标准。

失败发生在首个 committed v2 byte 之前时，abort 删除/隔离未发布 temp 和未完成
activation fact，验证 live root 与 backup/preflight 完全一致，再恢复 v1 writer。

首个 committed v2 byte 之后禁止“把 v2 降回 v1”或覆盖历史。安全 rollback 只有：

- 保留 v2-aware reader，停止 writer 并进入 read-only/recovery-required；或
- 在 fence 内经独立授权恢复**整个** pre-activation snapshot，明确丢弃全部
  post-activation state，逐字节验证后才允许 v1 writer 恢复。

部分目录 restore、仅改 marker、把 v2 manifest 重写成 v1 或让 v1 writer 接在 v2
ancestry 后面都 fail closed。

Backup manifest 必须覆盖 `locks/` 中每个 allowed persistent regular-file byte 与
metadata，但不能声称复制了 kernel/OS lock ownership；进程 quiescence 与 future
global fence 才是“无 holder”的证据。Future restore policy 必须在 fence 内逐项验证
lock name/type/owner/mode/link/byte，再以独立 object identity 恢复或明确重建；不得
blind tree-copy、根据 stale 文件存在与否猜 lease state，或静默遗漏/删除 lock entry。

### DV2-D11 — G1 alpha 与 shared beta gate

G1 real Workbench alpha 可使用明确 disposable 的 v1 project，或使用已经独立导出并
验证可恢复的 v1 data；G1 不得依赖 migration path、v2 profile 或 absolute durable
path。任何承诺升级后保留用户项目的 non-disposable beta 必须等待
[`§28.7 shared gate`](vibecad-multi-runtime-g1.md#287-shared-non-disposable-beta-gate)
完成。

### DV2-D12 — 后续实现需要新边界

P00..P03 只覆盖 contract、corpus、v1 version-dispatch seam 与 observational
inventory。P04 或后续的 dual-reader、new-write-v2、global fence、activation
marker、backup target、restore command、exact v2 codec/encoder 和 release migration
UX 都需要 future artifact append、exact allowlist 和新批准。第二 CAD 继续明确
deferred。

## 5. Reader、writer 与 ancestry 合同

### 5.1 Strict dispatch

Reader 先做 bounded lexical JSON 检查和 duplicate-key 拒绝，再根据该 record family
的 exact schema version dispatch。每个 codec 要求 exact keyset 和独立 checksum
domain。Unknown version/profile/field、wrong type、non-finite number、oversize value
或 v1/v2 hybrid 不能由 permissive mapping、default object constructor 或“尽力读取”
接纳。

v1 decode 产生 immutable domain value，并按 DV2-D02 补充**仅内存中**的 legacy
profile；不能把补充字段写回 v1 manifest。v2 decode 产生相同 domain-level read model，
但保留 explicit durable profile metadata 供 migration/compatibility 判定。现有
`RevisionRef.to_mapping()` 与 public projection 在 P02 不增加 profile 字段。

### 5.2 Writer state

| 阶段 | Reader | Writer | Activation fact |
|---|---|---|---|
| 当前 / P00 / P01 | v1 | byte-exact v1 | 无 |
| P02 / P03 | strict v1 dispatch；unknown v2 fail closed | byte-exact v1 | 无 |
| future dual-reader | strict v1 + v2 | byte-exact v1 | 无 |
| future pre-activation | strict v1 + v2 | byte-exact v1，fence 内暂停 | prepared，未激活 |
| future activated | strict v1 + v2 | new revision only v2 | activated |
| recovery-required | strict v1 + v2 | disabled | 保留可诊断事实 |

任何 reader/writer 组合无法匹配这张表都不能开放写入。

### 5.3 Mixed-lineage operations

Create、review、Accept、revert、reconcile、checkout 和 artifact delivery 必须绑定
每个 revision 的 decoded profile 和 exact digest，而不是从当前 runtime 或 HEAD
profile 猜 ancestor。Opaque checkout/grant 和 artifact URI 保持兼容；G1 不读取
manifest、profile 或 internal path。

Reconcile 遇到 v1 prepared journal 与 v2 HEAD、v2 journal 与 v1-only writer、
unknown activation state或 profile/digest mismatch 时进入 fixed
recovery-required/error，不能自动选一个较新的 timestamp 或继续写。

## 6. Full-root inventory 与 preflight

P03 inventory 必须接收 Application composition 已经 pin 的
`ApplicationDataLayout` root 与六个 child identities（`locks` 加五个 record
stores），并只使用 bounded、non-creating read-only observation/snapshot hook。
它不能调用会创建缺失 root/child 并 fsync 的 `ApplicationDataLayout.open()` 来建立
baseline，也不能通过取得 catalog/quota/resource lease 来“只读”扫描，因为首次
lease acquisition 可持久化新的 lock file。它不启动
Worker/runtime，不取得 project write lease，不 repair/reconcile/delete，不暴露 absolute
path，也不跟随 symlink。`workflow/store.py` 已有 Task snapshot；只有经证据证明缺少
只读事实，才可另开批准修改。

当前审计已经证明现有 `TaskRunStore.snapshot()` 会取得 catalog lease，Revision
discovery snapshot 会取得 quota lease；两者都不满足 P03 mutation-negative baseline
的直接复用条件。P03 必须使用 future reviewed non-creating hook。若这需要修改当前
exact allowlist 外的 `workflow/store.py`，先停止并取得单独批准，不得从 migration
module 绕过 store authority。

每次 inventory：

1. 先验证已经 pin 的 root/child identities，再以 non-creating read-only hook 取得
   每个 store 的 start change token；
2. 枚举 closed allowlist 中的全部 entry，并对数量、深度、单文件与总 bytes 设硬上限；
3. 对 regular file 记录相对类型 token、mode、uid、size、mtime、ctime、device/inode
   与 content hash；报告不得包含本地 path；
4. strict decode 每个已知 record，构建 project/revision/task/draft/checkout/artifact
   reference graph；
5. 验证 manifest/payload/checksum/digest、HEAD ancestry、journal/candidate、
   task/draft/verdict、materialization 与 checkout source closure；
6. 再取得 end change token 和 root identity；
7. 输出稳定排序的 counts、versions/profiles、blocker code/subject 和 readiness。

如果观察前后 path set、content hash、file kind、mode、uid、size、mtime、ctime 或
object identity 改变，本次 scan 只能报告 visible drift blocker。Access time 不参与
比较，因为 verification read 本身可能改变 atime。

Inventory output 只使用 validated opaque IDs、record family、relative type token、
count、size、digest 与 fixed blocker code。绝对路径、异常文本、record 原文、secret、
user content 和 environment value 不进入 report。

## 7. Future activation、backup 与 recovery 状态机

```text
v1_only
  → dual_reader_v1_writer
  → observationally_structural
  → fenced_preflight
  → backup_verified
  → activation_prepared
  → v2_writer_active
```

每条 transition 都必须是 durable、idempotent、checksum-bound 且可由 restart
reconcile。`activation_prepared` 不允许 v2 write；只有 atomic publish
`v2_writer_active` 后 future writer 才能创建新 v2 revision。

Crash/restart 分支：

- fence 前 crash：现有 v1 truth 不变，重新 inventory；
- fence 内、backup 前 crash：writer 保持 disabled，清理只能按 durable state；
- backup 未独立验证：activation 不前进；
- prepared 未 active：验证 live root/backup 后 abort 或重新执行同一 intent；
- active response unknown：先读 durable activation fact 和 HEAD，不盲重放；
- active 后 write unknown：保留 v2-aware reader，reconcile exact journal/HEAD；
- restore interrupted：writer 保持 disabled，重新验证 restore intent、backup
  identity 和 destination state；不能混用 live 与 backup 子树。

Backup 不等于 rollback 成功；只有 restore 后完整重扫、byte/hash/identity 与
cross-reference 验证通过，且 active v2 state 的处置无歧义，才能关闭恢复。

## 8. Gate 与放行标准

### 8.1 MR1-prep commit sequence

| ID | Commit subject | Scope | Independent gate |
|---|---|---|---|
| P00 | `docs(mr1): freeze durable-v2 migration contract` | 本文件与三份 canonical docs；controller 另附 rolling ledger | `git diff --check && PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_agent_skill.py`，relative-link 与 no-overclaim audit |
| P01 | `test(durable): freeze byte-exact v1 golden corpus` | reviewed v1 fixture bytes、index 与 corpus test；不改 production | fixture SHA-256/size/canonical-byte equality，normal test 无 update mode |
| P02 | `refactor(revision): insert version-dispatch codec seam` | new codec、Revision integration 与 focused tests | v1 corpus byte-identical；strict v1 dispatch；unknown v2/hybrid fail closed；reader/writer 仍 v1 |
| P03 | `feat(migration): add read-only durable inventory` | new inventory 与 bounded read-only snapshot hooks/tests | root/locks + five-store mutation-negative scan；before/after facts identical；只输出 `structurally_ready` |

P01..P03 的 exact path allowlist 和 focused command 以
[`§28.5–28.6`](vibecad-multi-runtime-g1.md#285-mr1-prep-exact-first-implementation-campaign)
为权威。任何新 path、production correction、v2 writer 或 activation/fence code 都是
breaker，必须重新批准。

### 8.2 Shared non-disposable beta gate

同一 exact integrated build 必须证明：

1. old v1 projects byte-identical 且可读，read/list/compare/export 不触发 rewrite；
2. new v2 revision 与 mixed v1 → v2 ancestry 可 create/review/revert/reconcile；
3. pending draft 跨 process 与 FreeCAD restart；
4. G1 只经 opaque checkout/grant 打开 v1 与 mixed-lineage HEAD/draft；
5. verdict、stale/revoked/dirty 与 Accept/Reject 仍权威；
6. artifact URI 保持兼容；
7. interrupted activation、backup restore 与 rollback 收敛；
8. old writer 遇到 v2 state fail closed；
9. source file 与 rejected HEAD 不受污染；
10. addon、wheel、MCPB 与 host skill byte hash 精确受审。

任一 v1 history byte 改变、data loss、ambiguous recovery、unknown inventory entry、
capacity 不足、backup 未独立验证、dry run 超 maintenance window、G1 durable-path
依赖或 old-writer mutation 都阻断 beta。达到任一阈值时强制真实 snapshot dry run：

```text
projects >= 100
or revisions >= 1,000
or v1 bytes >= 10 GiB
```

## 9. Manual validation matrix

| 场景 | 环境 | 预期观察 | Evidence owner / 时机 |
|---|---|---|---|
| v1 backup/restore drill | owner-private isolated clone，future fence implementation | logical path/kind/mode/uid/size/hash/record/reference 等价；restored inode 与 live/backup 独立，ctime 允许因 restore 改变 | controller；activation 前 |
| threshold snapshot dry run | 达到 100 project / 1,000 revision / 10 GiB 任一真实 snapshot | 在 maintenance window 与 capacity budget 内完成，否则 NO-GO | operator + controller；beta 前 |
| mixed ancestry G1 preview | exact integrated build + real managed FreeCAD Workbench | v1/v2 HEAD 与 draft 均只经 opaque checkout/grant 打开，read/list/compare/export/preview 不改写历史，UI 无 durable path/profile authority | G1 owner；shared beta gate |
| interrupted activation/restore | isolated disposable copy，逐 transition fault injection | restart 后 writer disabled 或收敛到唯一 durable state；无 partial restore | MR1 owner；shared beta gate |
| old-writer downgrade | v2-active disposable snapshot + reviewed old writer | mutation 前固定 fail-closed；tree/hash 不变 | controller；shared beta gate |

P00 不执行这些 manual checks；它只冻结预期、owner 和未来时机。

## 10. 预算、circuit breakers 与 expected impact

MR1-prep 固定四个 semantic commit：P00..P03；每次只允许一个 commit 的 exact
allowlist，index 由 controller 单独拥有。P00 expected impact 仅为文档与
`tests/test_agent_skill.py` link/contract consistency；不应改变 source、test、public
schema、runtime、package 或 golden byte。

立即停止的 circuit breaker：

- P00 subagent 修改 rolling artifact、source、test、index 或 allowlist 外路径；
- convenience-serialize `CadArtifactProfile` / `RuntimeDescriptor`；
- 宣称 v2 已实现、P03 为 activation-ready、G1 为 durable beta；
- eager/in-place v1 rewrite、v1 corpus update mode 或 mixed ancestry 被禁止；
- G1 获得 durable path/profile authority或引入第二 CAD；
- inventory 接触未知/用户文件内容、泄露 path、跟随 link 或执行 mutation；
- preflight 为建立 baseline 而创建 layout child、lock file 或取得 lease；
- activation 没有 fence/second scan/verified backup，或 downgrade writer 可写；
- unexpected gate RED、stage/index drift、dynamic course-script contact；
- 安装/启动 FreeCAD、外部凭据/网络、tag/release/commit/push。

## 11. Residuals

| ID | Evidence / impact | Disposition | Closure condition |
|---|---|---|---|
| DV2-RES-01 | P00 前没有 byte-exact v1 corpus；refactor 缺少防漂移 oracle | OPEN，P01 owner | reviewed indexed corpus gate GREEN |
| DV2-RES-02 | Revision version-dispatch codec seam 尚不存在；reader/writer 仍 v1 | OPEN，P02 owner | strict v1 seam tests GREEN、unknown v2 fail closed 且 v1 byte identity 不变 |
| DV2-RES-03 | full-root inventory 与 `structurally_ready` report 尚不存在 | OPEN，P03 owner | mutation-negative inventory gate GREEN |
| DV2-RES-04 | strict dual-reader、global fence、`activation_ready`、v2 writer 与 activation fact 未批准/实现 | DEFERRED，future approval | exact future packets、reader-before-writer、fenced second scan 与 activation gates GREEN |
| DV2-RES-05 | backup target、capacity thresholds、restore implementation 与 fault matrix 未实现 | DEFERRED，future activation owner | isolated restore + integrated recovery gates GREEN |
| DV2-RES-06 | non-disposable G1 beta 尚未通过 shared gate | OPEN，controller | §8.2 exact integrated build GREEN |
| DV2-RES-07 | 第二 CAD 没有需求或产品证据 | DEFERRED by design | new product decision、adapter/engine/durable acceptance approval |
| DV2-RES-08 | rolling artifact 把 future v2 reader/writer activation 写在同一 P04-and-later 句中；若合并执行会违反 reader-before-writer | OPEN，controller clarification | future implementation packet 前 append reader-first / writer-later exact commit sequence 与独立 gates |
| DV2-RES-09 | current Task snapshot 与 Revision discovery snapshot 会取得 catalog/quota lease，首次使用可创建 persistent lock file；直接复用会破坏 P03 mutation-negative baseline | OPEN，P03 design/controller | non-creating read-only hooks 的 exact allowlist 获批并通过 before/after no-mutation gate |

Residual 不得在当前 packet 中顺手修复。

## 12. Recovery snapshot MR1-P00-S01

### S01-1 — Completed milestones

- MRG1-R2 与 A04 authorization 已在
  `4d92d04eff11213a9c539c316451427a51f4dc6b` 持久化并推送。
- P00 candidate 执行期间 controller 已把独立 G1-C00P 提交/推送到
  `50220446b851f8c0008dea4405cd09a3dadee11b`；当前 HEAD/upstream 相等、index
  为空，这不扩大 P00 四路径 diff。
- P00 prewrite audit 已确认 current v1 writer、checkout-local v1/v2
  dual-reader/current-v2 writer，以及 corpus/codec/inventory/本文件原先缺失。
- 本 revision 只形成四份 unstaged documentation candidate；没有 source/test/index/
  commit/push authority。

### S01-2 — Ordered next steps

1. 独立 deep review 本合同和三份 canonical projection。
2. 运行 exact P00 GREEN、relative-link/no-overclaim/allowlist/status 检查。
3. controller 将结果与独立 review 对照；若任何 blocker 或 unnamed path 出现则停止。
4. controller append rolling ledger，exact-stage 五份 semantic doc path，机械复核，
   commit/push。
5. 只有 P00 push 后，才按独立 packet 开始 P01；P01/P02/P03 严格串行依赖其前项
   GREEN/push。

### S01-3 — Active decisions and authorization

- MRG1-A04、D19..D22 与本文件 DV2-D01..D12 生效范围只到 P00..P03。
- v1 immutable、reader-before-writer、mixed ancestry、downgrade fail-closed、
  full-root preflight、verified backup/restore 与 shared beta gate 均不可 waiver。
- P04、writer activation、global fence 与第二 CAD 未授权。

### S01-4 — Execution discipline

- profile：artifact-approval / spawn-send-wait / repo-artifact /
  native-session-poll；deep 映射 sol/max。
- allowlist：本 packet 的四份 documentation candidate；rolling ledger/index/commit/
  push 归 controller。
- gate：P00 exact command、relative-link、claim、allowlist、diff/status/hash 与独立
  review。
- recovery：从 pushed anchor 与本文件恢复，重新核对 HEAD/branch/status/index、
  A04、dynamic exclusions、current gate evidence 和第一个 OPEN residual；不依赖聊天
  memory，不接触 user-owned course-script content。
