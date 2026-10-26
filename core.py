"""Runtime detection, the symbol cache, and completion lookup.

The cache is a SQLite file outside the package (the package may be a
read-only zip). Python and Ruby fill it from JSONL produced by the scripts
in ``indexers/``. Java fills it from the JDK source scanner.

NOTE: name resolution is textual. ``items = ArrayList(); items.`` completes
only when the right-hand side is a type or module name visible in the file.
There is no type checker. A language server is the upgrade path.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time

# Require paths use "/", Python and Java use ".".
SEP = {"python": ".", "java": ".", "ruby": "/"}

# C types with no .py file. Owners match what python_index.py writes.
PYTHON_BUILTINS = {
    "str": "builtins.str",
    "list": "builtins.list",
    "dict": "builtins.dict",
    "set": "builtins.set",
    "tuple": "builtins.tuple",
    "int": "builtins.int",
    "float": "builtins.float",
    "bool": "builtins.bool",
    "bytes": "builtins.bytes",
    "bytearray": "builtins.bytearray",
    "object": "builtins.object",
    "frozenset": "builtins.frozenset",
    "complex": "builtins.complex",
    "type": "builtins.type",
    "memoryview": "builtins.memoryview",
}

RUBY_BUILTINS = (
    "String", "Array", "Hash", "Integer", "Float", "Symbol", "Regexp",
    "Range", "File", "Dir", "IO", "Time", "Struct", "Proc", "Method",
    "Object", "Kernel", "Numeric", "Complex", "Rational",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    lang TEXT NOT NULL,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    signature TEXT NOT NULL,
    doc TEXT NOT NULL,
    UNIQUE (lang, owner, name, signature)
);
CREATE INDEX IF NOT EXISTS idx_symbols_lookup ON symbols (lang, owner, name);
CREATE TABLE IF NOT EXISTS modules (
    lang TEXT NOT NULL,
    name TEXT NOT NULL,
    doc TEXT NOT NULL,
    UNIQUE (lang, name)
);
CREATE INDEX IF NOT EXISTS idx_modules_lookup ON modules (lang, name);
CREATE TABLE IF NOT EXISTS meta (
    lang TEXT PRIMARY KEY,
    source TEXT,
    version TEXT,
    built_at INTEGER,
    symbol_count INTEGER
);
"""

_KINDS = {"function", "type", "variable", "namespace"}
_RECEIVER_RE = re.compile(r"([A-Za-z_][\w]*(?:(?:\.|::)[A-Za-z_]\w*)*)$")
_SKIP_RECEIVERS = {"self", "this", "super", "cls"}

# NOTE: one language's module list is kept in memory so import completion
# can see every top-level segment, not just the first page of a SQL LIMIT.
# Ceiling is a few tens of thousands of names. Upgrade path: a segment table
# if a language grows past that.
_module_cache = {}


def clip_doc(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:500]


def package_root():
    return os.path.dirname(os.path.abspath(__file__))


def _prefix_end(prefix):
    if not prefix:
        return None
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _connect(path):
    con = sqlite3.connect(path, timeout=2)
    con.execute("PRAGMA busy_timeout = 2000")
    return con


def is_fresh(db_path, lang, source, version):
    if not db_path or not os.path.exists(db_path):
        return False
    try:
        con = _connect(db_path)
        try:
            row = con.execute(
                "SELECT source, version FROM meta WHERE lang = ?", (lang,)
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(row) and row[0] == source and row[1] == version


def read_meta(db_path):
    """Return {lang: {source, version, symbol_count}} for a built cache."""
    if not db_path or not os.path.exists(db_path):
        return {}
    try:
        con = _connect(db_path)
        try:
            rows = con.execute(
                "SELECT lang, source, version, symbol_count FROM meta"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return {
        lang: {"source": source, "version": version, "symbol_count": count}
        for lang, source, version, count in rows
    }


def write_language(db_path, lang, source, version, records):
    """Replace one language in the cache, then swap the file into place.

    ``records`` is an iterable of ``{"t": "m"|"s", ...}`` dicts. The live
    database is replaced with ``os.replace`` only after the write commits,
    so a reader never opens a half-built file.
    """
    directory = os.path.dirname(db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = db_path + ".building"
    if os.path.exists(tmp):
        os.remove(tmp)
    try:
        if os.path.exists(db_path):
            shutil.copy2(db_path, tmp)
        con = _connect(tmp)
        try:
            con.executescript(_SCHEMA)
            con.execute("DELETE FROM symbols WHERE lang = ?", (lang,))
            con.execute("DELETE FROM modules WHERE lang = ?", (lang,))
            sym_rows = []
            mod_rows = []
            for rec in records:
                kind = rec.get("t")
                if kind == "m":
                    name = rec.get("name") or ""
                    if name:
                        mod_rows.append((lang, name, clip_doc(rec.get("doc"))))
                elif kind == "s":
                    owner = rec.get("owner") or ""
                    name = rec.get("name") or ""
                    if not owner or not name:
                        continue
                    sym_kind = rec.get("kind") or "function"
                    if sym_kind not in _KINDS:
                        sym_kind = "function"
                    sig = rec.get("signature") or name
                    sym_rows.append(
                        (lang, owner, name, sym_kind, sig, clip_doc(rec.get("doc")))
                    )
            con.executemany(
                "INSERT OR REPLACE INTO modules (lang, name, doc) VALUES (?, ?, ?)",
                mod_rows,
            )
            con.executemany(
                "INSERT OR REPLACE INTO symbols "
                "(lang, owner, name, kind, signature, doc) VALUES (?, ?, ?, ?, ?, ?)",
                sym_rows,
            )
            con.execute(
                "INSERT OR REPLACE INTO meta "
                "(lang, source, version, built_at, symbol_count) VALUES (?, ?, ?, ?, ?)",
                (lang, source, version, int(time.time()), len(sym_rows)),
            )
            con.commit()
        finally:
            con.close()
        os.replace(tmp, db_path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    _module_cache.clear()
    return len(sym_rows)


def query_symbols(db_path, lang, owner, prefix, limit=80):
    """Members of ``owner`` whose names start with ``prefix``.

    Lookup is ``name >= prefix AND name < prefix_end`` so the
    ``(lang, owner, name)`` index is used.
    """
    if not db_path or not owner or not os.path.exists(db_path):
        return []
    try:
        con = _connect(db_path)
        try:
            if prefix:
                rows = con.execute(
                    "SELECT name, kind, signature, doc FROM symbols "
                    "WHERE lang = ? AND owner = ? AND name >= ? AND name < ? "
                    "ORDER BY name LIMIT ?",
                    (lang, owner, prefix, _prefix_end(prefix), limit),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT name, kind, signature, doc FROM symbols "
                    "WHERE lang = ? AND owner = ? ORDER BY name LIMIT ?",
                    (lang, owner, limit),
                ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return rows


def module_names(db_path, lang):
    """Every importable name for ``lang`` as ``(name, doc)``."""
    if not db_path or not os.path.exists(db_path):
        return []
    try:
        mtime = os.path.getmtime(db_path)
    except OSError:
        return []
    key = (db_path, mtime, lang)
    hit = _module_cache.get(key)
    if hit is not None:
        return hit
    try:
        con = _connect(db_path)
        try:
            rows = con.execute(
                "SELECT name, doc FROM modules WHERE lang = ? ORDER BY name",
                (lang,),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    _module_cache.clear()
    _module_cache[key] = rows
    return rows


def module_exists(db_path, lang, name):
    if not db_path or not name or not os.path.exists(db_path):
        return False
    try:
        con = _connect(db_path)
        try:
            row = con.execute(
                "SELECT 1 FROM modules WHERE lang = ? AND name = ? LIMIT 1",
                (lang, name),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return row is not None


def module_doc(db_path, lang, name):
    if not db_path or not name or not os.path.exists(db_path):
        return ""
    try:
        con = _connect(db_path)
        try:
            row = con.execute(
                "SELECT doc FROM modules WHERE lang = ? AND name = ? LIMIT 1",
                (lang, name),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ""
    return row[0] if row else ""


def symbol_doc(db_path, lang, owner, name):
    if not db_path or not owner or not name or not os.path.exists(db_path):
        return ""
    try:
        con = _connect(db_path)
        try:
            row = con.execute(
                "SELECT doc FROM symbols WHERE lang = ? AND owner = ? AND name = ? "
                "AND doc != '' LIMIT 1",
                (lang, owner, name),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ""
    return row[0] if row else ""


def import_segments(module_rows, typed, sep="."):
    """Next path segment for an import prefix.

    ``typed`` is the partial qualified name, such as ``java.ut`` or
    ``java.util.``. Each result is
    ``{"trigger", "annotation", "kind", "doc"}``. ``trigger`` replaces the
    identifier currently being typed.
    """
    if typed is None:
        return []
    if typed.endswith(sep):
        base = typed
        partial = ""
    elif sep in typed:
        head, partial = typed.rsplit(sep, 1)
        base = head + sep
    else:
        base = ""
        partial = typed

    found = {}
    for name, doc in module_rows:
        if base and not name.startswith(base):
            continue
        rest = name[len(base):]
        if not rest.startswith(partial):
            continue
        segment = rest.split(sep, 1)[0]
        if not segment:
            continue
        more = sep in rest
        exact = name == base + segment
        slot = found.get(segment)
        if slot is None:
            found[segment] = {
                "trigger": segment,
                "annotation": base + segment,
                "kind": "type" if exact and not more else "namespace",
                "doc": doc if exact else "",
                "exact": exact,
                "more": more,
            }
        else:
            if more:
                slot["more"] = True
            if exact:
                slot["exact"] = True
                slot["doc"] = doc or slot["doc"]
                slot["annotation"] = base + segment
    out = []
    for slot in found.values():
        if slot["exact"] and not slot["more"]:
            slot["kind"] = "type"
        elif slot["more"] and not slot["exact"]:
            slot["kind"] = "namespace"
        elif slot["exact"]:
            slot["kind"] = "type"
        del slot["exact"]
        del slot["more"]
        out.append(slot)
    out.sort(key=lambda item: item["trigger"].lower())
    return out


def import_typed(lang, line):
    """Partial import path at the end of ``line``, or None if it is not an import."""
    if lang == "python":
        match = re.match(r"\s*from\s+([\w.]*)\s+import\s+(.*)$", line)
        if match:
            mod, names = match.group(1), match.group(2)
            clause = names.split(",")[-1]
            if re.search(r"\bas\b", clause):
                return None
            last = clause.strip().split()[-1] if clause.strip() else ""
            if mod:
                return f"{mod}.{last}" if last else f"{mod}."
            return last
        match = re.match(r"\s*import\s+(.*)$", line)
        if match:
            clause = match.group(1).split(",")[-1].strip()
            clause = re.split(r"\s+as\s+", clause)[0].strip()
            return clause
        return None
    if lang == "java":
        match = re.match(r"\s*import\s+(?:static\s+)?([\w.]*)$", line)
        if match:
            return match.group(1)
        return None
    if lang == "ruby":
        match = re.match(r"""\s*require(?:_relative)?\s+['"]([^'"]*)$""", line)
        if match:
            return match.group(1)
        return None
    return None


def receiver_from(text):
    """Identifier or dotted name at the end of ``text``, before a dot."""
    match = _RECEIVER_RE.search(text.rstrip())
    if not match:
        return None
    token = match.group(1)
    if token in _SKIP_RECEIVERS or token[-1].isdigit():
        return None
    return token


def _flatten_parens(source):
    out = []
    depth = 0
    for char in source:
        if char == "(":
            depth += 1
            out.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            out.append(char)
        elif char == "\n" and depth:
            out.append(" ")
        else:
            out.append(char)
    return "".join(out)


def python_bindings(source):
    """Map a local name to the module or class it refers to."""
    bound = {}
    text = _flatten_parens(source)
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        match = re.match(r"from\s+([\w.]+)\s+import\s+(.+)$", line)
        if match:
            mod, names = match.group(1), match.group(2)
            if names.strip() == "*":
                # NOTE: star imports are not expanded. Upgrade path: read the
                # indexed module's type symbols when the cache is available.
                continue
            for part in names.split(","):
                part = part.strip()
                found = re.match(r"([\w.]+)(?:\s+as\s+(\w+))?$", part)
                if not found:
                    continue
                name, alias = found.group(1), found.group(2)
                local = alias or name.split(".")[-1]
                bound[local] = f"{mod}.{name}"
            continue
        match = re.match(r"import\s+(.+)$", line)
        if not match:
            continue
        for part in match.group(1).split(","):
            found = re.match(r"([\w.]+)(?:\s+as\s+(\w+))?$", part.strip())
            if not found:
                continue
            mod, alias = found.group(1), found.group(2)
            if alias:
                bound[alias] = mod
            else:
                top = mod.split(".")[0]
                bound[top] = top
    return bound


def python_assigns(source):
    """Map a variable to the type name on the right of ``=`` or ``:``."""
    assigned = {}
    for line in _flatten_parens(source).splitlines():
        raw = line.split("#", 1)[0]
        if re.match(r"\s*(import|from|def|class)\b", raw):
            continue
        stripped = raw.strip()
        ann = re.match(r"(\w+)\s*:\s*([A-Za-z_][\w.]*)\b", stripped)
        if ann:
            assigned[ann.group(1)] = ann.group(2)
        eq = re.match(r"(\w+)\s*=\s*([A-Za-z_][\w.]*)\s*(?:\(|$)", stripped)
        if eq and eq.group(1) != eq.group(2):
            assigned[eq.group(1)] = eq.group(2)
    return assigned


def _resolve_python_expr(bound, expr):
    if expr in PYTHON_BUILTINS:
        return PYTHON_BUILTINS[expr]
    parts = expr.split(".")
    head, rest = parts[0], parts[1:]
    if head in bound:
        base = bound[head]
        return base + ("." + ".".join(rest) if rest else "")
    if head in PYTHON_BUILTINS and not rest:
        return PYTHON_BUILTINS[head]
    return None


def java_imports(source):
    """Return ``(explicit simple->fqcn, star packages, package name)``."""
    explicit = {}
    stars = []
    package = ""
    found = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", source)
    if found:
        package = found.group(1)
    for match in re.finditer(
        r"(?m)^\s*import\s+(?:static\s+)?([\w.]+)(\.\*)?\s*;", source
    ):
        name, star = match.group(1), match.group(2)
        if star:
            stars.append(name)
        else:
            explicit[name.rsplit(".", 1)[-1]] = name
    return explicit, stars, package


def java_vars(source):
    """Map a local or field name to its simple type name."""
    found = {}
    pattern = re.compile(
        r"(?:^|[\s;{(])(?:final\s+)?([A-Z]\w*)(?:\s*<[^>\n]*>)?\s+"
        r"([A-Za-z_]\w*)\s*[=;,):]"
    )
    for match in pattern.finditer(source):
        found[match.group(2)] = match.group(1)
    for match in re.finditer(
        r"\bvar\s+([A-Za-z_]\w*)\s*=\s*new\s+([A-Z]\w*)", source
    ):
        found[match.group(1)] = match.group(2)
    return found


def _java_type_owner(db_path, explicit, stars, package, simple):
    if simple in explicit:
        return explicit[simple]
    for pkg in stars:
        fqcn = f"{pkg}.{simple}"
        if module_exists(db_path, "java", fqcn):
            return fqcn
    lang = f"java.lang.{simple}"
    if module_exists(db_path, "java", lang):
        return lang
    if package:
        fqcn = f"{package}.{simple}"
        if module_exists(db_path, "java", fqcn):
            return fqcn
    return None


def ruby_vars(source):
    found = {}
    for match in re.finditer(
        r"(?m)^\s*(\w+)\s*=\s*([A-Z][\w:]*)\s*(?:\.new\b)?", source
    ):
        found[match.group(1)] = match.group(2)
    return found


def resolve_owner(db_path, lang, source, expr):
    """Qualified owner for the name before a dot, or None."""
    if not expr:
        return None
    if lang == "python":
        bound = python_bindings(source)
        assigned = python_assigns(source)
        if expr in assigned:
            expr = assigned[expr]
        return _resolve_python_expr(bound, expr)
    if lang == "java":
        explicit, stars, package = java_imports(source)
        if "." in expr and module_exists(db_path, "java", expr):
            return expr
        simple = expr
        variables = java_vars(source)
        if simple in variables:
            simple = variables[simple]
        if "." in simple:
            return simple if module_exists(db_path, "java", simple) else None
        return _java_type_owner(db_path, explicit, stars, package, simple)
    if lang == "ruby":
        variables = ruby_vars(source)
        if expr in variables:
            expr = variables[expr]
        if re.match(r"[A-Z]\w*(?:::[A-Z]\w*)*$", expr):
            return expr
        return None
    return None


def bare_names(db_path, lang, source, prefix):
    """Imported and builtin type names starting with ``prefix``.

    Each item is ``(trigger, annotation, kind, doc)``.
    """
    if not prefix:
        return []
    items = {}

    def add(trigger, annotation, kind, doc):
        if trigger.startswith(prefix) and trigger not in items:
            items[trigger] = (trigger, annotation, kind, doc)

    if lang == "python":
        for local, owner in python_bindings(source).items():
            add(local, owner, "namespace", module_doc(db_path, lang, owner))
        for local, owner in PYTHON_BUILTINS.items():
            add(local, owner, "type", module_doc(db_path, lang, owner))
    elif lang == "java":
        explicit, stars, package = java_imports(source)
        for simple, fqcn in explicit.items():
            add(simple, fqcn, "type", module_doc(db_path, lang, fqcn))
        for simple in java_vars(source).values():
            owner = _java_type_owner(db_path, explicit, stars, package, simple)
            if owner:
                add(simple, owner, "type", module_doc(db_path, lang, owner))
        # java.lang is in scope without an import. Prefix-bound the scan.
        needle = "java.lang." + prefix
        end = _prefix_end(needle)
        for name, doc in module_names(db_path, lang):
            if name < needle:
                continue
            if end and name >= end:
                break
            simple = name.rsplit(".", 1)[-1]
            add(simple, name, "type", doc)
    elif lang == "ruby":
        for name in RUBY_BUILTINS:
            add(name, name, "type", module_doc(db_path, lang, name))
        for match in re.finditer(r"(?m)^\s*(?:class|module)\s+([A-Z]\w*)", source):
            add(match.group(1), match.group(1), "type", "")
        for name in ruby_vars(source).values():
            simple = name.split("::")[-1]
            add(simple, name, "type", module_doc(db_path, lang, name))
    return [items[key] for key in sorted(items, key=str.lower)]


def iter_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec


def run_indexer(executable, script_name, timeout=180):
    """Run an indexer script and yield its JSONL records.

    The script is executed by the language's own interpreter so Sublime's
    plugin host never imports user code.
    """
    script = os.path.join(package_root(), "indexers", script_name)
    # The package itself may be a read-only zip, so the JSONL goes to a temp file.
    handle = tempfile.NamedTemporaryFile(prefix="intellekt-", suffix=".jsonl", delete=False)
    out_path = handle.name
    handle.close()
    try:
        proc = subprocess.run(
            [executable, script, "--out", out_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            raise RuntimeError(tail or f"indexer exited {proc.returncode}")
        yield from iter_jsonl(out_path)
    finally:
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass


def _run_version(argv):
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = (proc.stdout or proc.stderr or "").strip()
    return text.splitlines()[0].strip() if text else ""


def detect_python(executable=""):
    exe = executable or shutil.which("python3") or shutil.which("python")
    if not exe or not os.path.isfile(exe):
        return None
    version = _run_version([exe, "-c", "import sys; print(sys.version.split()[0])"])
    if not version:
        return None
    return {"source": exe, "version": version, "executable": exe}


def detect_ruby(executable=""):
    exe = executable or shutil.which("ruby")
    if not exe or not os.path.isfile(exe):
        return None
    version = _run_version([exe, "-e", "print RUBY_VERSION"])
    if not version:
        return None
    return {"source": exe, "version": version, "executable": exe}


def _src_zip(home):
    """Return ``(jdk_home, src_zip)`` for a JDK prefix, including Homebrew."""
    if not home:
        return None, None
    home = os.path.abspath(os.path.expanduser(home))
    direct = os.path.join(home, "lib", "src.zip")
    if os.path.isfile(direct):
        return home, direct
    brew = os.path.join(home, "libexec", "openjdk.jdk", "Contents", "Home")
    brew_src = os.path.join(brew, "lib", "src.zip")
    if os.path.isfile(brew_src):
        return brew, brew_src
    return None, None


def _java_version(home):
    release = os.path.join(home, "release")
    if os.path.isfile(release):
        try:
            with open(release, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith("JAVA_VERSION="):
                        return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
    return "unknown"


def _jdk_from_java_bin(java_bin):
    try:
        java_bin = os.path.realpath(java_bin)
    except OSError:
        return None
    home = os.path.dirname(os.path.dirname(java_bin))
    found, _src = _src_zip(home)
    return found


def detect_java(java_home="", extra_sources=None):
    """Find a JDK that ships ``lib/src.zip``. A JRE is not enough."""
    candidates = []
    if java_home:
        candidates.append(java_home)
    env_home = os.environ.get("JAVA_HOME", "")
    if env_home and env_home not in candidates:
        candidates.append(env_home)
    if os.path.isfile("/usr/libexec/java_home"):
        reported = _run_version(["/usr/libexec/java_home"])
        if reported and reported not in candidates:
            candidates.append(reported)
    java_bin = shutil.which("java")
    if java_bin:
        found = _jdk_from_java_bin(java_bin)
        if found and found not in candidates:
            candidates.append(found)

    home = src = None
    for candidate in candidates:
        home, src = _src_zip(candidate)
        if src:
            break
        resolved = _jdk_from_java_bin(os.path.join(candidate, "bin", "java"))
        if resolved:
            home, src = _src_zip(resolved)
            if src:
                break
    if not src:
        return None

    sources = [src]
    for path in extra_sources or []:
        path = os.path.expanduser(str(path))
        if path and os.path.isfile(path) and path not in sources:
            sources.append(path)
    stamps = []
    for path in sources:
        try:
            stamps.append(f"{path}:{int(os.path.getmtime(path))}")
        except OSError:
            stamps.append(path)
    version = _java_version(home) + "|" + ",".join(stamps)
    return {"source": home, "version": version, "sources": sources}


def detect_all(python_executable="", ruby_executable="", java_home="", java_sources=None):
    return {
        "python": detect_python(python_executable),
        "ruby": detect_ruby(ruby_executable),
        "java": detect_java(java_home, java_sources),
    }


def status_text(db_path, detected):
    meta = read_meta(db_path)
    lines = []
    for lang in ("python", "java", "ruby"):
        info = detected.get(lang)
        if not info:
            lines.append(f"{lang}: not installed")
            continue
        row = meta.get(lang)
        label = info["version"].split("|", 1)[0]
        if not row:
            lines.append(f"{lang}: {label} found, not indexed yet")
        elif row["version"] != info["version"] or row["source"] != info["source"]:
            lines.append(f"{lang}: {label} (index is stale, rebuild it)")
        else:
            lines.append(f"{lang}: {label} — {row['symbol_count']} symbols")
    return "\n".join(lines)
