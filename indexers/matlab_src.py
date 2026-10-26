"""Scan GNU Octave or MATLAB ``.m`` files.

The first function in a script file is the public one. A ``classdef`` file
records that class and treats every function in the file as a method.

NOTE: local functions after a classdef are recorded as methods. Nested
``end`` is not matched. Upgrade path: a block matcher.
"""

import re

from . import merge_modules, read_text, walk_files

MAX_FILES = 4000
_SKIP = {"test", "tests", "testing", "private"}
_CLASS_RE = re.compile(r"(?m)^\s*classdef\s+(?:\([^)\n]*\)\s*)?([A-Za-z_]\w*)")
_FN_RE = re.compile(
    r"(?m)^\s*function\s+(?:\[[^\]\n]*\]\s*=\s*|[A-Za-z_]\w*\s*=\s*)?"
    r"([A-Za-z_]\w*)\s*(\([^)\n]*\))?"
)


def _snippet(name, args):
    labels = []
    for part in (args or "").strip()[1:-1].split(","):
        part = part.strip()
        if part:
            labels.append(part)
    if not labels:
        return name + "()"
    bits = ["${%s:%s}" % (index, label) for index, label in enumerate(labels, 1)]
    return "%s(%s)" % (name, ", ".join(bits))


def scan_source(text):
    """Yield records for one ``.m`` file."""
    text = re.sub(r"%.*?$", "", text, flags=re.M)
    found = _CLASS_RE.search(text)
    class_name = found.group(1) if found else ""
    if class_name:
        yield {"t": "m", "name": class_name, "doc": ""}
        yield {
            "t": "s", "owner": "matlab", "name": class_name, "kind": "type",
            "signature": class_name, "doc": "",
        }
    seen_main = False
    for match in _FN_RE.finditer(text):
        name, args = match.group(1), match.group(2)
        if class_name:
            owner = class_name
        elif seen_main:
            break
        else:
            seen_main = True
            owner = "matlab"
        yield {
            "t": "s", "owner": owner, "name": name, "kind": "function",
            "signature": _snippet(name, args), "doc": "",
        }


def scan_roots(roots):
    def records():
        for path in walk_files(roots, (".m",), MAX_FILES, _SKIP):
            text = read_text(path)
            if text:
                for rec in scan_source(text):
                    yield rec
    yield from merge_modules(records())
