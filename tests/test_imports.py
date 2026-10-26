"""Smoke test: every module in src/ and scripts/ imports with the locked dependencies.

Catches missing dependencies, import errors and incompatible dependency updates
before they reach the pipeline workflow.
"""
import importlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
SCRIPTS_DIR = ROOT / "scripts"

MODULES = sorted(
    ".".join(path.relative_to(SRC_DIR).with_suffix("").parts)
    for path in SRC_DIR.rglob("*.py")
    if path.stem != "__init__"
)
SCRIPTS = sorted(SCRIPTS_DIR.glob("*.py")) if SCRIPTS_DIR.exists() else []


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module: str) -> None:
    """Imports a module from src/, which is on the pytest path."""
    importlib.import_module(module)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_script_imports(script: Path) -> None:
    """Loads a script from scripts/ without running its main block."""
    spec = importlib.util.spec_from_file_location(f"script_{script.stem}", script)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
