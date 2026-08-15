"""Generated pins for packaged reviewed FreeCAD attestations.

The release generator replaces this mapping together with the canonical JSON
resource.  Keeping an empty mapping in an un-attested source checkout is
intentional: consumers fail closed and cannot manufacture VERIFIED coverage.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

PACKAGED_FREECAD_REVIEWED_RELEASE_ATTESTATION_SHA256_BY_RELEASE: Final = MappingProxyType(
    {
        "0.10.0": "0135830059981607ba5f87feda2d045ce0da62c97252ef5af4436a678466b4bb",
    }
)
