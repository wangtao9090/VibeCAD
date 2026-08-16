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
            "0.10.0",
            "macos.arm64",
        ): "cb54a6f3e7d76784c64cd200b770dd3066498ecf4f64f34df800a932604fb73f",
        (
            "0.10.0",
            "macos.x86_64",
        ): "0135830059981607ba5f87feda2d045ce0da62c97252ef5af4436a678466b4bb",
    }
)
