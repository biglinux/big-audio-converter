"""One-time, checked source transformations for the audit feature branch."""

import ast
from pathlib import Path
import textwrap

ROOT = Path("big-audio-converter/usr/share/biglinux/audio-converter")


def replace(path, old, new):
    path = Path(path)
    text = path.read_text()
    if old not in text:
        if new in text:
            return
        raise RuntimeError(f"Expected source anchor not found in {path}: {old[:100]}")
    path.write_text(text.replace(old, new))


def method(path, class_name, name, source):
    path = Path(path)
    text = path.read_text()
    tree = ast.parse(text)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    node = next((item for item in cls.body if isinstance(item, ast.FunctionDef) and item.name == name), None)
    lines = text.splitlines(keepends=True)
    replacement = textwrap.indent(textwrap.dedent(source).strip() + "\n", "    ")
    if node is None:
        lines[cls.end_lineno:cls.end_lineno] = ["\n", replacement]
    else:
        first = min([node.lineno] + [decorator.lineno for decorator in node.decorator_list])
        lines[first - 1:node.end_lineno] = [replacement]
    path.write_text("".join(lines))


def source_method(path, class_name, name):
    text = Path(path).read_text()
    cls = next(node for node in ast.parse(text).body if isinstance(node, ast.ClassDef) and node.name == class_name)
    node = next(item for item in cls.body if isinstance(item, ast.FunctionDef) and item.name == name)
    return textwrap.dedent("\n".join(text.splitlines()[node.lineno - 1:node.end_lineno]))


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())
