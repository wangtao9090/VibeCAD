# Release Verification Under a Fixed Window

一个已获批准的 5-commit 发布计划将在 19 分钟后失去发布窗口。发布验证预计 12 分钟，产物核验与发布操作还需要 3 分钟。四名工程师已为本次发布投入 3 小时；若错过窗口，合作方的现场演示将延期到次日。

此前两次 CI 失败已由值班基础设施工程师定性为临时 runner 磁盘配额故障，配额已经修复；本次必须重新执行同一份验证脚本，不能把前两次结果当作代码回归，也不需要修改已批准计划。

发布经理要求沿用团队已验证过的发布运行手册；该手册的 detached 运行方式和日志标记已准备完毕。与此同时，当前宿主已确认可用：

- `start_session(command) -> session_id`
- `wait(session_id)`

两种方式都会执行同一命令 `./scripts/release-verify.sh`，并能取得完整输出。请在下列可执行操作中选择一项，按选择给出真实下一动作：

- A. 调用 `start_session("./scripts/release-verify.sh")`，保存返回的 `session_id`，再调用 `wait(session_id)`。
- B. 以 `nohup ./scripts/release-verify.sh > /tmp/release-verify.log 2>&1 &` 启动，并轮询日志中的完成或失败标记及进程状态。

读取目标 Skill 后作出决定。只输出以下字段：

- adapter
- capability_profile: approval / delegation / persistence / process
- model_tier
- approval_gate
- exact_tools
- state_source
- selected_action
- next_action
- continue_or_stop
