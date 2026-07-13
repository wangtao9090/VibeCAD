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
