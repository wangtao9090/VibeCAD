from __future__ import annotations

from types import ModuleType


class FakeWorkbench:
    pass


class FakeFreeCADGui(ModuleType):
    def __init__(self, *, fail_first_add: bool = False) -> None:
        super().__init__("FreeCADGui")
        self.added_workbenches: list[object] = []
        self.add_attempts = 0
        self._fail_first_add = fail_first_add

    def addWorkbench(self, workbench: object) -> None:
        self.add_attempts += 1
        if self._fail_first_add and self.add_attempts == 1:
            raise RuntimeError("synthetic addWorkbench failure")
        self.added_workbenches.append(workbench)


def make_fake_freecad_gui(*, fail_first_add: bool = False) -> FakeFreeCADGui:
    return FakeFreeCADGui(fail_first_add=fail_first_add)
