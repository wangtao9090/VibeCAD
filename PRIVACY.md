# Privacy Policy / 隐私政策

**VibeCAD** — AI-native conversational CAD (an open-source MCP connector for FreeCAD)

Last updated: 2026-07-22

---

## English

### Summary

The VibeCAD CAD backend runs on your own machine. It has no telemetry, account service, or VibeCAD-operated cloud storage, and it does not independently upload your design files. VibeCAD communicates tool requests and results to the MCP client you chose; that client and any model provider it uses are governed by their own configuration and privacy terms.

### Local processing only

Geometry, models, durable project/revision/draft data, and published FCStd/STEP artifacts are created and stored locally on your device. To perform a requested CAD operation, MCP tool inputs and results pass between VibeCAD and your selected MCP client; public artifact results contain opaque `vibecad://` resource URIs rather than local filesystem paths, and the client can retrieve the corresponding FCStd/STEP bytes through MCP resource reads. The client may send conversation or tool content to its model provider, so review that client's privacy settings and provider terms.

### One-time runtime download

On first use, VibeCAD downloads the FreeCAD runtime (approximately 2–3 GB) from official open-source mirrors (micromamba / conda-forge). This is a plain software download: it does not carry or transmit any personal data.

### No telemetry

VibeCAD collects no telemetry, no usage statistics, and no account information.

### Network access

VibeCAD's own direct outbound network access is limited to software installation: the runtime download described above and fetching VibeCAD's open-source dependencies from PyPI at install/update time. This statement does not cover network processing performed by your MCP client or its model provider. VibeCAD 0.5.0 does not call a model provider directly and does not implement MCP Sampling or BYOK model access.

### Contact

- Email: wangtao9090@gmail.com
- GitHub: <https://github.com/wangtao9090/VibeCAD>

---

## 中文

### 概要

VibeCAD 的 CAD 后端在你自己的设备上运行。它没有遥测、账号服务或 VibeCAD 运营的云存储，也不会自行上传你的设计文件。VibeCAD 会与用户选择的 MCP 客户端交换工具请求和结果；该客户端及其使用的模型供应商受各自配置和隐私条款约束。

### 仅本地处理

几何数据、模型、持久化的项目/Revision/draft 数据，以及已发布的 FCStd/STEP 产物都在你的设备上创建和存储。为执行用户请求的 CAD 操作，MCP 工具输入和结果会在 VibeCAD 与所选 MCP 客户端之间传递；公开产物结果提供不透明的 `vibecad://` resource URI，而不是本地文件系统路径，客户端可通过 MCP resource read 取得对应的 FCStd/STEP 字节。客户端可能把对话或工具内容发送给其模型供应商，请同时检查客户端的隐私设置和供应商条款。

### 一次性运行时下载

首次使用时，VibeCAD 会从官方开源镜像（micromamba / conda-forge）下载 FreeCAD 运行时（约 2–3 GB）。这是一次普通的软件下载，不携带、不传输任何个人数据。

### 无遥测

VibeCAD 不收集遥测数据、不收集使用统计、不收集账号信息。

### 网络访问

VibeCAD 自身主动发起的外部网络访问仅限于软件安装：上述运行时下载，以及安装/更新时从 PyPI 获取 VibeCAD 的开源依赖。此说明不涵盖 MCP 客户端或其模型供应商进行的网络处理。VibeCAD 0.5.0 不直接调用模型供应商，也尚未实现 MCP Sampling 或 BYOK 模型接入。

### 联系方式

- 邮箱：wangtao9090@gmail.com
- GitHub：<https://github.com/wangtao9090/VibeCAD>
