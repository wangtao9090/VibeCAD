"""Generated platform pins for packaged reviewed FreeCAD attestations.

The release generator replaces this mapping together with the canonical JSON
resource for its trusted current platform.  A key is ``(release_version,
platform_id)``; the generator preserves sibling-platform keys.  Keeping an
empty mapping in an un-attested source checkout is intentional: consumers fail
closed and cannot manufacture VERIFIED coverage.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

PACKAGED_FREECAD_REVIEWED_RELEASE_ATTESTATION_SHA256_BY_RELEASE_PLATFORM: Final = MappingProxyType(
    {
        (
            "0.10.1",
            "macos.arm64",
        ): "750fb74da7b8ba94e531b2b7f3f89bd0dbe7a0f2bac0183686b0880432d1a11d",
        (
            "0.10.1",
            "macos.x86_64",
        ): "9ce19f5b4b27ea9c2ecc7f0e4796da600ca684ec977588f644d9a0616d136371",
        (
            "0.10.1",
            "windows.x86_64",
        ): "94a1082b346b9cd098eaf16a1ff7c1b47dbf1e8ff4ac32f703f4eb2ec38e204a",
    }
)
