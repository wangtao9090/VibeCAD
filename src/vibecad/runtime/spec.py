"""受管 FreeCAD 运行时的版本契约。纯 stdlib，可在 bootstrap 阶段安全导入。"""

from __future__ import annotations

from vibecad import __version__

VIBECAD_VERSION = __version__
RECEIPT_SCHEMA = 1
SERVER_PACKAGE_EPOCH = 3
MCP_VERSION = "1.27.2"
PUBLIC_SURFACE_SHA256 = "081b4175baa8081550cf617f694dfd46f0a711f726bba2db00b66e94252a7a75"
PYTHON_VERSION = (3, 12)
FREECAD_VERSION = (1, 1, 0)
PYTHON_PIN = f"python={PYTHON_VERSION[0]}.{PYTHON_VERSION[1]}"
FREECAD_PIN = "freecad=" + ".".join(map(str, FREECAD_VERSION))
MANAGED_KIND = "managed"
EXTERNAL_KIND = "external"


def expected_receipt(*, external: bool = False) -> dict[str, int | str]:
    """当前 bootstrap 愿意交棒的精确运行时凭据。

    两类 env 都绑定私有 server package identity；托管 env 还受 Python/FreeCAD
    pin 约束，外部 env 的实际可用性由 installer 子进程 import 验证负责。
    """
    receipt: dict[str, int | str] = {
        "schema": RECEIPT_SCHEMA,
        "runtime_kind": EXTERNAL_KIND if external else MANAGED_KIND,
        "vibecad_version": VIBECAD_VERSION,
        "server_package_epoch": SERVER_PACKAGE_EPOCH,
        "mcp_version": MCP_VERSION,
        "public_surface_sha256": PUBLIC_SURFACE_SHA256,
    }
    if not external:
        receipt.update({"python_pin": PYTHON_PIN, "freecad_pin": FREECAD_PIN})
    return receipt
