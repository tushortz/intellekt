"""Index a Python install.

Run with the user's interpreter:

    python3 python_index.py --out symbols.jsonl
    python3 python_index.py --lib ./some/tree --out symbols.jsonl

``.py`` files are parsed with ``ast``. Names imported in ``__init__`` or in a
``TYPE_CHECKING`` block are copied onto the package, which is how external
packages publish names such as ``BaseModel``. C stdlib modules and top-level
site packages are imported with ``inspect`` so compiled libraries complete.
``--lib`` skips the real stdlib, the allowlist, and those site imports.

NOTE: each site-package import is limited to ``SITE_IMPORT_SECONDS``. A
package that exceeds it, or whose import raises, is skipped. Side effects
stay in this process, not in Sublime. Upgrade path: import only the packages
named by the file being edited.

NOTE: the walk stops after ``MAX_FILES`` files per tree (stdlib and
site-packages are counted separately) and skips directories named ``test``,
``tests``, or ``testing``. Upgrade path: index one distribution at a time.
"""

import ast
import importlib
import inspect
import json
import os
import signal
import sys

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


def clip(text):
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


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


def _annotation_text(node):
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


def _decorator_name(dec):
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def module_name(root, path):
    """Return ``(dotted name, is_package)``. ``__init__.py`` is the package."""
    rel = os.path.splitext(os.path.relpath(path, root))[0]
    parts = rel.split(os.sep)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    if not parts or any(part.startswith(".") for part in parts):
        return None, False
    return ".".join(parts), is_package


def owning_root(path, roots):
    """Longest root wins, so a site-packages file is not named ``site-packages.pkg``."""
    matches = [
        item for item in roots
        if path == item or path.startswith(item + os.sep)
    ]
    if not matches:
        return None
    return max(matches, key=len)


def collect_files(root, acc, limit):
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
    def __init__(self, module, out, seen_modules):
        self.owner = module
        self.out = out
        self.seen_modules = seen_modules
        self.in_class = False

    def visit_ClassDef(self, node):
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

    def visit_FunctionDef(self, node):
        self._function(node)

    def visit_AsyncFunctionDef(self, node):
        self._function(node)

    def _function(self, node):
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


def _const_list(node):
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    names = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            names.append(elt.value)
        else:
            return None
    return names


def _is_type_checking(test):
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _relative_module(module, is_package, level, imported):
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


def _collect_exports(tree, module, is_package):
    """Public re-exports. ``all`` is None when the file has no ``__all__``."""
    found = {"all": None, "bindings": []}
    top = module.split(".")[0]

    def handle(stmt):
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


def index_file(root, path, out, seen_modules, exports):
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


def _alias_signature(alias, source):
    sig = source.get("signature") or source["name"]
    src = source["name"]
    if sig.startswith(src):
        return alias + sig[len(src):]
    return alias


def _bind_aliases(module, tree, out):
    """Copy ``name = obj.method`` onto the module.

    ``random.randint`` is not a def. It is ``_inst.randint``. The method
    already indexed on the class is copied so ``from random import randint``
    can see it.

    NOTE: the first same-named function in the file supplies the signature.
    A module that binds the name to a different callable is not told apart.
    Upgrade path: follow the assignment with a type checker.
    """
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


def link_submodules(out):
    """A child module is a member of its parent, so ``pkg.sub`` completes."""
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


def apply_reexports(out, exports):
    """Copy imported names onto the package that re-exports them.

    NOTE: four passes, so a re-export of a re-export is followed that far.
    Deeper chains are dropped. Upgrade path: keep passing until stable.
    """
    by_owner = {}
    defined = set()
    modules = set()
    for rec in out:
        if rec.get("t") == "s":
            by_owner.setdefault(rec["owner"], []).append(rec)
            defined.add((rec["owner"], rec["name"]))
        elif rec.get("t") == "m":
            modules.add(rec["name"])

    def copy_methods(src_owner, dst_owner):
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

    def copy_symbol(src_owner, src_name, dst_owner, dst_name):
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


def _defined_on(cls, name):
    for base in cls.__mro__:
        if name in base.__dict__:
            return base is cls
    return False


def _inherited_data(cls, name):
    """True for a property defined on ``cls`` or a base other than ``object``.

    ``datetime.year`` lives on ``date``. It still has to complete on ``datetime``.
    """
    for base in cls.__mro__:
        if name in base.__dict__:
            return base is not object
    return False


def _member_kind(obj):
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


def _is_classmethod(obj):
    if isinstance(obj, classmethod):
        return True
    text = getattr(obj, "__text_signature__", "") or ""
    return text.startswith("($type,") or text.startswith("($cls,")


def _return_type(obj, owner):
    """Annotated return type, or the class itself for a classmethod.

    NOTE: ``datetime.now`` has no annotation; its text signature starts with
    ``$type``, so the return type is ``datetime``. A classmethod that returns
    a different type needs an annotation.
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


def _add_symbol(out, defined, owner, name, kind, signature, doc, returns=""):
    if (owner, name) in defined:
        return False
    out.append({
        "t": "s", "owner": owner, "name": name,
        "kind": kind, "signature": signature, "doc": doc, "returns": returns,
    })
    defined.add((owner, name))
    return True


def index_c_module(name, out, seen_modules, defined, depth=0):
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


def site_import_names(roots):
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


def import_site_package(name, out, seen_modules, defined):
    if not hasattr(signal, "SIGALRM"):
        index_c_module(name, out, seen_modules, defined)
        return

    def handler(signum, frame):
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


def _index_tree(files, roots, out, seen, exports):
    for path in files:
        root = owning_root(path, roots)
        if root:
            index_file(root, path, out, seen, exports)


def build(lib=None):
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
