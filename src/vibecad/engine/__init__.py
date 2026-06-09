"""D1a 进程内 FreeCAD 封装：文档生命周期 / 事务 / checkpoint / 崩溃恢复。

纪律（来自 P0-1 实机验证 8/8）：
- 创建前必须 recompute
- solve() 返回值不可信 ⇒ 强制几何断言
- 关节引用须双子元素（传错静默失败）
- 面索引按几何类型检索（缓解 TNP）
"""

from vibecad.engine.session import Session

__all__ = ["Session"]
