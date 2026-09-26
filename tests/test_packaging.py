"""Does the thing we ship contain the thing we wrote?

This exists because it didn't, once. pyproject listed only the two modules the
project started with, so the built wheel shipped server.py and executor.py and
silently dropped the three modules server.py imports on the first line. It built
fine, uploaded fine, and would have failed on first run for anyone who installed
it. Nothing in the source tree was wrong — only the manifest.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _declared_modules() -> set[str]:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return set(cfg["tool"]["setuptools"]["py-modules"])


# run.py is a standalone script you invoke directly, not something the package
# imports. It is intentionally absent from py-modules; everything else must be there.
STANDALONE = {"run"}


def _source_modules() -> set[str]:
    """Top-level .py files that belong in the importable package."""
    return {p.stem for p in ROOT.glob("*.py")
            if not p.stem.startswith("_") and p.stem not in STANDALONE}


def test_every_source_module_is_declared():
    missing = _source_modules() - _declared_modules()
    assert not missing, (
        f"{sorted(missing)} exist but are not in pyproject's py-modules, so they "
        "will be missing from the built wheel"
    )


def test_every_declared_module_exists():
    phantom = _declared_modules() - _source_modules()
    assert not phantom, f"pyproject declares {sorted(phantom)} but no such file exists"


def test_first_party_imports_are_all_packaged():
    """Whatever server.py imports from this project must ship with it.

    The precise failure that got through: server.py imports envmemory, plans and
    targeting, none of which were in the wheel.
    """
    declared = _declared_modules()
    source = _source_modules()
    tree = ast.parse((ROOT / "server.py").read_text())

    imported_first_party = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in source:
                    imported_first_party.add(root)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            root = node.module.split(".")[0]
            if root in source:
                imported_first_party.add(root)

    assert imported_first_party, "expected server.py to import project modules"
    missing = imported_first_party - declared
    assert not missing, (
        f"server.py imports {sorted(missing)} but the wheel will not contain them"
    )


def test_entry_point_targets_something_real():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    target = cfg["project"]["scripts"]["mac-computer-use"]
    module, _, func = target.partition(":")
    assert module in _declared_modules(), f"entry point module {module!r} is not packaged"
    tree = ast.parse((ROOT / f"{module}.py").read_text())
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert func in names, f"entry point {target!r} points at a function that does not exist"
