# VibeCAD 内部 CAD Runtime Adapter 开发指南

> 状态：MR0-C05 / internal Python boundary
>
> 本指南描述当前源码中的内部合同、注册和 conformance 流程。它不是公共 SDK、MCP wire schema、
> adapter plugin API 或第二 CAD 产品支持声明。

当前产品架构与非声明边界见
[`ARCHITECTURE.md`](ARCHITECTURE.md)，验收状态见
[`ACCEPTANCE_TESTS.md`](ACCEPTANCE_TESTS.md)，批准、gate 与 residual 的权威记录见
[`orchestrated/vibecad-multi-runtime-g1.md`](orchestrated/vibecad-multi-runtime-g1.md)，
MR1 Revision reader/writer、inventory 与 recovery 边界见
[`orchestrated/vibecad-durable-v2.md`](orchestrated/vibecad-durable-v2.md)。

## 1. 先区分四个边界

- **managed runtime** 是 installer、receipt、supervisor 和受管 Python/FreeCAD 环境，不是本指南的
  generic invocation lifecycle。
- **generic runtime lifecycle** 是 domain-neutral immutable values、descriptor registry 和五方法
  control port。它不包含 CAD、FreeCAD、重建或仿真语义。
- **CAD runtime adapter** 是内部 CAD identity、capability planning、artifact/selector qualification
  和 exact adapter selection 边界。它本身不是完整 CAD execution protocol。
- **provider** 是未来可读取 sealed Revision/immutable Artifact 并返回 immutable artifact/proposal
  的 producer。provider 不拥有 Task、Revision、lease、review、Accept/Reject 或 HEAD commit 权威。

“supported”或“connected”只适用于已进入真实 default composition、具有 native engine evidence 并
通过产品验收的 adapter。当前唯一符合该定义的 CAD adapter 是 FreeCAD。

## 2. Generic runtime immutable API

实现入口位于
[`src/vibecad/runtime/contracts.py`](../src/vibecad/runtime/contracts.py)：

| 类型 | 当前精确职责 |
|---|---|
| `RuntimeIdentity(family, provider, version)` | 一个 exact versioned runtime；deterministic key 为 `family/provider@version` |
| `RuntimeCapability(name, version=1)` | exact name/version capability；没有隐式 version fallback |
| `RuntimeDescriptor(identity, capabilities, execution_profiles, metadata)` | immutable discovery snapshot；capability/profile 去重并稳定排序 |
| `RuntimeBudget(max_elapsed_ms, max_memory_bytes, max_output_bytes)` | caller-owned 正整数上限；合同本身不拥有 clock 或 enforcement process |
| `RuntimeArtifact(artifact_id, kind, media_type, digest, runtime, metadata)` | runtime-qualified immutable artifact descriptor；digest 是 lowercase SHA-256 形状 |
| `RuntimeInvocation(...)` | immutable invocation、owner、Task correlation、runtime/capability、budget/deadline、sealed inputs、payload 和 profile |
| `RuntimeStatus(invocation_id, runtime, state, diagnostics)` | 一次 lifecycle 状态快照 |
| `RuntimeHealth(runtime, state, diagnostics)` | exact runtime identity 的 health 快照 |
| `RuntimeResult(...)` | terminal result envelope；可含 artifacts、provenance、diagnostics、evidence 和 JSON output |
| `RuntimeProvenance(...)` | result runtime/invocation 与输入 artifact IDs 的 immutable attribution |
| `RuntimeDiagnostic`, `RuntimeEvidence` | bounded diagnostic 和仍需 domain verification 的 evidence |

这些 module-level class names 是当前内部 Python surface；`vibecad.runtime.__init__` 没有把它们重导出
为稳定第三方 facade。所有 contract dataclass 都是 frozen/slots；传入的 mappings/sequences 会被
有界 snapshot，而不是保留 caller-owned mutable containers。`RuntimeLifecycleState` 的 closed values
是 `PENDING`、`RUNNING`、`SUCCEEDED`、`FAILED`、`CANCELLED`、`UNKNOWN`；
`RuntimeHealthState` 是 `HEALTHY`、`DEGRADED`、`UNAVAILABLE`、`UNKNOWN`。

Generic descriptor registry 位于
[`src/vibecad/runtime/registry.py`](../src/vibecad/runtime/registry.py)。
`RuntimeRegistry` 注册 `RuntimeDescriptor`，不注册或启动 provider/control instances。lookup 使用完整
`RuntimeIdentity`；重复 identity 和未知 exact version 都 fail closed。

## 3. Lifecycle control 与 result caveat

`RuntimeControlPort` 的合同只有以下五个 synchronous instance method：

- `start(self, invocation)`
- `get_status(self, invocation_id)`
- `cancel(self, invocation_id, *, reason)`
- `reconcile(self, invocation_id)`
- `health(self, identity)`

它没有 `get_result`、`wait_result` 或 output-stream method。application/domain integration 必须通过自己
已经授权的执行边界观察 terminal result，再把 immutable observation 放入 conformance transcript。
测试中的 concrete-result helper 不是 `RuntimeControlPort` API。

一个有效 generic conformance fixture 应实际执行两条不同 invocation：

1. success invocation：`start`、`get_status`，取得 terminal `SUCCEEDED` status，并提供
   caller-observed `RuntimeResult`；
2. cancellation invocation：`start`、`cancel`、`reconcile`，最终和 reconcile 都为
   `CANCELLED`；
3. 对 exact descriptor identity 调 `health`；
4. success result 的 runtime、invocation、terminal state、provenance 和 input artifact IDs 必须与
   invocation 一致。

`RuntimeResult` constructor 只在 artifacts 非空时强制 provenance 存在；C04 的 successful
conformance transcript 更严格，要求匹配的 provenance，即使合法结果没有 output artifacts。
若未来需要从 generic port 本身取得 result，必须单独 version C01，而不是在 adapter guide 中发明第六
方法。

## 4. CAD identity、capability 与五种 decision

CAD domain contract 位于
[`src/vibecad/interaction/cad_runtime.py`](../src/vibecad/interaction/cad_runtime.py)。

`CadRuntimeIdentity` 包装 `RuntimeIdentity`，并要求 `family == "cad"`。普通 capability request 使用
`RuntimeCapability`；非 portable behavior 使用 `CadRuntimeExtension`，其 extension name 必须以
`<provider>.` namespace 开头。

`CadRuntimeDescriptor.plan()` 只返回五种显式 decision：

| Decision | Executable | 语义 |
|---|---:|---|
| `CadNativeDecision` | 是 | exact requested capability 被 runtime 原生声明 |
| `CadSemanticMappingDecision` | 是 | 选择另一个已声明 capability，并携带 bounded disclosure |
| `CadApproximationDecision` | 否 | 只提出 approximation proposal，等待后续明确决定 |
| `CadUnsupportedDecision` | 否 | mutation 前以固定 reason 拒绝 |
| `CadExtensionDecision` | 是 | 执行 provider-namespaced extension 的已声明 capability |

没有配置 decision 时，exact declared `RuntimeCapability` 自动得到 native decision；其他 request 自动
得到 `capability_not_declared` unsupported decision。mapping 或 extension 的 selected capability 也必须
存在于 generic descriptor。approximation/unsupported 传给 `adapter_for()` 时必须抛出
`NonExecutableCadDecisionError`，不能静默 fallback。

## 5. Artifact profile 与 selector authority

`CadArtifactProfile` 属于一个 exact `CadRuntimeIdentity`，并包含 versioned
`CadArtifactDeclaration(runtime, role, kind, media_type, version)`：

- profile 必须恰好声明一个 `CadArtifactRole.NATIVE_MODEL`；
- `SEMANTIC_OBSERVATION`、`SELECTOR_MAPPING`、`PROVENANCE` 各最多一个；
- kind 在一个 profile 内唯一；
- concrete `RuntimeArtifact` 必须匹配 exact runtime、declared kind 和 media type。

其他可用 role 是 `EXCHANGE` 与 `EVIDENCE`。这些检查限定 metadata，不会打开 artifact、重新计算
digest 或证明 native engine 能读写它。真实 byte identity、hash、reload、STEP parsing 和几何事实仍由
FreeCAD execution/domain verifier 负责。

`CadArtifactProfile` 与 `RuntimeDescriptor` 是 admission/planning 用的内部 runtime snapshot，不是
durable/public schema source。MR1 future durable profile 必须另行定义为 versioned CAD-domain value，
只表达稳定 profile identity、artifact role/format/media/cardinality 与 payload binding；不能
convenience-serialize Python class/module、runtime metadata/capability、execution profile、安装路径、
receipt 或 adapter instance。生成 runtime 如需留存，属于独立 provenance/evidence，不决定 historical
revision 能否 decode。

`CadSelectorEnvelope` 始终包含现有 revision-bound `SelectorV1` semantic authority。可选
`NativeLocator` 只提供 runtime-specific execution/evidence reference，且 runtime 和 revision 必须与
envelope/semantic selector 相同。裸 `NativeLocator`、`Face3`、`Edge8` 或其他 ephemeral index 不能成为
durable selector；`without_native()` 必须保留 semantic selector。

现有 selector 定义与 resolution 见
[`src/vibecad/execution/selectors.py`](../src/vibecad/execution/selectors.py)，CAD nominal execution
boundary 见
[`src/vibecad/interaction/cad.py`](../src/vibecad/interaction/cad.py)。

## 6. Structural adapter、registration 与 routing

`CadRuntimeAdapter` 是 runtime-checkable structural protocol，精确要求：

- `runtime_descriptor` property 返回 `CadRuntimeDescriptor`；
- `generation_lost` property 返回 `bool`；
- `terminate_generation()` 可零参数调用；
- `close_generation()` 可零参数调用。

它不定义 `validate_program`、`execute_program`、checkpoint、export、artifact persistence 或 result
retrieval。当前完整 product execution 仍通过 nominal `CadExecutionPort`；仅实现上述四个 members
只能证明 metadata/admission/routing shape。

`CadRuntimeAdapterRegistry` 的正确注册顺序是：

1. snapshot 最多 256 个 adapter references；
2. 对每个 adapter 执行一次 authority/structural/metadata admission；
3. snapshot exact descriptor、identity 和 adapter；
4. 按 family/provider/version 稳定排序；
5. 同时建立内部 generic descriptor registry；
6. 重复或未知 exact identity fail closed。

registry 没有 entry-point scan、package plugin scan、环境变量发现或 version negotiation。
`CadRuntimeRouter.plan()` 只读取 admitted descriptor snapshot；`adapter_for()` 先 plan，再按 exact
identity 返回 registry 中同一个 adapter instance。`CadDomainService` 只是 application-facing 的窄委托，
不拥有 Task state 或 persistence。

## 7. 唯一 authority 与信任边界

Task Kernel 始终是 Task、Revision、lease、review、Accept/Reject 和 HEAD commit 的唯一 durable
authority。runtime capability declaration 是规划证据，不是越权许可。

CAD admission 拒绝 adapter 的 exact public names `accept`、`commit`、`head`、`lease`、
`public_tool`、`reject`、`review`、`revision`、`store`、`task`；也拒绝 public CamelCase、
snake_case 或其他 identifier 中的 `accept`、`commit`、`head`、`reject`、`review` token。Generic
control conformance 同样拒绝后五种 authority token。

当前 application-owned FreeCAD parent compatibility adapter 可以把私有 `_store` 和 bounded
revision/candidate capability 用于 Kernel 已授权的 validation、checkpoint、export 和 evidence。child
Worker、未来 provider 和 Workbench client 不得接收 store/lease object、daemon credential 或
commit capability。

这些规则是 architecture/admission guard，不是 OS sandbox，也不能证明隐藏的 provider code 没有恶意
行为。adapter 与 child process 仍必须由 composition、process isolation、domain validation 和 review
共同约束。

## 8. Conformance 流程

Generic evaluator 位于
[`src/vibecad/runtime/conformance.py`](../src/vibecad/runtime/conformance.py)，CAD evaluator 位于
[`src/vibecad/interaction/cad_conformance.py`](../src/vibecad/interaction/cad_conformance.py)。

### 8.1 Generic transcript conformance

构造 `RuntimeConformanceCase(case_id, descriptor, control_class, success, cancellation, health)`，其中
success 使用 `RuntimeSuccessTranscript`，cancellation 使用 `RuntimeCancellationTranscript`。随后调用
`evaluate_runtime_conformance(cases)`，并要求 `ConformanceReport.conforms` 为 true。
`ConformanceReport.findings` 只包含
`ConformanceFinding(code, case_id, subject)`。

production evaluator 不构造 control class、不调用 provider method，也不取得 result。它静态读取普通
class MRO/namespace，并要求五方法都是 synchronous raw Python function、参数名/kind 精确、没有
default。任何 raw function 的 non-null `__signature__` 都以稳定 `control_method_signature` finding
拒绝，防止伪造 call shape；只有通过后才使用
`inspect.signature(..., follow_wrapped=False, eval_str=False)`。annotation 不求值。

### 8.2 CAD admission 与 admitted-snapshot conformance

先为每个 adapter 构造 `CadRuntimeAdmissionCase(case_id, adapter)` 并调用
`evaluate_cad_runtime_admission()`。该步骤只委托现有 registry admission 一次；authority-negative
adapter 在 descriptor/generation property read 和 hook call 前失败。

admission 通过后，由 caller 构造一个 `CadRuntimeAdapterRegistry` snapshot，再建立
`CadRuntimeConformanceCase(case_id, registry, identity, executable_request, unsupported_request,
artifacts, selector)` 并调用 `evaluate_cad_runtime_conformance()`。snapshot evaluator 可使用 canonical
router plan/`adapter_for()`，但不重新读取 provider descriptor/generation metadata，也不调用
terminate/close 或 CAD execution hooks。

CAD conformance 要求：

- executable request 得到 exact executable declared capability 和 exact adapter identity；
- unsupported request 保持 non-executable，`adapter_for()` 返回 exact
  `NonExecutableCadDecisionError`；
- artifacts 匹配 admitted runtime/profile 的 runtime/kind/media；
- selector 是匹配 runtime 的 `CadSelectorEnvelope`，而不是裸 native locator。

### 8.3 Bounded、atomic、stable reports

- 每个 evaluator 最多接受 32 个 raw cases；取得第 33 项时整批立即返回一个 case-limit finding、
  prepared cases 为空、恰好读取 33 项且不读取第 34 项。
- over-limit batch 不执行 buffered prefix semantics、CAD admission、provider metadata read 或 hook。
- findings 最多 128 个，只含 fixed code、validated/fallback case ID 和 fixed subject，按
  `(case_id, code, subject)` 排序。
- case ID 最多 64 个 ASCII contract characters；重复 ID group 产生 deterministic finding，组内 case
  不执行。
- 每个 CAD snapshot case 最多 32 个 artifacts；第 33 项使该 artifact collection 原子失败。
- iterator bounds 限制 enumeration 次数，不是 wall-clock sandbox；fixture 仍不得在一次 `next()` 中
  阻塞。

Conformance 通过只证明这些内部 contracts，不证明 native CAD quality、artifact bytes、durable
storage、public support 或产品验收。

## 9. 当前实现 limits

| 边界 | 当前硬上限或要求 |
|---|---|
| Generic contract text | 256 characters；printable、single-line；name/version/media/digest 另有 pattern |
| CAD contract text | 256 UTF-8 bytes；printable、single-line |
| JSON depth | 32 |
| JSON items per container | 1,024 |
| JSON total nodes | 8,192 |
| JSON bytes per string / cumulative | 65,536 / 1,048,576 |
| JSON safe integer | absolute value 不超过 `2**53 - 1` |
| Contract capabilities/profiles/artifacts/diagnostics/evidence 等 collection | 1,024 |
| Generic descriptors / CAD adapters | 256 / 256 |
| CAD declarations/decisions | 1,024 |
| Conformance cases/findings/case ID | 32 / 128 / 64 ASCII |
| CAD artifacts per conformance case | 32 |
| Generic budget/deadline | 正 safe integer；具体 enforcement 由 owner/integration 提供 |
| Current product admission | 30,000 ms；1 created object；262,144 result bytes |

这些是当前 internal implementation limits，不是允许通过文档覆盖的 tuning knobs。

## 10. 唯一 runnable metadata/routing example

下面示例只构造 immutable metadata、执行 registry admission、plan 和 exact routing。它不实现 generic
lifecycle 或 `CadExecutionPort`，不启动 FreeCAD，不读写项目/Revision/artifact，也不是
`examplecad` 产品支持证据。

```python
from vibecad.interaction.cad_runtime import (
    CAD_EXECUTE_PROGRAM_V1,
    CadArtifactDeclaration,
    CadArtifactProfile,
    CadArtifactRole,
    CadDomainService,
    CadNativeDecision,
    CadRuntimeAdapterRegistry,
    CadRuntimeDescriptor,
    CadRuntimeIdentity,
    CadRuntimeRouter,
)
from vibecad.runtime.contracts import RuntimeDescriptor, RuntimeIdentity

identity = CadRuntimeIdentity(
    runtime=RuntimeIdentity(
        family="cad",
        provider="examplecad",
        version="1.0",
    )
)

descriptor = CadRuntimeDescriptor(
    runtime_descriptor=RuntimeDescriptor(
        identity=identity.runtime,
        capabilities=(CAD_EXECUTE_PROGRAM_V1,),
        execution_profiles=("headless",),
    ),
    artifact_profile=CadArtifactProfile(
        runtime=identity,
        declarations=(
            CadArtifactDeclaration(
                runtime=identity,
                role=CadArtifactRole.NATIVE_MODEL,
                kind="native_model",
                media_type="application/vnd.examplecad.model",
                version=1,
            ),
        ),
    ),
)


class ExampleCadAdapter:
    def __init__(self) -> None:
        self._generation_lost = False

    @property
    def runtime_descriptor(self) -> CadRuntimeDescriptor:
        return descriptor

    @property
    def generation_lost(self) -> bool:
        return self._generation_lost

    def terminate_generation(self) -> None:
        self._generation_lost = True

    def close_generation(self) -> None:
        self._generation_lost = True


adapter = ExampleCadAdapter()
registry = CadRuntimeAdapterRegistry((adapter,))
service = CadDomainService(CadRuntimeRouter(registry))

decision = service.plan(identity, CAD_EXECUTE_PROGRAM_V1)
if type(decision) is not CadNativeDecision:
    raise RuntimeError("capability was not selected natively")
if service.adapter_for(identity, CAD_EXECUTE_PROGRAM_V1) is not adapter:
    raise RuntimeError("exact adapter identity was not preserved")
```

完整 positive/negative fixtures 见：

- [`tests/test_runtime_contracts.py`](../tests/test_runtime_contracts.py)
- [`tests/test_runtime_registry.py`](../tests/test_runtime_registry.py)
- [`tests/test_runtime_conformance.py`](../tests/test_runtime_conformance.py)
- [`tests/test_cad_runtime.py`](../tests/test_cad_runtime.py)
- [`tests/test_cad_runtime_conformance.py`](../tests/test_cad_runtime_conformance.py)
- [`tests/test_runtime_purity.py`](../tests/test_runtime_purity.py)

## 11. FreeCAD-only default composition

当前 `WorkerCadExecutionPort` 定义见
[`src/vibecad/execution/worker_port.py`](../src/vibecad/execution/worker_port.py)。
它同时保留完整 nominal `CadExecutionPort` behavior，并提供一个 immutable
`CadRuntimeDescriptor`：

- identity：`cad/freecad@1.1.0`；
- capability：`authoring.execute_program@1`；
- execution profile：`headless`；
- native artifact：`native_model` / `application/vnd.freecad.fcstd`；
- exchange artifact：`exchange_model` / `model/step`。

default factory 位于
[`src/vibecad/application/project.py`](../src/vibecad/application/project.py)。
它只构造一个 `WorkerCadExecutionPort`，只把该 FreeCAD adapter 注册到
`CadRuntimeAdapterRegistry`，通过 `CadDomainService(CadRuntimeRouter(...))` 选择
`CAD_EXECUTE_PROGRAM_V1`，验证返回的还是同一 Worker，然后交给现有 Application/Task Kernel。

该 composition 没有第二 adapter、自动 plugin discovery 或 public runtime selection。公共 MCP 面仍为
28 tools，semantic operation registry 仍为六个 operation。FreeCAD composition/real Worker evidence
见：

- [`tests/test_cad_execution_port.py`](../tests/test_cad_execution_port.py)
- [`tests/test_agent_application.py`](../tests/test_agent_application.py)
- [`tests/test_freecad_worker.py`](../tests/test_freecad_worker.py)

## 12. MR1 durable boundary 与 breakers

MR0 只把当前 fixed layout 包在 runtime-qualified FreeCAD descriptor 后面。
`RevisionRef`、`LocalRevisionStore`、Candidate directory、manifest、recovery journal 和 public artifact
delivery 仍要求 `model.FCStd` 与 `model.step`。内部 `CadArtifactProfile` 能描述另一种 native media type
不代表 durable store 能持久化它。

MR1-P00 已冻结
[`Revision durable-v2 迁移合同`](orchestrated/vibecad-durable-v2.md)，但没有实现 Revision v2 writer
或第二 CAD：

- committed v1 Revision history 永不 eager/in-place rewrite；HEAD/Task/journal/checkout 等
  operational instance 仍按既有 lifecycle 合法变化，但其 frozen v1 encoding 不能被 migration
  convenience-rewrite；
- 缺 profile 的 strict v1 record 只映射到固定 legacy FreeCAD
  `model.FCStd`/`fcstd`/`application/vnd.freecad.fcstd` +
  `model.step`/`step`/`model/step` profile；
- P02 只插入 strict v1 version-dispatch seam，并让 unknown v2/hybrid fail closed；future strict
  dual-reader 必须另行交付并保持 writer byte-exact v1，later new-write-v2 才可能获准；
- ancestry 可以是 v1 ancestor → v2 descendant；v2 激活后 downgrade writer mutation fail closed；
- P03 inventory 从 `data/` root identity 开始观察 `locks/` control namespace，再覆盖
  `projects/`、`tasks/`、`bootstrap/`、`checkouts/` 和 `artifacts/`，只能观察
  `structurally_ready`；
- `activation_ready` 只属于 future daemon-quiesced、global writer/maintenance fence 内的第二次
  full scan，并且 activation 还要求 capacity、独立验证的 backup/restore 与明确 rollback。

P03 必须复用 Application 已 pin 的 layout 和 future non-creating snapshot hook；不能调用会补建
root/children 的 `ApplicationDataLayout.open()`，也不能以 catalog/quota/resource lease 包装扫描，
因为首次 lease acquisition 可创建 persistent lock file。lock entry 在 release 后仍保留，所以
presence 不等于 active lease；quiescence 只能由 future global fence 证明。若现有 Task snapshot
缺少这个 mutation-negative property，`workflow/store.py` 需要另一个 exact allowlist/approval，
不能由 migration module 绕过 store authority。

Managed checkout open/tombstone 当前具有自己 record family 的 v1/v2 dual-reader 和 current-v2
writer。这个事实不能推广成 Revision durable-v2 已实现，也不能允许 adapter 根据 checkout version
推测 Revision profile。P00..P03 不改变 `RevisionRef.to_mapping()`、28-tool MCP surface、六 operation、
`SelectorV1` 或 artifact URI。

以下任一需求都是 breaker，必须停止当前 adapter packet：

- 修改 `RevisionRef`、Candidate/store/manifest/recovery schema 或写入第二 native format：转入 MR1；
- 修改 public MCP schema、28-tool surface、六 operation 或 `SelectorV1` wire contract：需要独立
  versioned public-protocol approval；
- 把 adapter/provider 暴露为 Task/lease/review/commit/HEAD authority：拒绝该设计；
- 把 fake/conforming adapter 加入 default composition 或宣称支持第二 CAD：需要真实 adapter、native
  engine evidence、durable migration 与产品 acceptance；
- 需要 G1 UI、host verification、tag、release、external credentials/spend：进入各自单独批准的
  campaign。

MR0 conformance-ready foundation 降低未来 adapter 的合同风险，但不会关闭
`MRG1-RES-01A`、`MRG1-RES-03..05`、`MRG1-RES-06A`、`MRG1-RES-07..10`。
