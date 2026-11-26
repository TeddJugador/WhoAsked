"""Every text file read or write names its encoding.

Python's default is UTF-8 on Linux but Windows-1252 on Windows, so a bare read_text()
garbles UTF-8 text (the event catalogue's dashes, the README's arrows) on a Windows
machine, and a bare write_text() can corrupt the file it writes. Found the hard way, on
Windows. (Ruff's rule for this, PLW1514, is still preview-only.)
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("src", "infra", "eval", "tests", "scripts", "app.py")
TEXT_METHODS = {"read_text", "write_text"}


def _python_files() -> list[Path]:
    files: list[Path] = []
    for entry in SCANNED:
        path = ROOT / entry
        files.extend([path] if path.is_file() else sorted(path.rglob("*.py")))
    return [f for f in files if "cdk.out" not in f.parts and "build" not in f.parts]


def _binary_mode(call: ast.Call) -> bool:
    mode = (
        call.args[1]
        if len(call.args) > 1
        else next((k.value for k in call.keywords if k.arg == "mode"), None)
    )
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value


def test_text_io_always_names_its_encoding() -> None:
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else ""
            )
            is_text_io = name in TEXT_METHODS or (name == "open" and not _binary_mode(node))
            if is_text_io and not any(k.arg == "encoding" for k in node.keywords):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}()")
    assert not offenders, "text I/O without encoding=:\n" + "\n".join(offenders)
