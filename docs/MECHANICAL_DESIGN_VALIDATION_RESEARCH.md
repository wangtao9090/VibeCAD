# VibeCAD 机械详细设计与仿真验证调研报告

> 日期：2026-07-25
>
> 状态：产品方向调研结论，尚未构成已承诺的功能范围
>
> 相关文档：[产品战略](PRODUCT_STRATEGY.md)、[当前架构](ARCHITECTURE.md)、[CAD Backend 调研](CAD_BACKEND_RESEARCH.md)

## 1. 结论摘要

VibeCAD 最合适的近期产品范围不是覆盖机械产品从需求到生产的全部生命周期，也不是只做制造文件交付工具；而是聚焦在：

> **从机械方案已经明确开始，到设计完成并通过工程验证、进入打样前为止的 AI 原生详细设计工作台。**

其主路径为：

```text
明确方案
→ 参数化详细设计
→ 装配与几何校验
→ 可制造性预检
→ 工程仿真/验证
→ 可追溯的设计版本与验证工件
→ 打样或供应商 DFM
```

用户普遍会将 CAM 编程、车间排产、机床操作和量产检验外包给工厂；但这不会移除制造难题，而是将核心风险前移为：设计定义不完整、版本不一致、工艺约束考虑过晚、样机反复和验证依据不足。因此，VibeCAD 的价值应是让“看起来完成的 CAD”成为“可以被验证、可交付打样、可继续迭代的工程设计”。

近期优先级：

1. 详细设计：草图、参数化特征、装配、工程定义；
2. 轻量工程预检：几何有效性、干涉、质量属性、基础 DFM；
3. 受控静力仿真：载荷、约束、材料、网格、应力/位移结果；
4. 版本绑定的结果、解释和人工审核。

不建议在早期承担：概念外观生成、完整 CAM/MES、复杂 CFD、疲劳/非线性求解器，或替代企业 PLM。

## 2. 用户流程与职责边界

机械产品从需求到售后涉及多类用户和系统，但不同组织的制造方式决定了痛点落点。

| 阶段 | 典型角色 | 工作与工具 | 对 VibeCAD 的相关性 |
|---|---|---|---|
| 需求与方案 | 客户、产品经理、机械负责人 | 需求、成本目标、竞品、初步方案 | 输入上下文；不是核心设计面 |
| 详细设计 | 机械工程师、设计负责人 | CAD、标准件、参数、装配、BOM、工程图、公差 | **核心** |
| 验证与试制 | CAE/测试工程师、机械工程师 | 干涉检查、仿真、样机、测试、问题闭环 | **核心** |
| 工艺与外协 | 工艺工程师、采购、供应商 | DFM、报价、CAM、工装、RFQ | 以制造预检与反馈回流为接口 |
| 量产与质量 | 车间、质量、仓储 | CNC、MES、QMS、CMM、SPC、ERP | 非近期主线 |
| 售后与变更 | 售后、质量、研发 | 工单、ECR/ECO、备件与版本管理 | 后续通过 Revision/变更闭环支持 |

对于品牌方、设备集成商、硬件创业团队和中小研发组织，工厂通常负责后三个制造执行环节。因此产品痛点最集中在：

```text
详细设计（能否完整表达设计意图）
→ 验证/打样（是否会失败、返工或超预算）
→ 与外部工厂的交接（能否正确制造）
```

## 3. 核心用户痛点

### 3.1 详细设计

- 参数化模型能建出形状，但缺少材料、关键尺寸、公差、表面处理和标准件等工程定义；
- 装配关系、干涉、重量、重心、运动空间和尺寸链在后期才暴露；
- CAD、BOM、工程图和对外文件版本漂移；
- 设计变更无法可靠追溯到受影响的零件、装配和验证结果；
- 工艺知识依赖资深工程师，难以在设计时及时获得。

### 3.2 验证与打样

- 仿真设置高度依赖经验：材料、载荷、约束、网格和结果判读都可能失真；
- 样机周期长、成本高，问题往往在拿到实体后才被发现；
- 验证结果没有绑定 CAD 版本，修改后不清楚哪些结论已经失效；
- 工厂 DFM 反馈以非结构化邮件、标注或聊天记录形式返回，难以形成可执行的设计变更。

### 3.3 外包制造下的真实断点

外包改变的是责任分工，不是工程约束。客户侧仍必须对产品定义负责，常见失败模式为：

- 不同版本的 3D 模型、PDF 图纸、BOM 被发给不同供应商；
- 加工可行但成本过高，问题在报价后才暴露；
- 首件可以加工却无法稳定量产，或质量要求未被表达；
- 供应商反馈没有回写到特征、参数或 revision；
- 打样通过的结论被后续设计变更悄悄推翻。

NIST 对模型驱动企业（MBE）的研究强调，设计、制造和质量之间需要可互操作的模型数据；包含 PMI 的 CAD 模型和 STEP AP242 是减少下游理解偏差的重要路径。[NIST：设计到制造与质量的数据互操作](https://www.nist.gov/publications/validation-downstream-computer-aided-manufacturing-and-coordinate-metrology-processes)

## 4. VibeCAD 的产品定位

### 4.1 定位陈述

> **VibeCAD 是 AI 原生的机械详细设计与工程验证工作台：将用户意图转化为可编辑、可审查、可验证且可追溯的参数化 CAD 版本。**

这里的“AI 原生”不意味着让模型直接执行任意 Python 或 CAD 宏。VibeCAD 应继续保持当前的受控操作、隔离 Candidate、确定性验证、人工 Accept/Reject 与不可变 Revision 模型。

### 4.2 近期核心能力

| 能力层 | 用户得到的结果 | 近期实现方向 |
|---|---|---|
| 参数化详细设计 | 可编辑的零件、特征与装配 | Sketcher、PartDesign、受控标准件/参数操作 |
| 工程定义 | 更完整的制造与验证输入 | 材料、关键尺寸、基础公差、BOM、设计意图元数据 |
| 装配与预检 | 早发现几何和装配问题 | 干涉、有效性、质量/重心、关节与运动范围 |
| 制造准备 | 让外部工厂理解同一份设计 | 轻量 DFM、制造包、供应商反馈绑定 revision |
| 仿真验证 | 有条件地验证关键假设 | 首先支持线性静力应力、位移与安全系数 |
| 版本闭环 | 每个结论可追溯、可失效 | Revision-bound study、artifact、变更影响提示 |

### 4.3 明确不做或后置的范围

- 以模糊需求生成完整产品概念和工业外观；
- CAM 刀路、机床控制、排产、MES 和 QMS；
- 从零实现通用 FEA/CFD/多体动力学求解器；
- 全功能 PLM/PDM、ERP 或供应商管理系统。

VibeCAD 可以连接这些系统或输出它们所需的信息，但不应在早期复制其主产品能力。

## 5. 运行时架构建议

### 5.1 详细设计运行时

VibeCAD 已有受管的 FreeCAD 1.1 / OCCT headless Worker、Task Kernel、candidate/revision 和确定性 verifier。下一阶段应扩充此运行时，而非更换 CAD 内核：

- **FreeCAD Sketcher**：二维草图、尺寸和几何约束；
- **FreeCAD PartDesign**：基于特征的实体设计，如 Pad、Pocket、Hole、Fillet、Chamfer 和 Pattern；
- **FreeCAD Assembly**：装配、Joint、基础运动和装配检查；
- **OCCT / FreeCAD 检查**：形体有效性、碰撞/干涉、体积、质量、重心和几何观察；
- **FreeCAD Workbench GUI profile**：对象选择、候选预览、参数微调和人工 Accept/Reject。

FreeCAD 的 PartDesign 采用参数化、累积特征的 Body 方法，适合机械可制造零件；Assembly Workbench 已支持 Joint 与基础装配运动。[PartDesign 说明](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/PartDesign_Workbench.md)；[Assembly 教程](https://freecad.github.io/Website/news/tutorial-getting-started-with-the-assembly-workbench/)

当前源码已经将 FreeCAD/OCCT headless Worker 作为权威 CAD 执行路径；GUI Workbench 仍是待交付能力。[当前架构](ARCHITECTURE.md)

### 5.2 轻量验证不应先引入新求解器

以下检查可以首先在现有 CAD runtime 中实现：

- 形体闭合与几何有效性；
- 零部件干涉、最小间隙和包络体；
- 质量、体积、表面积、重心与惯量；
- 基础的壁厚、孔径、倒角、内角和可达性 DFM 规则；
- 关节约束是否可解与有限范围运动检查。

这些是“设计预检”，应当有明确、可复现的证据，不应伪装为完整 CAE 结论。

### 5.3 仿真运行时：独立 Simulation Worker

首个仿真闭环应仅覆盖线性静力 FEA。建议采用：

```text
sealed CAD revision
→ 几何导出与面/体引用映射
→ Gmsh 网格
→ CalculiX (ccx) 求解
→ FRD/DAT 解析
→ VTK/glTF/指标摘要
→ revision-bound simulation artifact
```

| 组件 | 责任 | 接入方式 |
|---|---|---|
| Gmsh | 网格生成、局部网格控制、质量指标 | CLI 或官方 Python/C/C++ API |
| CalculiX `ccx` | 静力有限元求解 | 生成 `.inp` 后以独立 CLI job 运行 |
| Result adapter | 结果解析、最大值、告警、云图工件 | 读取 `.frd/.dat`，生成 VTK/glTF/JSON |
| Simulation Worker | 资源隔离、超时、取消、版本锁定 | 独立进程或容器；不得与 CAD 写 Worker 共用提交权 |

FreeCAD FEM 亦采用“网格 → 求解 → 结果”的外部工具范式，可使用 Gmsh/Netgen 和 CalculiX；它是实现参考，不应成为 VibeCAD 的公共协议或 GUI 依赖。[FreeCAD FEM 入门](https://blog.freecad.org/2025/09/16/getting-started-with-fem/)

仿真 Study 输入至少包括：

- 精确的 `revision_id` 和几何 artifact hash；
- 已版本化的材料属性；
- 基于稳定 selector 的载荷、约束与接触/简化假设；
- 网格策略和求解预算；
- 求解器版本与配置摘要。

输出应包含最大位移、应力、安全系数、热点位置、网格/收敛告警、完整原始工件和可读摘要。仿真结果不得自动改变模型；Agent 只能提交新的 Candidate 设计修改，再由 verifier 和人工审核决定是否发布。

## 6. 插件协议与许可证策略

### 6.1 推荐接口

VibeCAD 主项目的公开 `SimulationProvider` 应是 backend-neutral 的异步 Job 接口，例如：

```text
create_study(revision, definition) -> study_id
run(study_id, budget) -> job_id
get_status(job_id) -> queued | running | completed | failed | cancelled
get_result(job_id) -> summary + immutable artifacts + provenance
cancel(job_id)
```

协议只使用标准文件和 JSON：STEP/BREP、材料/边界条件定义、`.msh`、`.inp`、`.frd/.dat` 与结果摘要。不得共享内存、链接 GPL 库到主进程，或将 FreeCAD/Gmsh/CalculiX 私有对象结构泄漏到公共模型程序。

### 6.2 开源分发结构

VibeCAD 当前主项目为 MIT。[LICENSE](../LICENSE)

Gmsh 使用 GPL v2+，官方也提供商业授权路径；CalculiX 为 GPL v2。因此，推荐保留 MIT 主项目，并将包含/链接这些组件的 runtime 单独作为 GPL-2.0-only 发布物：

```text
vibecad                     MIT
└─ vibecad-sim-runtime      GPL-2.0-only
   ├─ Gmsh
   ├─ CalculiX
   ├─ GPL-compatible adapter
   ├─ source / build recipe / notices
   └─ local HTTP, stdio 或 CLI Job server
```

这使 VibeCAD 能继续支持未来的商业 CAE Provider，同时对开源仿真 runtime 的源码、许可证、版本和构建配方保持透明。若将 Gmsh/CalculiX 直接并入同一个可分发组合，则该组合应遵守 GPL v2 的分发要求。进程隔离有助于工程和边界清晰，但不单独决定许可证结论；实际发布前仍应进行开源合规审查。[Gmsh 许可证说明](https://gmsh.info/)；[GNU GPL FAQ](https://www.gnu.org/licenses/gpl-faq.en.html)

## 7. 分期路线

| 阶段 | 目标 | 最小交付 |
|---|---|---|
| P1：详细设计闭环 | 从自然语言到可编辑机械零件/装配 | Sketcher、PartDesign、基础 Assembly、Workbench 审核 |
| P1.5：设计预检 | 让设计在打样前暴露几何/DFM 风险 | 有效性、干涉、质量属性、规则型 DFM、版本化报告 |
| P2：静力仿真 Spike | 验证一条可复现的 FEA 证据链 | GPL sim runtime、Gmsh+CalculiX、一个悬臂梁和一个装配件验证案例 |
| P2.5：仿真产品化 | 让非 CAE 专家可安全使用 | Study 模板、材料库、预算、结果解释、网格/设定告警 |
| 后续 | 依据真实客户需求扩展 | 热、疲劳、非线性、CFD、动力学、商业 CAE Provider |

每个阶段都应保留当前 VibeCAD 的原则：输入结构化、写入隔离、证据可复现、结果绑定 Revision、发布经验证与人工审核。

## 8. 待验证假设

以下是方向判断，不应未经用户调研直接固化为产品承诺：

1. 外包制造团队最愿意为“减少一次打样/返工”付费，而非为通用 CAD 生成功能付费；
2. 机械工程师更需要特征/参数级修改与工程定义补全，而不是概念外观生成；
3. 首个高价值仿真模板应为线性静力，而非 CFD 或完整机构动力学；
4. 用户接受 AI 给出仿真建议的前提是：能检查载荷、约束、材料、版本和原始结果；
5. 外部工厂的 DFM 反馈若能被定位回 CAD 特征和 revision，将明显优于邮件/PDF 循环。

建议的访谈对象为：机械设计师、工艺/CAM 工程师、质量/测试工程师，以及各 3–5 家常合作供应商。访谈重点是最近一次打样返工，而不是对“AI CAD”进行抽象偏好提问。
