#!/usr/bin/env python3
"""Check the integration's classes for the damage a bad edit leaves behind.

A class declaration line that is accidentally dropped (while editing a large
file, say) does not always stop the file from loading: the class below it can
quietly become part of the one above. Two signs give it away, and this script
looks for both in every Python file of the integration:

* a class with more than one ``__init__`` method;
* a string on its own line anywhere in a class body except the first line
  (an orphaned docstring, left behind when its ``class`` line went missing).

Usage:
    python3 tools/validate_classes.py [directory]

The directory defaults to ``custom_components/mg_saic``. Exits with 1 and lists
every problem if any is found, 0 otherwise.
"""

from __future__ import annotations

import ast
from pathlib import Path
import sys

DEFAULT_DIRECTORY = "custom_components/mg_saic"


def check_file(path: Path) -> tuple[int, list[str]]:
    """Return the number of classes in a file and the problems found in it."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as ex:
        return 0, [f"{path}: SyntaxError: {ex}"]

    errors: list[str] = []
    classes = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        classes += 1
        inits = [
            item
            for item in node.body
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
            and item.name == "__init__"
        ]
        if len(inits) > 1:
            lines = ", ".join(str(item.lineno) for item in inits)
            errors.append(
                f"{path}: class {node.name} (line {node.lineno}) "
                f"has {len(inits)} __init__ methods at lines {lines}"
            )
        for item in node.body[1:]:
            if (
                isinstance(item, ast.Expr)
                and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)
            ):
                errors.append(
                    f"{path}: class {node.name} (line {node.lineno}) "
                    f"has an orphaned string literal at line {item.lineno} "
                    "- likely a missing class declaration above it"
                )
    return classes, errors


def main(argv: list[str]) -> int:
    directory = Path(argv[1] if len(argv) > 1 else DEFAULT_DIRECTORY)
    files = sorted(directory.glob("*.py"))
    if not files:
        print(f"No Python files found in {directory}")
        return 1

    total = 0
    errors: list[str] = []
    for path in files:
        classes, problems = check_file(path)
        total += classes
        errors.extend(problems)

    if errors:
        print("\nINTEGRATION CLASS VALIDATION FAILED:\n")
        for error in errors:
            print(f"  ERROR: {error}")
        print(
            "\nThis usually means a class declaration line was accidentally\n"
            "dropped during a file edit. Check the lines listed above."
        )
        return 1

    print(f"OK - {total} classes across {len(files)} files validated.")
    print("No duplicate __init__ methods or orphaned docstrings found.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
