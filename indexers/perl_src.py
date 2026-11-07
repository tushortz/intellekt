"""Scan installed Perl modules for ``package`` and ``sub`` lines, skipping POD."""
# NOTE: this is a line scan, so a sub built by a string eval is missed.
# Upgrade path: ``PPI``.

from __future__ import annotations

import re
from typing import Any, Iterable, Iterator, Optional

from . import merge_modules, read_text, walk_files

MAX_FILES = 4000
_SKIP = {"t", "xt", "test", "tests", "testing", "inc"}
_PKG_RE = re.compile(r"^package\s+([\w:]+)\b")
_SUB_RE = re.compile(r"^sub\s+([A-Za-z]\w*)\s*(\([^)]*\))?")
_SKIP_SUBS = {"BEGIN", "END", "CHECK", "INIT", "UNITCHECK"}
_BUILTINS = (
    "bless", "chomp", "chop", "close", "defined", "die", "each", "exists",
    "grep", "join", "keys", "length", "map", "open", "pop", "print", "printf",
    "push", "ref", "say", "shift", "splice", "split", "sprintf", "substr",
    "undef", "values", "warn",
)


def _snippet(name: str, args: Optional[str]) -> str:
    labels = []
    for part in (args or "").strip()[1:-1].split(","):
        part = part.strip().lstrip("$%@")
        if part:
            labels.append(part)
    if not labels:
        return name + "()"
    bits = ["${%s:%s}" % (index, label) for index, label in enumerate(labels, 1)]
    return "%s(%s)" % (name, ", ".join(bits))


def scan_source(text: str, fallback: str = "") -> Iterator[dict[str, Any]]:
    """Yield records for one ``.pm`` file."""
    package = fallback
    seen = set()
    in_pod = False
    if package:
        seen.add(package)
        yield {"t": "m", "name": package, "doc": ""}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("=cut"):
            in_pod = False
            continue
        if line.startswith("=") and not line.startswith("=="):
            in_pod = True
            continue
        if in_pod or not line or line.startswith("#"):
            continue
        found = _PKG_RE.match(line)
        if found:
            package = found.group(1)
            if package not in seen:
                seen.add(package)
                yield {"t": "m", "name": package, "doc": ""}
            continue
        found = _SUB_RE.match(line)
        if not found or not package:
            continue
        name = found.group(1)
        if name in _SKIP_SUBS or name.startswith("_"):
            continue
        yield {
            "t": "s", "owner": package, "name": name, "kind": "function",
            "signature": _snippet(name, found.group(2)), "doc": "",
        }


def scan_roots(roots: Iterable[str]) -> Iterator[dict[str, Any]]:
    def records() -> Iterator[dict[str, Any]]:
        for name in _BUILTINS:
            yield {
                "t": "s", "owner": "perl", "name": name, "kind": "function",
                "signature": name + "()", "doc": "",
            }
        for path in walk_files(roots, (".pm",), MAX_FILES, _SKIP):
            text = read_text(path)
            if text:
                for rec in scan_source(text):
                    yield rec
    yield from merge_modules(records())
