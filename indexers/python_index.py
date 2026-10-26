"""Index a Python install without executing site-packages.

Run with the user's interpreter:

    python3 python_index.py --out symbols.jsonl
    python3 python_index.py --lib ./some/tree --out symbols.jsonl

``.py`` files are parsed with ``ast``. The allowlist of C stdlib modules is
imported with ``inspect`` because those modules have no Python source.
``--lib`` skips the real stdlib and the allowlist so a fixture stays hermetic.

NOTE: site-packages are AST-only, so compiled extensions there contribute no
members. Upgrade path: add the extension's module name to ``C_MODULES``.

NOTE: the walk stops after ``MAX_FILES`` files and skips directories named
``test``, ``tests``, or ``testing``. Upgrade path: index one top-level
package at a time.
"""

import ast
import importlib
import inspect
import json
import os
import sys

MAX_FILES = 8000
SKIP_DIRS = {"test", "tests", "testing", "__pycache__", "site-packages"}

# builtins is included so str, list, dict, and the other builtin types resolve.
# The rest are C modules (or wrappers) the plan indexes by import.
C_MODULES = (
    "builtins",
    "math",
    "json",
    "re",
    "os",
    "sys",
    "collections",
    "itertools",
    "functools",
    "datetime",
    "pathlib",
    "typing",
    "sqlite3",
    "io",
    "abc",
    "enum",
    "dataclasses",
)


def clip(text):
    if not text:
        return ""
    return " ".join(text.split())[:500]


def snippet_from_args(name, args):
    parts = []
    index = 1

    def add(label):
        nonlocal index
        parts.append("${%s:%s}" % (index, label))
        index += 1

    pos = list(getattr(args, "posonlyargs", ())) + list(args.args)
    for arg in pos:
        if arg.arg in ("self", "cls") and index == 1:
            continue
        add(arg.arg)
    if args.vararg:
        add("*" + args.vararg.arg)
    for arg in args.kwonlyargs:
        add(arg.arg)
    if args.kwarg:
        add("**" + args.kwarg.arg)
    return "%s(%s)" % (name, ", ".join(parts))


def snippet_from_signature(name, obj):
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return name + "()"
    parts = []
    index = 1
    for param in sig.parameters.values():
        if param.name in ("self", "cls") and index == 1:
            continue
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            label = "*" + param.name
        elif param.kind is inspect.Parameter.VAR_KEYWORD:
            label = "**" + param.name
        else:
            label = param.name
        parts.append("${%s:%s}" % (index, label))
        index += 1
    return "%s(%s)" % (name, ", ".join(parts))


def module_name(root, path):
    rel = os.path.splitext(os.path.relpath(path, root))[0]
    parts = rel.split(os.sep)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or any(part.startswith(".") for part in parts):
        return None
    return ".".join(parts)


def collect_files(root, acc):
    if len(acc) >= MAX_FILES or not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name for name in dirnames
            if name not in SKIP_DIRS and not name.endswith((".egg-info", ".dist-info"))
        ]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            acc.append(os.path.join(dirpath, filename))
            if len(acc) >= MAX_FILES:
                return


class _Visitor(ast.NodeVisitor):
    def __init__(self, module, out, seen_modules):
        self.owner = module
        self.out = out
        self.seen_modules = seen_modules

    def visit_ClassDef(self, node):
        fq = self.owner + "." + node.name
        doc = clip(ast.get_docstring(node) or "")
        self.out.append({
            "t": "s", "owner": self.owner, "name": node.name,
            "kind": "type", "signature": node.name, "doc": doc,
        })
        self.out.append({"t": "m", "name": fq, "doc": doc})
        previous = self.owner
        self.owner = fq
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.visit(child)
        self.owner = previous

    def visit_FunctionDef(self, node):
        self._function(node)

    def visit_AsyncFunctionDef(self, node):
        self._function(node)

    def _function(self, node):
        doc = clip(ast.get_docstring(node) or "")
        self.out.append({
            "t": "s",
            "owner": self.owner,
            "name": node.name,
            "kind": "function",
            "signature": snippet_from_args(node.name, node.args),
            "doc": doc,
        })


def index_file(root, path, out, seen_modules):
    module = module_name(root, path)
    if not module:
        return
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            source = handle.read()
        tree = ast.parse(source, filename=path)
    except (OSError, SyntaxError, ValueError):
        return
    doc = clip(ast.get_docstring(tree) or "")
    if module not in seen_modules:
        out.append({"t": "m", "name": module, "doc": doc})
        seen_modules.add(module)
    visitor = _Visitor(module, out, seen_modules)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            visitor.visit(node)


def _defined_on(cls, name):
    for base in cls.__mro__:
        if name in base.__dict__:
            return base is cls
    return False


def index_c_module(name, out, seen_modules):
    try:
        mod = importlib.import_module(name)
    except Exception:
        return
    if name not in seen_modules:
        seen_modules.add(name)
        out.append({"t": "m", "name": name, "doc": clip(inspect.getdoc(mod) or "")})
    for member_name, obj in inspect.getmembers(mod):
        if member_name.startswith("_"):
            continue
        if inspect.isclass(obj):
            fq = name + "." + member_name
            doc = clip(inspect.getdoc(obj) or "")
            out.append({
                "t": "s", "owner": name, "name": member_name,
                "kind": "type", "signature": member_name, "doc": doc,
            })
            out.append({"t": "m", "name": fq, "doc": doc})
            for method_name, method in inspect.getmembers(obj):
                if method_name.startswith("_") and method_name != "__init__":
                    continue
                if not _defined_on(obj, method_name):
                    continue
                if not (
                    inspect.isfunction(method)
                    or inspect.ismethod(method)
                    or inspect.isbuiltin(method)
                    or inspect.ismethoddescriptor(method)
                ):
                    continue
                out.append({
                    "t": "s",
                    "owner": fq,
                    "name": method_name,
                    "kind": "function",
                    "signature": snippet_from_signature(method_name, method),
                    "doc": clip(inspect.getdoc(method) or ""),
                })
        elif inspect.isfunction(obj) or inspect.isbuiltin(obj):
            out.append({
                "t": "s",
                "owner": name,
                "name": member_name,
                "kind": "function",
                "signature": snippet_from_signature(member_name, obj),
                "doc": clip(inspect.getdoc(obj) or ""),
            })


def stdlib_roots():
    import sysconfig
    roots = []
    for key in ("stdlib", "platstdlib"):
        path = sysconfig.get_path(key)
        if path and path not in roots and os.path.isdir(path):
            roots.append(path)
    return roots


def site_roots():
    import site
    roots = []
    try:
        for path in site.getsitepackages():
            if path and path not in roots and os.path.isdir(path):
                roots.append(path)
    except Exception:
        pass
    user = site.getusersitepackages()
    if user and user not in roots and os.path.isdir(user):
        roots.append(user)
    return roots


def build(lib=None):
    out = []
    seen = set()
    files = []
    if lib:
        lib = os.path.abspath(lib)
        collect_files(lib, files)
        for path in files:
            index_file(lib, path, out, seen)
        return out
    roots = stdlib_roots() + site_roots()
    for root in roots:
        collect_files(root, files)
    for path in files:
        root = next(
            (item for item in roots if path == item or path.startswith(item + os.sep)),
            None,
        )
        if root:
            index_file(root, path, out, seen)
    for name in C_MODULES:
        index_c_module(name, out, seen)
    return out


def main(argv):
    out_path = None
    lib = None
    args = argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--out" and i + 1 < len(args):
            out_path = args[i + 1]
            i += 2
        elif args[i] == "--lib" and i + 1 < len(args):
            lib = args[i + 1]
            i += 2
        else:
            i += 1
    if not out_path:
        print("usage: python_index.py --out FILE [--lib DIR]", file=sys.stderr)
        return 2
    records = build(lib)
    with open(out_path, "w", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec, ensure_ascii=False))
            handle.write("\n")
    print("indexed %s records" % len(records), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
