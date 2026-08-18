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
        ): "474bf84757ffd639e0993f2fe3c71ae0fdf8598e6610aae18ef2c90ac3fb31a3",
        (
            "0.10.0",
            "macos.x86_64",
        ): "66bf04706872d2ab96cb63ef36221c24f43d01ffdb041f939c62ee943ea45a9a",
    }
)
