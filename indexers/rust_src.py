"""Scan Rust sources.

A file ``library/core/src/option.rs`` owns ``core::option``. Types in
``core`` and ``alloc`` are also recorded under ``std::`` so ``use std::vec::Vec``
hits the same methods. ``pub use`` re-exports are not followed.

NOTE: braces inside strings confuse the scan, and ``pub use`` is skipped.
``sys`` directories are skipped. The cargo registry stops after the file
cap. Upgrade path: rust-analyzer's parser, run outside Sublime.

NOTE: the crate of the project being edited is not indexed. Upgrade path:
index the active view's folder. If the rust-src component is missing, Rust
stays quiet. Nothing is downloaded.
"""

import os
import re

from . import merge_modules, read_text, walk_files

MAX_FILES = 4000
_SKIP = {
    "test", "tests", "testing", "testdata", "examples", "benches",
    "node_modules", "sys", "sys_common",
}
_FN_RE = re.compile(
    r"\bpub(?:\([^)]*\))?\s+(?:(?:const|async|unsafe)\s+)*fn\s+"
    r"([A-Za-z_]\w*)\s*\(([^)]*)\)(?:\s*->\s*([^;{]+))?"
)
_TYPE_RE = re.compile(
    r"\bpub(?:\([^)]*\))?\s+(?:struct|enum|trait|type|union)\s+([A-Z]\w*)"
)
_IMPL_RE = re.compile(
    r"\bimpl(?:\s*<[^>{;]*>)?\s+(?:[\w:]+\s+for\s+)?([A-Za-z_][\w:]*)"
)


def _std_names(name):
    names = [name]
    for crate in ("core::", "alloc::"):
        if name.startswith(crate):
            alias = "std::" + name[len(crate):]
            if alias not in names:
                names.append(alias)
    return names


def _full(prefix, simple):
    if not simple:
        return prefix
    if "::" in simple or not prefix:
        return simple
    return prefix + "::" + simple


def _snippet(name, args):
    labels = []
    for part in (args or "").split(","):
        part = part.strip()
        if not part or part in ("self", "&self", "&mut self", "mut self"):
            continue
        label = part.split(":", 1)[0].strip()
        label = label.lstrip("&").replace("mut ", "").strip()
        if label:
            labels.append(label[:40])
    if not labels:
        return name + "()"
    bits = ["${%s:%s}" % (index, label) for index, label in enumerate(labels, 1)]
    return "%s(%s)" % (name, ", ".join(bits))


def _returns(text, prefix, owner):
    text = (text or "").strip()
    text = text.split("<", 1)[0].strip().rstrip(",")
    if text in ("Self", "self"):
        return owner
    if not text or not text[0].isupper():
        return ""
    return _full(prefix, text)


def scan_source(text, prefix):
    """Return records for one Rust file whose module path is ``prefix``."""
    out = []
    if prefix:
        for name in _std_names(prefix):
            out.append({"t": "m", "name": name, "doc": ""})
    depth = 0
    impl = None
    struct = None
    pending = []

    def emit_module(name, doc):
        for full in _std_names(name):
            out.append({"t": "m", "name": full, "doc": doc if full == name else ""})

    def emit_symbol(owner, name, kind, signature, doc, returns=""):
        for full in _std_names(owner):
            out.append({
                "t": "s", "owner": full, "name": name, "kind": kind,
                "signature": signature, "doc": doc if full == owner else "",
                "returns": returns if full == owner else (
                    _std_names(returns)[-1] if returns.startswith(("core::", "alloc::")) else returns
                ),
            })

    for raw in text.splitlines():
        if impl and depth <= impl[1]:
            impl = None
        if struct and depth <= struct[1]:
            struct = None
        stripped = raw.strip()
        if stripped.startswith("///"):
            pending.append(stripped[3:].strip())
            continue
        if stripped.startswith("//") or stripped.startswith("#!"):
            continue
        if not stripped:
            pending = []
            continue
        doc = "\n".join(pending).strip()
        code = re.sub(r"//.*", "", raw)
        start = depth
        typed = _TYPE_RE.search(code)
        impl_match = _IMPL_RE.search(code) if " fn " not in code and not code.strip().startswith("fn ") else None
        if typed:
            full = _full(prefix, typed.group(1))
            emit_module(full, doc)
            parent = full.rsplit("::", 1)[0] if "::" in full else ""
            if parent:
                emit_symbol(parent, typed.group(1), "type", typed.group(1), doc)
            if "{" in code:
                struct = (full, start)
            pending = []
        elif impl_match:
            owner = _full(prefix, impl_match.group(1))
            impl = (owner, start)
            pending = []
        else:
            func = _FN_RE.search(code)
            if func and impl and start > impl[1]:
                returns = _returns(func.group(3), prefix, impl[0])
                emit_symbol(
                    impl[0], func.group(1), "function",
                    _snippet(func.group(1), func.group(2)), doc, returns,
                )
                pending = []
            elif struct and start > struct[1]:
                field = re.match(r"pub(?:\([^)]*\))?\s+([a-z_]\w*)\s*:", stripped)
                if field:
                    emit_symbol(struct[0], field.group(1), "variable", field.group(1), doc)
                    pending = []
                else:
                    pending = []
            else:
                pending = []
        depth = start + code.count("{") - code.count("}")
        if depth < 0:
            depth = 0
    return out


def _std_prefix(path, root):
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    if "src" not in parts or not parts:
        return ""
    crate = parts[0]
    if crate.startswith("proc_macro"):
        return ""
    index = parts.index("src")
    mods = [part for part in parts[index + 1:-1] if part]
    leaf = parts[-1]
    if leaf not in ("mod.rs", "lib.rs") and leaf.endswith(".rs"):
        mods.append(leaf[:-3])
    if any(part in _SKIP for part in mods):
        return ""
    return "::".join([crate] + mods) if mods else crate


def _cargo_prefix(path, root):
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    if len(parts) < 2 or "src" not in parts:
        return ""
    crate = re.sub(r"-\d+\.\d+\.\d+.*$", "", parts[1])
    index = parts.index("src")
    mods = [part for part in parts[index + 1:-1] if part]
    leaf = parts[-1]
    if leaf not in ("mod.rs", "lib.rs") and leaf.endswith(".rs"):
        mods.append(leaf[:-3])
    if not crate or any(part in _SKIP for part in mods):
        return ""
    return "::".join([crate] + mods) if mods else crate


def _tree(root, prefix_of, limit):
    if not root or not os.path.isdir(root):
        return
    for path in walk_files([root], (".rs",), limit, _SKIP):
        prefix = prefix_of(path, root)
        text = read_text(path)
        if not text or not prefix:
            continue
        for rec in scan_source(text, prefix):
            yield rec


def scan_roots(library, cargo="", limit=MAX_FILES):
    """Yield records from the rust-src library, then the cargo registry."""
    def records():
        yield from _tree(library, _std_prefix, limit)
        if cargo:
            yield from _tree(cargo, _cargo_prefix, limit)
    yield from merge_modules(records())
