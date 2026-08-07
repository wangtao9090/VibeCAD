# Privacy Policy / 隐私政策

**VibeCAD** — AI-native conversational CAD (an open-source MCP connector for FreeCAD)

Last updated: 2026-08-04

---

## English

### Summary

The VibeCAD CAD backend runs on your own machine. It has no telemetry, account service, or VibeCAD-operated cloud storage. The default visual path uses the multimodal model already provided by your host Agent. VibeCAD 0.9.0 also contains an optional, non-default OpenAI visual transport; when you explicitly configure and select it, bounded metadata-free PNG derivatives of the selected images are sent to that provider under your account and its privacy terms.

### Local processing only

Geometry, models, durable project/revision/draft data, and published FCStd/STEP artifacts are created and stored locally on your device. To perform a requested CAD operation, MCP tool inputs and results pass between VibeCAD and your selected MCP client; public artifact results contain opaque `vibecad://` resource URIs rather than local filesystem paths, and the client can retrieve the corresponding FCStd/STEP bytes through MCP resource reads. The client may send conversation or tool content to its model provider, so review that client's privacy settings and provider terms.

### One-time runtime download

On first use, VibeCAD downloads the FreeCAD runtime (approximately 2–3 GB) from official open-source mirrors (micromamba / conda-forge). This is a plain software download: it does not carry or transmit any personal data.

### No telemetry

VibeCAD collects no telemetry, no usage statistics, and no account information.

### Network access

VibeCAD's normal direct outbound access covers software installation: the runtime download described above and open-source dependencies from PyPI. If the optional OpenAI visual transport is explicitly configured and selected, it additionally sends bounded image derivatives and the strict reconstruction request to that provider; originals remain in VibeCAD's local sealed store, while deletion of the local copy cannot retract a provider-retained copy. This statement does not cover network processing performed independently by your MCP client or its model provider. VibeCAD does not implement MCP Sampling or operate a cloud service or telemetry channel; its authenticated local daemon and managed FreeCAD Worker remain on your device.

### Contact

- Email: wangtao9090@gmail.com
- GitHub: <https://github.com/wangtao9090/VibeCAD>

---

## 中文

### 概要

VibeCAD 的 CAD 后端在你自己的设备上运行。它没有遥测、账号服务或 VibeCAD 运营的云存储。默认视觉路径使用宿主 Agent 已有的多模态模型。VibeCAD 0.9.0 还包含可选、非默认的 OpenAI 视觉 transport；只有在用户显式配置并选择它时，才会把选中图片的有界、去元数据 PNG 派生图发送到该账号下的 Provider，并受其隐私条款约束。

### 仅本地处理

几何数据、模型、持久化的项目/Revision/draft 数据，以及已发布的 FCStd/STEP 产物都在你的设备上创建和存储。为执行用户请求的 CAD 操作，MCP 工具输入和结果会在 VibeCAD 与所选 MCP 客户端之间传递；公开产物结果提供不透明的 `vibecad://` resource URI，而不是本地文件系统路径，客户端可通过 MCP resource read 取得对应的 FCStd/STEP 字节。客户端可能把对话或工具内容发送给其模型供应商，请同时检查客户端的隐私设置和供应商条款。

### 一次性运行时下载

首次使用时，VibeCAD 会从官方开源镜像（micromamba / conda-forge）下载 FreeCAD 运行时（约 2–3 GB）。这是一次普通的软件下载，不携带、不传输任何个人数据。

### 无遥测

VibeCAD 不收集遥测数据、不收集使用统计、不收集账号信息。

### 网络访问

VibeCAD 的常规主动网络访问用于软件安装：上述运行时下载，以及从 PyPI 获取开源依赖。若显式配置并选择可选 OpenAI 视觉 transport，它还会向该 Provider 发送有界图片派生图和严格重建请求；原图仍在本地 sealed store，本地删除不能撤回 Provider 已保留的副本。此说明不涵盖 MCP 客户端或其模型供应商独立进行的网络处理。VibeCAD 不实现 MCP Sampling，也不运营云服务或遥测通道；认证本地 daemon 与受管 FreeCAD Worker 都留在用户设备上。

### 联系方式

- 邮箱：wangtao9090@gmail.com
- GitHub：<https://github.com/wangtao9090/VibeCAD>
