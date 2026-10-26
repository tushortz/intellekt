"""Scan a Go install.

Exported ``func``, ``type``, and ``const`` names are kept. A method
``func (b *Buffer) Write`` is owned by ``<import-path>.Buffer``. The import
path is the directory under ``GOROOT/src`` or the module cache, with the
``@version`` suffix removed.

NOTE: ``cmd`` is skipped. Files for other operating systems are still
indexed (``//go:build`` is ignored), and a signature that spans lines is
missed. The module cache stops after ``MODULE_FILES``. Upgrade path: honor
build tags, then index one module at a time.

NOTE: a module in the project being edited is not indexed unless it is
already in the module cache. Upgrade path: index the active view's folder.
"""

import os
import re

from . import merge_modules, read_text, walk_files

STDLIB_FILES = 8000
MODULE_FILES = 2000
# Keep in step with core.GO_BUILTINS. error.Error is the one real method.
_BUILTINS = (
    "string", "bool", "error", "any", "byte", "rune",
    "int", "int8", "int16", "int32", "int64",
    "uint", "uint8", "uint16", "uint32", "uint64", "uintptr",
    "float32", "float64", "complex64", "complex128",
)
_SKIP = {"test", "tests", "testing", "testdata", "vendor", "cmd", "node_modules"}
_FUNC_RE = re.compile(
    r"^func\s+(?:\((?P<recv>[^)]*)\)\s+)?(?P<name>[A-Za-z_]\w*)\s*"
    r"\((?P<args>[^)]*)\)\s*(?P<rest>.*)$"
)
_BASIC = {
    "int", "int8", "int16", "int32", "int64", "uint", "uint8", "uint16",
    "uint32", "uint64", "uintptr", "float32", "float64", "complex64",
    "complex128", "byte", "rune", "string", "bool", "any",
}


def _snippet(name, args):
    labels = []
    for part in args.split(","):
        part = part.strip()
        if not part:
            continue
        label = part.split()[0].lstrip("*").strip()
        if label:
            labels.append(label[:40])
    if not labels:
        return name + "()"
    bits = ["${%s:%s}" % (index, label) for index, label in enumerate(labels, 1)]
    return "%s(%s)" % (name, ", ".join(bits))


def _type_name(text, prefix):
    text = (text or "").strip()
    if not text or text.startswith("("):
        found = ""
        for part in text.strip("()").split(","):
            part = part.strip().lstrip("*").split(".")[-1]
            if part == "error" or (part[:1].isupper() and part not in _BASIC):
                found = part
                break
        text = found
    else:
        # ``func F() *`` is a return type continued on the next line.
        # ``" *".split()`` is empty, so there is no first token.
        bits = text.lstrip("*").split()
        if not bits:
            return ""
        text = bits[0].split(".")[-1]
    if not text or text in _BASIC:
        return ""
    if text == "error":
        return "error"
    if not text[0].isupper():
        return ""
    if "." in text or not prefix:
        return text
    return prefix + "." + text


def _recv(text):
    match = re.search(r"\*?([A-Z]\w*)\s*$", (text or "").strip())
    return match.group(1) if match else ""


def _import_path(path, root, modules):
    rel = os.path.relpath(os.path.dirname(path), root)
    if rel == ".":
        return ""
    parts = rel.split(os.sep)
    if any(part in ("vendor", "testdata", "internal") for part in parts):
        # internal is still indexed: callers inside that tree import it.
        # vendor and testdata are not.
        if any(part in ("vendor", "testdata") for part in parts):
            return ""
    if modules:
        parts = [part.split("@", 1)[0] for part in parts]
    return "/".join(parts)


def scan_source(text, import_path):
    """Return records for one Go file already known to live at ``import_path``."""
    if not import_path:
        return []
    out = [{"t": "m", "name": import_path, "doc": ""}]
    pending = []
    const_block = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("//"):
            pending.append(stripped[2:].strip())
            continue
        if not stripped:
            pending = []
            continue
        doc = "\n".join(pending).strip()
        pending = []
        if stripped.startswith("const ("):
            const_block = True
            continue
        if const_block:
            if stripped.startswith(")"):
                const_block = False
            else:
                match = re.match(r"([A-Z]\w*)\b", stripped)
                if match:
                    name = match.group(1)
                    out.append({
                        "t": "s", "owner": import_path, "name": name,
                        "kind": "variable", "signature": name, "doc": doc, "returns": "",
                    })
            continue
        const = re.match(r"const\s+([A-Z]\w*)\b", stripped)
        if const:
            name = const.group(1)
            out.append({
                "t": "s", "owner": import_path, "name": name, "kind": "variable",
                "signature": name, "doc": doc, "returns": "",
            })
            continue
        typed = re.match(r"type\s+([A-Z]\w*)\b", stripped)
        if typed:
            name = typed.group(1)
            full = import_path + "." + name
            out.append({"t": "m", "name": full, "doc": doc})
            out.append({
                "t": "s", "owner": import_path, "name": name, "kind": "type",
                "signature": name, "doc": doc, "returns": "",
            })
            continue
        func = _FUNC_RE.match(stripped)
        if not func or not func.group("name")[:1].isupper():
            continue
        name = func.group("name")
        recv = _recv(func.group("recv"))
        owner = import_path + "." + recv if recv else import_path
        returns = _type_name(func.group("rest").split("{", 1)[0], import_path)
        if recv and returns == recv:
            returns = owner
        out.append({
            "t": "s", "owner": owner, "name": name, "kind": "function",
            "signature": _snippet(name, func.group("args") or ""),
            "doc": doc, "returns": returns,
        })
    return out


def _tree(root, modules, limit):
    if not root or not os.path.isdir(root):
        return
    for path in walk_files([root], (".go",), limit, _SKIP):
        base = os.path.basename(path)
        if base.endswith("_test.go"):
            continue
        import_path = _import_path(path, root, modules)
        text = read_text(path)
        if not text or not import_path:
            continue
        for rec in scan_source(text, import_path):
            yield rec


def scan_roots(stdlib, modules="", stdlib_limit=STDLIB_FILES, module_limit=MODULE_FILES):
    """Yield records from ``GOROOT/src`` and, after that, the module cache."""
    def records():
        for name in _BUILTINS:
            yield {"t": "m", "name": name, "doc": ""}
        yield {
            "t": "s", "owner": "error", "name": "Error", "kind": "function",
            "signature": "Error()", "doc": "Error message.", "returns": "",
        }
        yield from _tree(stdlib, False, stdlib_limit)
        yield from _tree(modules, True, module_limit)
    yield from merge_modules(records())
