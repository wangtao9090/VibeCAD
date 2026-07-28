import FreeCADGui


class VibeCADWorkbench(Workbench):  # noqa: F821
    MenuText = "VibeCAD"
    ToolTip = "VibeCAD thin client"

    def Initialize(self) -> None:
        return None

    def Activated(self) -> None:
        host = __import__(
            "vibecad_workbench.host",
            fromlist=("activate_workbench",),
        )
        host.activate_workbench()

    def Deactivated(self) -> None:
        host = __import__(
            "vibecad_workbench.host",
            fromlist=("deactivate_workbench",),
        )
        host.deactivate_workbench()

    def GetClassName(self) -> str:
        return "Gui::PythonWorkbench"


if getattr(FreeCADGui, "_vibecad_workbench_instance", None) is None:
    workbench = VibeCADWorkbench()
    FreeCADGui.addWorkbench(workbench)
    FreeCADGui._vibecad_workbench_instance = workbench
