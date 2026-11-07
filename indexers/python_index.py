"""Index a Python install: ``python3 python_index.py --out symbols.jsonl [--lib
./tree]``. Runs under the user's interpreter, reading ``.py`` files via
``ast`` and C/site modules via ``inspect``."""
# NOTE: each site-package import is capped at ``SITE_IMPORT_SECONDS``; slow or
# failing imports are skipped. Upgrade path: import only packages the edited
# file names.
# NOTE: the walk stops after ``MAX_FILES`` per tree and skips test dirs.
# Upgrade path: index one distribution at a time.

from __future__ import annotations

import ast
import importlib
import inspect
import json
import os
import signal
import sys
from typing import Any, Optional

MAX_FILES = 8000
SITE_IMPORT_SECONDS = 8
# Submodules of an imported package, not the whole dependency graph.
SITE_MODULE_DEPTH = 2
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


def clip(text: Optional[str]) -> str:
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def snippet_from_args(name: str, args: ast.arguments) -> str:
    parts = []
    index = 1

    def add(label: str) -> None:
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


def snippet_from_signature(name: str, obj: Any) -> str:
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


def _annotation_text(node: Optional[ast.expr]) -> str:
    """Dotted name from a return annotation. ``X | None`` keeps ``X``."""
    if node is None:
        return ""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip()
    if isinstance(node, ast.Attribute):
        try:
            return ast.unparse(node)
        except Exception:
            return ""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left, right = _annotation_text(node.left), _annotation_text(node.right)
        if right in ("None", "NoneType"):
            return left
        if left in ("None", "NoneType"):
            return right
    return ""


def _decorator_name(dec: ast.expr) -> str:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def module_name(root: str, path: str) -> tuple[Optional[str], bool]:
    """Return ``(dotted name, is_package)``. ``__init__.py`` is the package."""
    rel = os.path.splitext(os.path.relpath(path, root))[0]
    parts = rel.split(os.sep)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    if not parts or any(part.startswith(".") for part in parts):
        return None, False
    return ".".join(parts), is_package


def owning_root(path: str, roots: list[str]) -> Optional[str]:
    """Longest matching root, so a site-packages file is not ``site-packages.pkg``."""
    matches = [
        item for item in roots
        if path == item or path.startswith(item + os.sep)
    ]
    if not matches:
        return None
    return max(matches, key=len)


def collect_files(root: str, acc: list[str], limit: int) -> None:
    if len(acc) >= limit or not os.path.isdir(root):
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
            if len(acc) >= limit:
                return


class _Visitor(ast.NodeVisitor):
    def __init__(self, module: str, out: list[dict[str, Any]], seen_modules: set[str]) -> None:
        self.owner = module
        self.out = out
        self.seen_modules = seen_modules
        self.in_class = False

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        fq = self.owner + "." + node.name
        doc = clip(ast.get_docstring(node) or "")
        self.out.append({
            "t": "s", "owner": self.owner, "name": node.name,
            "kind": "type", "signature": node.name, "doc": doc,
        })
        self.out.append({"t": "m", "name": fq, "doc": doc})
        previous, was_class = self.owner, self.in_class
        self.owner = fq
        self.in_class = True
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.visit(child)
        self.owner = previous
        self.in_class = was_class

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def _function(self, node: Any) -> None:
        doc = clip(ast.get_docstring(node) or "")
        names = [_decorator_name(dec) for dec in node.decorator_list]
        is_property = "property" in names
        returns = _annotation_text(node.returns)
        # NOTE: an unannotated classmethod is recorded as returning its class
        # (datetime.now). A classmethod that returns something else needs an
        # annotation. Upgrade path: a type checker.
        if not returns and self.in_class and "classmethod" in names:
            returns = self.owner
        self.out.append({
            "t": "s",
            "owner": self.owner,
            "name": node.name,
            "kind": "variable" if is_property else "function",
            "signature": node.name if is_property else snippet_from_args(node.name, node.args),
            "doc": doc,
            "returns": returns,
        })


def _const_list(node: Optional[ast.expr]) -> Optional[list[str]]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    names = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            names.append(elt.value)
        else:
            return None
    return names


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _relative_module(module: str, is_package: bool, level: int, imported: Optional[str]) -> str:
    if level == 0:
        return imported or ""
    base = module.split(".") if is_package else module.split(".")[:-1]
    drop = level - 1
    if drop:
        if drop > len(base):
            return ""
        base = base[:-drop]
    if imported:
        base.extend(imported.split("."))
    return ".".join(part for part in base if part)


def _collect_exports(tree: ast.Module, module: str, is_package: bool) -> dict[str, Any]:
    """Return ``{"all": set|None, "bindings": [(local, module, name)]}``."""
    found = {"all": None, "bindings": []}
    top = module.split(".")[0]

    def handle(stmt: ast.stmt) -> None:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    names = _const_list(stmt.value)
                    if names is not None:
                        found["all"] = set(names)
        elif isinstance(stmt, ast.ImportFrom):
            target = _relative_module(module, is_package, stmt.level or 0, stmt.module)
            if not target or not (target == top or target.startswith(top + ".")):
                return
            for alias in stmt.names:
                local = alias.asname or (alias.name if alias.name != "*" else None)
                found["bindings"].append((local, target, alias.name))
        elif isinstance(stmt, ast.If) and _is_type_checking(stmt.test):
            for child in stmt.body:
                handle(child)

    for stmt in tree.body:
        handle(stmt)
    return found


def index_file(
    root: str,
    path: str,
    out: list[dict[str, Any]],
    seen_modules: set[str],
    exports: dict[str, dict[str, Any]],
) -> None:
    module, is_package = module_name(root, path)
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
        elif isinstance(node, ast.If) and _is_type_checking(node.test):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    visitor.visit(child)
    _bind_aliases(module, tree, out)
    exports[module] = _collect_exports(tree, module, is_package)


def _alias_signature(alias: str, source: dict[str, Any]) -> str:
    sig = source.get("signature") or source["name"]
    src = source["name"]
    if sig.startswith(src):
        return alias + sig[len(src):]
    return alias


def _bind_aliases(module: str, tree: ast.Module, out: list[dict[str, Any]]) -> None:
    """Copy ``name = obj.method`` onto the module (e.g. ``random.randint``)."""
    # NOTE: the first same-named function in the file supplies the signature, so a
    # binding to a different callable is not told apart. Upgrade path: follow the
    # assignment with a type checker.
    defined = {
        rec["name"] for rec in out
        if rec.get("t") == "s" and rec.get("owner") == module
    }
    prefix = module + "."
    by_name = {}
    for rec in out:
        if rec.get("t") != "s":
            continue
        owner = rec.get("owner") or ""
        if owner == module or owner.startswith(prefix):
            by_name.setdefault(rec["name"], rec)
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            targets, value = [stmt.target], stmt.value
        else:
            continue
        attr = value.attr if isinstance(value, ast.Attribute) else ""
        source = by_name.get(attr) if attr else None
        for target in targets:
            if not isinstance(target, ast.Name) or target.id.startswith("_"):
                continue
            name = target.id
            if name in defined:
                continue
            if source and source.get("kind") == "function":
                cloned = dict(source)
                cloned["owner"] = module
                cloned["name"] = name
                cloned["signature"] = _alias_signature(name, source)
            else:
                cloned = {
                    "t": "s", "owner": module, "name": name,
                    "kind": "function" if attr else "variable",
                    "signature": (name + "()") if attr else name,
                    "doc": "",
                }
            out.append(cloned)
            defined.add(name)


def link_submodules(out: list[dict[str, Any]]) -> None:
    """Add each child module as a member of its parent so ``pkg.sub`` completes."""
    modules = {}
    for rec in out:
        if rec.get("t") == "m":
            modules[rec["name"]] = rec.get("doc") or ""
    have = {(rec["owner"], rec["name"]) for rec in out if rec.get("t") == "s"}
    for name, doc in list(modules.items()):
        if "." not in name:
            continue
        parent, child = name.rsplit(".", 1)
        if parent in modules and (parent, child) not in have:
            out.append({
                "t": "s", "owner": parent, "name": child,
                "kind": "namespace", "signature": child, "doc": doc,
            })
            have.add((parent, child))


def apply_reexports(out: list[dict[str, Any]], exports: dict[str, dict[str, Any]]) -> None:
    """Copy imported names onto the package that re-exports them."""
    # NOTE: four passes; deeper re-export chains are dropped. Upgrade path: keep
    # passing until stable.
    by_owner = {}
    defined = set()
    modules = set()
    for rec in out:
        if rec.get("t") == "s":
            by_owner.setdefault(rec["owner"], []).append(rec)
            defined.add((rec["owner"], rec["name"]))
        elif rec.get("t") == "m":
            modules.add(rec["name"])

    def copy_methods(src_owner: str, dst_owner: str) -> None:
        if src_owner == dst_owner:
            return
        for rec in list(by_owner.get(src_owner, [])):
            if (dst_owner, rec["name"]) in defined:
                continue
            cloned = dict(rec)
            cloned["owner"] = dst_owner
            out.append(cloned)
            by_owner.setdefault(dst_owner, []).append(cloned)
            defined.add((dst_owner, rec["name"]))
        if dst_owner not in modules:
            out.append({"t": "m", "name": dst_owner, "doc": ""})
            modules.add(dst_owner)

    def copy_symbol(src_owner: str, src_name: str, dst_owner: str, dst_name: str) -> bool:
        if not dst_name or (dst_owner, dst_name) in defined:
            return False
        for rec in by_owner.get(src_owner, []):
            if rec["name"] != src_name:
                continue
            cloned = dict(rec)
            cloned["owner"] = dst_owner
            cloned["name"] = dst_name
            if rec.get("kind") == "type":
                cloned["signature"] = dst_name
            out.append(cloned)
            by_owner.setdefault(dst_owner, []).append(cloned)
            defined.add((dst_owner, dst_name))
            qual = dst_owner + "." + dst_name
            if qual not in modules:
                out.append({"t": "m", "name": qual, "doc": cloned.get("doc") or ""})
                modules.add(qual)
            if rec.get("kind") == "type":
                copy_methods(src_owner + "." + src_name, qual)
            return True
        return False

    for _pass in range(4):
        for module, exp in exports.items():
            explicit = {}
            stars = []
            for local, target_mod, target_name in exp["bindings"]:
                if target_name == "*":
                    stars.append(target_mod)
                elif local:
                    explicit[local] = (target_mod, target_name)
            names = exp["all"] if exp["all"] is not None else set(explicit)
            for name in names:
                if (module, name) in defined:
                    continue
                if name in explicit:
                    copy_symbol(explicit[name][0], explicit[name][1], module, name)
                    continue
                for target_mod in stars:
                    if copy_symbol(target_mod, name, module, name):
                        break
            if exp["all"] is None:
                for local, (target_mod, target_name) in explicit.items():
                    copy_symbol(target_mod, target_name, module, local)
                for target_mod in stars:
                    for rec in list(by_owner.get(target_mod, [])):
                        if not rec["name"].startswith("_"):
                            copy_symbol(target_mod, rec["name"], module, rec["name"])


def _defined_on(cls: type, name: str) -> bool:
    for base in cls.__mro__:
        if name in base.__dict__:
            return base is cls
    return False


def _inherited_data(cls: type, name: str) -> bool:
    """True if ``name`` is defined on ``cls`` or a base other than ``object``."""
    for base in cls.__mro__:
        if name in base.__dict__:
            return base is not object
    return False


def _member_kind(obj: Any) -> str:
    if isinstance(obj, property) or type(obj).__name__ in (
        "getset_descriptor", "member_descriptor",
    ):
        return "variable"
    if (
        inspect.isfunction(obj)
        or inspect.ismethod(obj)
        or inspect.isbuiltin(obj)
        or inspect.ismethoddescriptor(obj)
    ):
        return "function"
    return ""


def _is_classmethod(obj: Any) -> bool:
    if isinstance(obj, classmethod):
        return True
    text = getattr(obj, "__text_signature__", "") or ""
    return text.startswith("($type,") or text.startswith("($cls,")


def _return_type(obj: Any, owner: str) -> str:
    """Annotated return type, or ``owner`` for a classmethod.

    NOTE: an unannotated classmethod (``datetime.now``) is assumed to return
    its class; one returning another type needs an annotation.
    """
    try:
        ann = inspect.signature(obj).return_annotation
    except (TypeError, ValueError):
        ann = inspect.Signature.empty
    if ann is not inspect.Signature.empty and ann is not None:
        if isinstance(ann, str):
            return ann.strip()
        if isinstance(ann, type):
            module = getattr(ann, "__module__", "") or ""
            qual = getattr(ann, "__qualname__", "") or ann.__name__
            if module == "builtins":
                return qual
            if module:
                return module + "." + qual
            return qual
        if getattr(ann, "__name__", "") in ("Self", "self"):
            return owner
    if _is_classmethod(obj):
        return owner
    return ""


def _add_symbol(
    out: list[dict[str, Any]],
    defined: set[tuple[str, str]],
    owner: str,
    name: str,
    kind: str,
    signature: str,
    doc: str,
    returns: str = "",
) -> bool:
    if (owner, name) in defined:
        return False
    out.append({
        "t": "s", "owner": owner, "name": name,
        "kind": kind, "signature": signature, "doc": doc, "returns": returns,
    })
    defined.add((owner, name))
    return True


def index_c_module(
    name: str,
    out: list[dict[str, Any]],
    seen_modules: set[str],
    defined: set[tuple[str, str]],
    depth: int = 0,
) -> None:
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
        home = getattr(obj, "__module__", None) or ""
        in_package = not home or home == name or home.startswith(name + ".")
        if inspect.ismodule(obj):
            mod_name = getattr(obj, "__name__", "") or ""
            if mod_name != name + "." + member_name and not mod_name.startswith(name + "."):
                continue
            _add_symbol(out, defined, name, member_name, "namespace", member_name, clip(inspect.getdoc(obj) or ""))
            if depth < SITE_MODULE_DEPTH and mod_name not in seen_modules:
                index_c_module(mod_name, out, seen_modules, defined, depth + 1)
            continue
        if not in_package:
            continue
        if inspect.isclass(obj):
            fq = name + "." + member_name
            doc = clip(inspect.getdoc(obj) or "")
            if _add_symbol(out, defined, name, member_name, "type", member_name, doc):
                out.append({"t": "m", "name": fq, "doc": doc})
            for method_name, method in inspect.getmembers(obj):
                if method_name.startswith("_") and method_name != "__init__":
                    continue
                kind = _member_kind(method)
                if kind == "variable":
                    if not _inherited_data(obj, method_name):
                        continue
                elif kind != "function" or not _defined_on(obj, method_name):
                    continue
                _add_symbol(
                    out, defined, fq, method_name, kind,
                    snippet_from_signature(method_name, method) if kind == "function" else method_name,
                    clip(inspect.getdoc(method) or ""),
                    _return_type(method, fq) if kind == "function" else "",
                )
        elif inspect.isfunction(obj) or inspect.isbuiltin(obj):
            _add_symbol(
                out, defined, name, member_name, "function",
                snippet_from_signature(member_name, obj),
                clip(inspect.getdoc(obj) or ""),
                _return_type(obj, name),
            )


def site_import_names(roots: list[str]) -> list[str]:
    names = []
    seen = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            if entry.endswith(".dist-info"):
                top = os.path.join(root, entry, "top_level.txt")
                if not os.path.isfile(top):
                    continue
                try:
                    with open(top, encoding="utf-8", errors="replace") as handle:
                        lines = handle.read().splitlines()
                except OSError:
                    continue
                for line in lines:
                    name = line.strip().split(".")[0]
                    if name.isidentifier() and not name.startswith("_") and name not in seen:
                        seen.add(name)
                        names.append(name)
                continue
            if entry.startswith(".") or entry.endswith((".egg-info", ".pth", ".so", ".dylib")):
                continue
            name = entry[:-3] if entry.endswith(".py") else entry
            if name.isidentifier() and not name.startswith("_") and name not in seen:
                if entry.endswith(".py") or os.path.isdir(os.path.join(root, entry)):
                    seen.add(name)
                    names.append(name)
    return names


def import_site_package(
    name: str, out: list[dict[str, Any]], seen_modules: set[str], defined: set[tuple[str, str]]
) -> None:
    if not hasattr(signal, "SIGALRM"):
        index_c_module(name, out, seen_modules, defined)
        return

    def handler(signum: int, frame: Any) -> None:
        raise TimeoutError(name)

    previous = signal.signal(signal.SIGALRM, handler)
    signal.alarm(SITE_IMPORT_SECONDS)
    try:
        index_c_module(name, out, seen_modules, defined)
    except TimeoutError:
        return
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def stdlib_roots() -> list[str]:
    import sysconfig
    roots = []
    for key in ("stdlib", "platstdlib"):
        path = sysconfig.get_path(key)
        if path and path not in roots and os.path.isdir(path):
            roots.append(path)
    return roots


def site_roots() -> list[str]:
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


def _index_tree(
    files: list[str],
    roots: list[str],
    out: list[dict[str, Any]],
    seen: set[str],
    exports: dict[str, dict[str, Any]],
) -> None:
    for path in files:
        root = owning_root(path, roots)
        if root:
            index_file(root, path, out, seen, exports)


def build(lib: Optional[str] = None) -> list[dict[str, Any]]:
    out = []
    seen = set()
    exports = {}
    if lib:
        lib = os.path.abspath(lib)
        files = []
        collect_files(lib, files, MAX_FILES)
        _index_tree(files, [lib], out, seen, exports)
        link_submodules(out)
        apply_reexports(out, exports)
        return out
    stdlib = stdlib_roots()
    site = site_roots()
    stdlib_files = []
    site_files = []
    for root in stdlib:
        collect_files(root, stdlib_files, MAX_FILES)
    for root in site:
        collect_files(root, site_files, MAX_FILES)
    roots = stdlib + site
    _index_tree(stdlib_files + site_files, roots, out, seen, exports)
    link_submodules(out)
    apply_reexports(out, exports)
    defined = {(rec["owner"], rec["name"]) for rec in out if rec.get("t") == "s"}
    for name in C_MODULES:
        index_c_module(name, out, seen, defined)
    for name in site_import_names(site):
        if name not in C_MODULES:
            import_site_package(name, out, seen, defined)
    return out


def main(argv: list[str]) -> int:
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
