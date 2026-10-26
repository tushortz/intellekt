"""Scan TypeScript declaration files.

Global values (``declare var console: Console``) are stored on the owner
``global``. The cache rejects an empty owner. ``console.`` follows the
recorded type, which is the interface module ``Console``.

NOTE: this is a brace scan, not a TypeScript parser. Overloads merged across
files, mapped types, and most generics are missed. Upgrade path: the
TypeScript binder, run outside Sublime.

NOTE: only the installed ``typescript`` package and the global npm root are
read. A project's ``node_modules`` is not. Upgrade path: index the folder of
the active view.
"""

import re

from . import SKIP_DIRS, merge_modules, read_text, walk_files

MAX_FILES = 4000
_SKIP_RETURNS = {
    "void", "any", "unknown", "never", "string", "number", "boolean",
    "null", "undefined", "object", "symbol", "bigint", "true", "false",
}
_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "function", "new"}


def _clean_doc(raw):
    lines = [re.sub(r"^\s*\*\s?", "", line).rstrip() for line in raw.splitlines()]
    return "\n".join(lines).strip()


def _docs(text):
    found = {}
    for match in re.finditer(r"/\*\*(.*?)\*/", text, re.S):
        line = text.count("\n", 0, match.end())
        found[line] = _clean_doc(match.group(1))
    return found


def _blank(text):
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    return re.sub(r"//.*?$", "", text, flags=re.M)


def _labels(args):
    labels = []
    for part in args.split(","):
        part = part.strip()
        if not part:
            continue
        name = part.split(":")[0].split("=")[0].strip().lstrip(".")
        name = name.rstrip("?").strip()
        if not name or name in ("this", "..."):
            continue
        labels.append(name[:40])
    return labels


def _snippet(name, args):
    labels = _labels(args)
    if not labels:
        return name + "()"
    bits = ["${%s:%s}" % (index, label) for index, label in enumerate(labels, 1)]
    return "%s(%s)" % (name, ", ".join(bits))


def _returns(text):
    text = (text or "").strip().rstrip(";").strip()
    if not re.fullmatch(r"[A-Za-z_][\w.]*", text):
        return ""
    if text in _SKIP_RETURNS:
        return ""
    return text


def _symbol(owner, name, kind, signature, doc, returns=""):
    return {
        "t": "s",
        "owner": owner,
        "name": name,
        "kind": kind,
        "signature": signature,
        "doc": doc,
        "returns": returns,
    }


def scan_source(text):
    """Return module and symbol records for one ``.d.ts`` file."""
    docs = _docs(text)
    lines = _blank(text).splitlines()
    out = []
    depth = 0
    frames = []

    def prefix():
        parts = [frame["name"] for frame in frames if frame["kind"] in ("mod", "ns")]
        return ".".join(parts)

    def current_type():
        for frame in reversed(frames):
            if frame["kind"] == "type":
                return frame
        return None

    def doc_at(index):
        return docs.get(index) or docs.get(index - 1) or ""

    for index, line in enumerate(lines):
        frames[:] = [frame for frame in frames if frame["depth"] <= depth]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        doc = doc_at(index)
        opened = stripped.count("{")
        module = re.search(r"""\bmodule\s+['"]([^'"]+)['"]""", stripped)
        namespace = re.search(r"\bnamespace\s+([A-Za-z_]\w*)", stripped)
        typed = re.search(
            r"\b(interface|class|enum)\s+([A-Za-z_]\w*)",
            stripped,
        )
        func = re.search(
            r"\bfunction\s+([A-Za-z_]\w*)\s*(?:<[^()<>]*>)?\s*\(([^)]*)\)"
            r"(?:\s*:\s*([^{;]+))?",
            stripped,
        )
        var = re.search(
            r"\b(?:var|const|let)\s+([A-Za-z_]\w*)\s*:\s*([A-Za-z_][\w.]*)",
            stripped,
        )
        if module and opened:
            frames.append({"kind": "mod", "name": module.group(1), "depth": depth + 1})
            out.append({"t": "m", "name": module.group(1), "doc": doc})
        elif namespace and opened:
            parent = prefix()
            name = namespace.group(1)
            full = parent + "." + name if parent else name
            frames.append({"kind": "ns", "name": name, "depth": depth + 1})
            out.append({"t": "m", "name": full, "doc": doc})
        elif typed:
            parent = prefix()
            simple = typed.group(2)
            full = parent + "." + simple if parent else simple
            out.append({"t": "m", "name": full, "doc": doc})
            if parent:
                out.append(_symbol(parent, simple, "type", simple, doc))
            if opened:
                frames.append({"kind": "type", "name": full, "depth": depth + 1})
        elif func and not current_type():
            owner = prefix() or "global"
            returns = _returns(func.group(3))
            out.append(_symbol(
                owner, func.group(1), "function",
                _snippet(func.group(1), func.group(2) or ""), doc, returns,
            ))
        elif var and not current_type():
            owner = prefix() or "global"
            returns = _returns(var.group(2))
            out.append(_symbol(owner, var.group(1), "variable", var.group(1), doc, returns))
        else:
            holder = current_type()
            method = re.match(
                r"(?:(?:public|private|protected|static|readonly|abstract|async|get|set)\s+)*"
                r"([A-Za-z_]\w*)\??\s*(?:<[^()<>]*>)?\s*\(([^)]*)\)\s*(?::\s*([^;{]+))?",
                stripped,
            )
            if holder and method and method.group(1) not in _KEYWORDS:
                name = method.group(1)
                out.append(_symbol(
                    holder["name"], name, "function",
                    _snippet(name, method.group(2) or ""), doc, _returns(method.group(3)),
                ))
        depth += opened - stripped.count("}")
        if depth < 0:
            depth = 0
    return out


def scan_roots(roots, limit=MAX_FILES):
    """Yield records from ``.d.ts`` files under ``roots``."""
    def records():
        seen = 0
        for path in walk_files(roots, (".d.ts",), limit, SKIP_DIRS | {"dist", "test", "tests"}):
            text = read_text(path)
            if not text:
                continue
            seen += 1
            for rec in scan_source(text):
                yield rec
            if seen >= limit:
                break
    yield from merge_modules(records())
