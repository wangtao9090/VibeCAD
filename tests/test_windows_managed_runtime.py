from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.slow
def test_real_managed_freecad_round_trips_headless_model(tmp_path: Path) -> None:
    """W1 proves the engine before W2 adds the Windows Worker/store boundary."""

    if sys.platform != "win32":
        pytest.skip("the Windows managed-runtime gate runs only on Windows")
    python_raw = os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON")
    if not python_raw:
        pytest.skip("managed FreeCAD Python was not requested")
    python = Path(python_raw)
    if not python.is_file():
        pytest.fail("managed FreeCAD Python is unavailable")

    from vibecad.runtime import paths as runtime_paths
    from vibecad.runtime.status import capture_runtime_generation_evidence

    evidence = capture_runtime_generation_evidence(runtime_paths.active_runtime_prefix())
    assert python.resolve() == evidence.python.resolve()

    model = tmp_path / "windows-w1.FCStd"
    step = tmp_path / "windows-w1.step"
    code = """
import json
import os
import sys
from pathlib import Path
from vibecad.freecad_env import prepare_freecad_import
prepare_freecad_import()
activation = {
    "conda_prefix": os.environ.get("CONDA_PREFIX"),
    "path_prefix": os.environ.get("PATH", "").split(os.pathsep)[:6],
    "proj_data": os.environ.get("PROJ_DATA"),
    "proj_network": os.environ.get("PROJ_NETWORK"),
    "ssl_cert_dir": os.environ.get("SSL_CERT_DIR"),
    "ssl_cert_file": os.environ.get("SSL_CERT_FILE"),
    "xml_catalog_files": os.environ.get("XML_CATALOG_FILES"),
}
import FreeCAD
import Part
model = Path(sys.argv[1])
step = Path(sys.argv[2])
document = FreeCAD.newDocument("WindowsW1")
feature = document.addObject("PartDesign::Feature", "Box")
feature.Shape = Part.makeBox(20, 10, 5)
document.recompute()
assert feature.Shape.isValid() and len(feature.Shape.Solids) == 1
assert abs(float(feature.Shape.Volume) - 1000.0) < 1e-7
document.saveAs(str(model))
Part.export([feature], str(step))
FreeCAD.closeDocument(document.Name)
reopened = FreeCAD.openDocument(str(model))
reopened.recompute()
loaded = reopened.getObject("Box")
assert loaded is not None and loaded.Shape.isValid()
assert len(loaded.Shape.Solids) == 1
assert abs(float(loaded.Shape.Volume) - 1000.0) < 1e-7
payload = {
    "activation": activation,
    "freecad": FreeCAD.Version()[:3],
    "model_bytes": model.stat().st_size,
    "step_bytes": step.stat().st_size,
    "volume_mm3": float(loaded.Shape.Volume),
}
FreeCAD.closeDocument(reopened.Name)
print("VIBECAD_WINDOWS_W1=" + json.dumps(payload, sort_keys=True))
"""
    completed = subprocess.run(
        [str(python), "-I", "-B", "-c", code, str(model), str(step)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout[-4000:]
    marker = "VIBECAD_WINDOWS_W1="
    payload_line = next(
        (line for line in completed.stdout.splitlines() if line.startswith(marker)),
        None,
    )
    assert payload_line is not None, completed.stdout[-4000:]
    payload = json.loads(payload_line.removeprefix(marker))
    prefix = python.parent
    assert payload["activation"] == {
        "conda_prefix": str(prefix),
        "path_prefix": [
            str(prefix),
            str(prefix / "Library" / "mingw-w64" / "bin"),
            str(prefix / "Library" / "usr" / "bin"),
            str(prefix / "Library" / "bin"),
            str(prefix / "Scripts"),
            str(prefix / "bin"),
        ],
        "proj_data": str(prefix / "Library" / "share" / "proj"),
        "proj_network": (
            "OFF"
            if (prefix / "Library" / "share" / "proj" / "copyright_and_licenses.csv").is_file()
            else "ON"
        ),
        "ssl_cert_dir": str(prefix / "Library" / "ssl" / "certs"),
        "ssl_cert_file": str(prefix / "Library" / "ssl" / "cacert.pem"),
        "xml_catalog_files": (prefix / "etc" / "xml" / "catalog").as_uri(),
    }
    assert payload["freecad"] == ["1", "1", "0"]
    assert payload["volume_mm3"] == pytest.approx(1000.0)
    assert payload["model_bytes"] == model.stat().st_size > 0
    assert payload["step_bytes"] == step.stat().st_size > 0


@pytest.mark.slow
def test_real_managed_worker_starts_inside_windows_job_and_stops_cleanly() -> None:
    if sys.platform != "win32":
        pytest.skip("the Windows managed-runtime gate runs only on Windows")
    python_raw = os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON")
    if not python_raw:
        pytest.skip("managed FreeCAD Python was not requested")

    from vibecad.worker.generation import WorkerGenerationState
    from vibecad.worker.proxy import FreeCadWorker

    source_root = Path(__file__).resolve().parents[1] / "src"
    worker = FreeCadWorker.start(
        python=Path(python_raw),
        source_root=source_root,
    )
    try:
        assert worker.state is WorkerGenerationState.READY
        assert worker.pid > 0
        assert worker._process.launch_primitive == "windows_job"  # noqa: SLF001
    finally:
        worker.terminate()
    assert worker.state is WorkerGenerationState.DEAD
