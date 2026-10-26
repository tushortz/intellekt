"""Runtime detection, the symbol cache, and completion lookup.

The cache is a SQLite file outside the package (the package may be a
read-only zip). Python, Ruby, JavaScript, and PHP fill it from JSONL
produced by the scripts in ``indexers/``. Java, TypeScript, C#, Go, Rust,
and C++ fill it from the in-process scanners.

NOTE: Python names are resolved from an ``ast`` of the buffer. A line that
does not parse (the one being typed) is replaced with ``pass`` and the parse
is retried. Annotations and recorded return types are used.
``a = datetime.now()`` follows ``now`` when the index has a return type, or
when ``now`` is a classmethod. The other languages stay textual. There is
no type checker. A language server is the upgrade path.
"""

import ast
import html
import json
import os
import re
import sys
import shutil
import sqlite3
import subprocess
import tempfile
import time

# Import paths. Member completion still uses ".", "->", and "::".
SEP = {
    "python": ".",
    "java": ".",
    "ruby": "/",
    "javascript": "/",
    "typescript": "/",
    "csharp": ".",
    "go": "/",
    "php": "\\",
    "rust": "::",
    "cpp": "/",
    "css": ".",
    "html": ".",
    "perl": "::",
    "matlab": ".",
}
# Bumped when the index contents change so an existing cache is rebuilt.
_INDEX_VERSION = "7"

# Status, indexing, and the completion scope walk this list.
LANGS = (
    "python", "java", "ruby",
    "javascript", "typescript", "csharp", "go", "php", "rust", "cpp",
    "css", "html", "perl", "matlab",
)
# source.c++ is before source.c. C files read the C++ index.
LANG_SELECTORS = (
    ("source.python", "python"),
    ("source.java", "java"),
    ("source.ruby", "ruby"),
    ("source.js", "javascript"),
    ("source.jsx", "javascript"),
    ("source.ts", "typescript"),
    ("source.tsx", "typescript"),
    ("source.cs", "csharp"),
    ("source.go", "go"),
    ("source.php", "php"),
    ("source.rust", "rust"),
    ("source.c++", "cpp"),
    ("source.c", "cpp"),
    ("source.css", "css"),
    ("source.matlab", "matlab"),
    ("source.perl", "perl"),
    ("text.html", "html"),
)
# ":" opens completion on "::", ">" on "->".
LANG_TRIGGERS = {
    "python": ".",
    "java": ".",
    "ruby": ".:",
    "javascript": ".",
    "typescript": ".",
    "csharp": ".",
    "go": ".",
    "php": ".>:",
    "rust": ".:",
    "cpp": ".>:",
    "css": ":",
    "html": "<",
    "perl": ".>:",
    "matlab": ".",
}

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

# Owners match the modules go_src.py writes for types with no file of their own.
GO_BUILTINS = {
    name: name for name in (
        "string", "bool", "error", "any", "byte", "rune",
        "int", "int8", "int16", "int32", "int64",
        "uint", "uint8", "uint16", "uint32", "uint64", "uintptr",
        "float32", "float64", "complex64", "complex128",
    )
}

# Prelude paths match rust_src.py: core and alloc, also copied under std::.
RUST_PRELUDE = {
    "Vec": "alloc::vec::Vec",
    "Option": "core::option::Option",
    "Result": "core::result::Result",
    "String": "alloc::string::String",
    "Box": "alloc::boxed::Box",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    lang TEXT NOT NULL,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    signature TEXT NOT NULL,
    doc TEXT NOT NULL,
    returns TEXT NOT NULL DEFAULT '',
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
_RECEIVER_RE = re.compile(
    r"(\$?[A-Za-z_][\w]*(?:(?:\?\.|->|\.|::)\$?[A-Za-z_]\w*)*)$"
)
_SKIP_RECEIVERS = {"self", "this", "super", "cls", "$this"}

# NOTE: one language's module list is kept in memory so import completion
# can see every top-level segment, not just the first page of a SQL LIMIT.
# Ceiling is a few tens of thousands of names. Upgrade path: a segment table
# if a language grows past that.
_module_cache = {}


def clip_doc(text):
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


# NOTE: headings, lists, fences, inline code, bold, italic, and http links.
# Underscore emphasis is skipped so names like some_var stay literal.
# Tables, images, and raw HTML are escaped as text. Upgrade path: a
# CommonMark library, if one is ever vendored.
_FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.S)
_CODE_RE = re.compile(r"``([^`\n]+)``|`([^`\n]+)`")
_CODE_STYLE = (
    "color: var(--orangish); "
    "background-color: color(var(--orangish) blend(var(--background) 86%)); "
    "padding: 0 0.25em; border-radius: 2px;"
)
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BOLD_RE = re.compile(r"(?<![\w*])\*\*(?!\s)(.+?)(?<!\s)\*\*(?![\w*])")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s|\*)(.+?)(?<!\s|\*)\*(?![\w*])")
_LIST_RE = re.compile(r"\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_LIST_START_RE = re.compile(r"\s*(?:[-*+]|\d+[.)])\s+\S")
_ORDERED_RE = re.compile(r"\s*\d+[.)]\s+")


def markdown_html(text):
    """Docstring markdown as minihtml for a hover popup."""
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return ""
    chunks = []
    pos = 0
    for match in _FENCE_RE.finditer(text):
        chunks.extend(_md_blocks(text[pos:match.start()]))
        chunks.append(_code_html(match.group(1).strip("\n"), block=True))
        pos = match.end()
    chunks.extend(_md_blocks(text[pos:]))
    return "<br><br>".join(chunk for chunk in chunks if chunk)


def _code_html(text, block=False):
    body = html.escape(text, quote=False)
    if block:
        return (
            '<pre style="background-color: color(var(--foreground) blend(var(--background) 88%%)); '
            'padding: 0.35em 0.5em; margin: 0.15em 0;">'
            '<code style="color: var(--orangish);">%s</code></pre>' % body
        )
    return '<code style="%s">%s</code>' % (_CODE_STYLE, body)


def _md_inline(raw):
    parts = []
    pos = 0
    for match in _CODE_RE.finditer(raw):
        parts.append(_md_format(html.escape(raw[pos:match.start()], quote=True)))
        parts.append(_code_html(match.group(1) or match.group(2)))
        pos = match.end()
    parts.append(_md_format(html.escape(raw[pos:], quote=True)))
    return "".join(parts)


def _md_format(text):
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    return _ITALIC_RE.sub(r"<i>\1</i>", text)


def _md_blocks(raw):
    lines = raw.split("\n")
    blocks = []
    i = 0
    n = len(lines)
    while i < n:
        if not lines[i].strip():
            i += 1
            continue
        heading = re.match(r"#{1,6}\s+(.*)$", lines[i].strip())
        if heading:
            blocks.append("<b>%s</b>" % _md_inline(heading.group(1).strip()))
            i += 1
            continue
        if _LIST_START_RE.match(lines[i]):
            ordered = bool(_ORDERED_RE.match(lines[i]))
            items = []
            while i < n and _LIST_START_RE.match(lines[i]):
                item = _LIST_RE.match(lines[i])
                pieces = [item.group(1).strip()]
                i += 1
                while i < n and lines[i].startswith("  ") and lines[i].strip() and not _LIST_START_RE.match(lines[i]):
                    pieces.append(lines[i].strip())
                    i += 1
                items.append(" ".join(pieces))
            tag = "ol" if ordered else "ul"
            blocks.append(
                "<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % _md_inline(item) for item in items), tag)
            )
            continue
        para = []
        while i < n and lines[i].strip():
            stripped = lines[i].strip()
            if para and (re.match(r"#{1,6}\s+\S", stripped) or _LIST_START_RE.match(lines[i])):
                break
            para.append(stripped)
            i += 1
        blocks.append(_md_inline(" ".join(para)))
    return blocks


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


def _ensure_schema(con):
    con.executescript(_SCHEMA)
    # A cache built before return types existed has the old symbols table.
    cols = [row[1] for row in con.execute("PRAGMA table_info(symbols)")]
    if "returns" not in cols:
        con.execute("ALTER TABLE symbols ADD COLUMN returns TEXT NOT NULL DEFAULT ''")


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
            _ensure_schema(con)
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
                    sym_rows.append((
                        lang, owner, name, sym_kind, sig, clip_doc(rec.get("doc")),
                        rec.get("returns") or "",
                    ))
            con.executemany(
                "INSERT OR REPLACE INTO modules (lang, name, doc) VALUES (?, ?, ?)",
                mod_rows,
            )
            con.executemany(
                "INSERT OR REPLACE INTO symbols "
                "(lang, owner, name, kind, signature, doc, returns) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
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


def query_symbols(db_path, lang, owner, prefix, limit=80, public_only=False):
    """Members of ``owner`` whose names start with ``prefix``.

    Lookup is ``name >= prefix AND name < prefix_end`` so the
    ``(lang, owner, name)`` index is used. ``public_only`` drops names
    that start with ``_`` unless ``prefix`` itself does.
    """
    if not db_path or not owner or not os.path.exists(db_path):
        return []
    hide_private = public_only and not (prefix or "").startswith("_")
    try:
        con = _connect(db_path)
        try:
            if prefix:
                sql = (
                    "SELECT name, kind, signature, doc FROM symbols "
                    "WHERE lang = ? AND owner = ? AND name >= ? AND name < ?"
                )
                args = [lang, owner, prefix, _prefix_end(prefix)]
            else:
                sql = (
                    "SELECT name, kind, signature, doc FROM symbols "
                    "WHERE lang = ? AND owner = ?"
                )
                args = [lang, owner]
            if hide_private:
                sql += " AND name NOT GLOB '_*'"
            sql += " ORDER BY name LIMIT ?"
            args.append(limit)
            rows = con.execute(sql, args).fetchall()
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


def symbol_info(db_path, lang, owner, name):
    """``(kind, returns)`` for one symbol, or None."""
    if not db_path or not owner or not name or not os.path.exists(db_path):
        return None
    try:
        con = _connect(db_path)
        try:
            try:
                row = con.execute(
                    "SELECT kind, returns FROM symbols "
                    "WHERE lang = ? AND owner = ? AND name = ? LIMIT 1",
                    (lang, owner, name),
                ).fetchone()
            except sqlite3.OperationalError:
                row = con.execute(
                    "SELECT kind FROM symbols "
                    "WHERE lang = ? AND owner = ? AND name = ? LIMIT 1",
                    (lang, owner, name),
                ).fetchone()
                if row:
                    return (row[0], "")
                return None
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return (row[0], row[1] or "")


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


def from_import_segments(module_rows, symbol_rows, typed, sep="."):
    """Import-path segments plus names defined on that module.

    ``from random import`` is typed as ``random.``. ``randint`` lives in
    ``symbols``, not ``modules``, so path segments alone never offer it.
    A name already offered as a module is left as that segment. Names
    starting with ``_`` are skipped unless the typed prefix does too.
    """
    segments = import_segments(module_rows, typed, sep)
    if not typed or sep not in typed:
        return segments
    if typed.endswith(sep):
        owner = typed[:-len(sep)]
        partial = ""
    else:
        owner, partial = typed.rsplit(sep, 1)
    if not owner:
        return segments
    have = {item["trigger"] for item in segments}
    extra = []
    for name, kind, _signature, doc in symbol_rows:
        if not name or name in have:
            continue
        if name.startswith("_") and not partial.startswith("_"):
            continue
        if partial and not name.startswith(partial):
            continue
        extra.append({
            "trigger": name,
            "annotation": owner + sep + name,
            "kind": kind or "function",
            "doc": doc or "",
        })
    if not extra:
        return segments
    return sorted(segments + extra, key=lambda item: item["trigger"].lower())


def import_typed(lang, line):
    """Partial import path at the end of ``line``, or None if it is not an import."""
    if lang == "python":
        match = re.match(r"\s*from\s+([\w.]*)\s+import\b\s*(.*)$", line)
        if match:
            mod, names = match.group(1), match.group(2)
            clause = names.split(",")[-1]
            if re.search(r"\bas\b", clause):
                return None
            last = clause.strip().split()[-1] if clause.strip() else ""
            if mod:
                return f"{mod}.{last}" if last else f"{mod}."
            return last
        # ``from collections`` or ``from collections.`` — the package path,
        # before ``import`` is typed. ``""`` means ``from `` with no name yet.
        match = re.match(r"\s*from\s+([\w.]*)$", line)
        if match:
            return match.group(1)
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
    if lang in ("javascript", "typescript"):
        match = re.search(r"""(?:from\s+|require\(\s*|import\s+)['"]([^'"]*)$""", line)
        if match:
            return match.group(1)
        return None
    if lang == "csharp":
        match = re.match(r"\s*using\s+(?:static\s+)?([\w.]*)$", line)
        if match:
            return match.group(1)
        return None
    if lang == "go":
        if re.match(r'\s*(?:import\b.*)?["][^"]*$', line):
            match = re.search(r'"([^"]*)$', line)
            if match:
                return match.group(1)
        return None
    if lang == "php":
        match = re.match(r"\s*use\s+([\w\\]*)$", line)
        if match:
            return match.group(1)
        return None
    if lang == "rust":
        match = re.match(r"\s*use\s+([\w:]*)(?:\{([\w:]*))?\s*$", line)
        if not match:
            return None
        if match.group(2) is not None:
            return (match.group(1) or "") + match.group(2)
        return match.group(1)
    if lang == "cpp":
        match = re.search(r"""#\s*include\s*[<"]([^>"]*)$""", line)
        if match:
            return match.group(1)
        return None
    if lang == "perl":
        match = re.search(r"\b(?:use|require)\s+([\w:]*)$", line)
        if match:
            return match.group(1)
        return None
    return None


def css_value(line):
    """``(property, partial)`` when ``line`` is completing a CSS value."""
    match = re.search(
        r"(?:^|[;{])\s*([A-Za-z-]{2,})\s*:\s*([A-Za-z-]*)$", line or ""
    )
    if not match:
        return None
    return match.group(1).lower(), match.group(2).lower()


def html_context(line):
    """``('tag', partial)`` or ``('attr', tag, partial)`` inside a start tag."""
    line = line or ""
    match = re.search(r"<([A-Za-z][\w:-]*)$", line)
    if match:
        return ("tag", match.group(1).lower())
    match = re.search(r"<([A-Za-z][\w:-]*)\b([^<>]*\s)([A-Za-z:-]*)$", line)
    if match:
        return ("attr", match.group(1).lower(), match.group(3).lower())
    return None


def perl_assigns(source):
    assigned = {}
    calls = set()
    for match in re.finditer(r"\$(\w+)\s*=\s*([\w:]+)\s*->", source or ""):
        name = "$" + match.group(1)
        assigned[name] = match.group(2)
        calls.add(name)
    return assigned, calls


def perl_imports(source):
    """Names brought in by ``use Module qw(...)``."""
    bound = {}
    for match in re.finditer(
        r"(?m)^\s*use\s+([\w:]+)\s+qw\s*[(\[]([^)\]]+)", source or ""
    ):
        for name in match.group(2).split():
            if name.isidentifier():
                bound[name] = match.group(1)
    return bound


def matlab_assigns(source):
    assigned = {}
    calls = set()
    for match in re.finditer(
        r"(?m)^\s*(\w+)\s*=\s*([A-Za-z_]\w*)\s*\(", source or ""
    ):
        assigned[match.group(1)] = match.group(2)
        calls.add(match.group(1))
    return assigned, calls


def receiver_from(text):
    """Identifier or dotted name at the end of ``text``, before a member operator."""
    match = _RECEIVER_RE.search(text.rstrip())
    if not match:
        return None
    token = match.group(1)
    if token in _SKIP_RECEIVERS or token[-1].isdigit():
        return None
    return token


def member_op(text):
    """Return ``(left, op)`` when ``text`` ends with ``.``, ``?.``, ``->``, or ``::``."""
    text = (text or "").rstrip()
    for op in ("?.", "->", "::", "."):
        if text.endswith(op):
            return text[:-len(op)], op
    return None, None


# list[int] and X | Y parse on the user's interpreter. Sublime's plugin host
# is 3.8, which rejects both, so a failed parse is rewritten into forms 3.8
# accepts and tried again.
_GENERIC_WORDS = ("list", "dict", "set", "tuple", "frozenset", "type")
_UNION_RE = re.compile(r"([A-Za-z_][\w.]*)\s*\|\s*([A-Za-z_][\w.]*)")
_CONTAINERS = {
    "list", "set", "frozenset", "Iterable", "Sequence", "Collection",
}
_ALIAS = {
    "List": "list", "Set": "set", "Tuple": "tuple", "Dict": "dict",
    "FrozenSet": "frozenset", "Frozenset": "frozenset",
}
_AST_INDEX = getattr(ast, "Index", ())


def _rewrite_unions(source):
    """Turn ``X | Y`` into ``Union[X, Y]`` so Python 3.8 can parse it."""
    def one(line):
        previous = None
        while previous != line:
            previous = line
            line = _UNION_RE.sub(r"Union[\1, \2]", line)
        return line
    return "\n".join(one(line) for line in source.splitlines())


def _rewrite_generics(source):
    """Turn ``list[int]`` into ``List[int]`` so Python 3.8 can parse it."""
    def one(line):
        for word in _GENERIC_WORDS:
            line = re.sub(r"\b%s\[" % word, word.capitalize() + "[", line)
        return line
    return "\n".join(one(line) for line in source.splitlines())


def _relax_syntax(source):
    if sys.version_info < (3, 10):
        source = _rewrite_unions(source)
    if sys.version_info < (3, 9):
        source = _rewrite_generics(source)
    return source


def _parse_python(source):
    """Parse ``source``. A few unparseable lines are replaced with ``pass``.

    NOTE: at most six broken lines are repaired. Upgrade path: parse only the
    statements above the caret.
    """
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        pass
    source = _relax_syntax(source)
    lines = source.splitlines() or [""]
    for _ in range(6):
        try:
            return ast.parse("\n".join(lines))
        except SyntaxError as err:
            if not err.lineno or err.lineno < 1 or err.lineno > len(lines):
                return None
            indent = re.match(r"[ \t]*", lines[err.lineno - 1]).group(0)
            replacement = indent + "pass"
            if lines[err.lineno - 1] == replacement:
                return None
            lines[err.lineno - 1] = replacement
        except ValueError:
            return None
    return None


def _slice(node):
    if _AST_INDEX and isinstance(node, _AST_INDEX):
        return node.value
    return node


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        if not base:
            return ""
        return base + "." + node.attr
    return ""


def _parse_expr(text):
    if not text:
        return None
    try:
        return ast.parse(text, mode="eval").body
    except (SyntaxError, ValueError):
        pass
    if sys.version_info < (3, 10) and "|" in text:
        try:
            return ast.parse(_relax_syntax(text), mode="eval").body
        except (SyntaxError, ValueError):
            return None
    return None


def _is_ellipsis(node):
    if isinstance(node, ast.Constant):
        return node.value is Ellipsis
    return type(node).__name__ == "Ellipsis"


def _union_parts(node):
    elts = node.elts if isinstance(node, ast.Tuple) else [node]
    found = []
    for elt in elts:
        name, element = _ann_parts(elt)
        if name not in ("", "None", "NoneType"):
            found.append((name, element))
    if len(found) == 1:
        return found[0]
    return "", ""


def _ann_parts(node):
    """``(type name, element name)`` from an annotation node.

    ``datetime | None`` and ``Optional[datetime]`` are ``datetime``.
    ``list[datetime]`` is ``(list, datetime)``. Two real types in one union
    are left unresolved.
    """
    if node is None:
        return "", ""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            parsed = _parse_expr(node.value.strip())
            if parsed is None:
                return "", ""
            return _ann_parts(parsed)
        if node.value is None:
            return "None", ""
        return "", ""
    if isinstance(node, ast.Name):
        return _ALIAS.get(node.id, node.id), ""
    if isinstance(node, ast.Attribute):
        return _dotted(node), ""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left_name, left_el = _ann_parts(node.left)
        right_name, right_el = _ann_parts(node.right)
        if right_name in ("", "None", "NoneType"):
            return left_name, left_el
        if left_name in ("", "None", "NoneType"):
            return right_name, right_el
        return "", ""
    if isinstance(node, ast.Subscript):
        outer = _dotted(node.value)
        simple = _ALIAS.get(outer.rsplit(".", 1)[-1], outer.rsplit(".", 1)[-1]) if outer else ""
        sl = _slice(node.slice)
        if simple == "Optional":
            return _ann_parts(sl)
        if simple == "Union":
            return _union_parts(sl)
        element = ""
        if simple in _CONTAINERS:
            first = sl.elts[0] if isinstance(sl, ast.Tuple) else sl
            element = _ann_parts(first)[0]
            return (simple if simple in PYTHON_BUILTINS else outer), element
        if simple == "tuple":
            if isinstance(sl, ast.Tuple):
                elts = sl.elts
                if len(elts) == 1 or (len(elts) == 2 and _is_ellipsis(elts[1])):
                    element = _ann_parts(elts[0])[0]
            else:
                element = _ann_parts(sl)[0]
            return "tuple", element
        return outer, ""
    return "", ""


def _expr_name(node):
    """``(dotted name, is_call)`` for an assignment value."""
    if isinstance(node, ast.Call):
        return _dotted(node.func), True
    return _dotted(node), False


def _arg_nodes(args):
    nodes = list(getattr(args, "posonlyargs", ()))
    nodes.extend(args.args)
    nodes.extend(args.kwonlyargs)
    return nodes


class _PyFacts(ast.NodeVisitor):
    """Imports, assignments, and annotations in one buffer."""

    def __init__(self):
        self.bound = {}
        self.assigned = {}
        self.calls = set()
        self.annotated = {}
        self.elements = {}

    def visit_Import(self, node):
        for alias in node.names:
            if alias.asname:
                self.bound[alias.asname] = alias.name
            else:
                top = alias.name.split(".")[0]
                self.bound[top] = top

    def visit_ImportFrom(self, node):
        if node.level or not node.module:
            return
        for alias in node.names:
            if alias.name == "*":
                # NOTE: star imports are not expanded. Upgrade path: read the
                # indexed module's type symbols when the cache is available.
                continue
            local = alias.asname or alias.name.split(".")[-1]
            self.bound[local] = node.module + "." + alias.name

    def visit_FunctionDef(self, node):
        self._function(node)

    def visit_AsyncFunctionDef(self, node):
        self._function(node)

    def _function(self, node):
        for arg in _arg_nodes(node.args):
            if arg.annotation is not None:
                self._annotate(arg.arg, arg.annotation)
        for stmt in node.body:
            self.visit(stmt)

    def visit_ClassDef(self, node):
        for stmt in node.body:
            self.visit(stmt)

    def visit_AnnAssign(self, node):
        if not isinstance(node.target, ast.Name):
            return
        if node.annotation is not None:
            self._annotate(node.target.id, node.annotation)
        if node.value is not None:
            self._assign(node.target.id, node.value)

    def visit_Assign(self, node):
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._assign(target.id, node.value)

    def visit_For(self, node):
        self._for(node)

    def visit_AsyncFor(self, node):
        self._for(node)

    def _for(self, node):
        if (
            isinstance(node.target, ast.Name)
            and isinstance(node.iter, ast.Name)
            and node.target.id not in self.annotated
            and node.target.id not in self.assigned
        ):
            element = self.elements.get(node.iter.id)
            if element:
                self.annotated[node.target.id] = element
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

    def _annotate(self, name, node):
        if name in _SKIP_RECEIVERS:
            return
        type_name, element = _ann_parts(node)
        if not type_name:
            return
        self.annotated[name] = type_name
        if element:
            self.elements[name] = element
        else:
            self.elements.pop(name, None)

    def _assign(self, name, value):
        if name in _SKIP_RECEIVERS:
            return
        expr, is_call = _expr_name(value)
        if not expr or expr == name:
            return
        self.assigned[name] = expr
        if is_call:
            self.calls.add(name)
        else:
            self.calls.discard(name)


def _python_facts(source):
    facts = _PyFacts()
    tree = _parse_python(source or "")
    if tree is not None:
        facts.visit(tree)
    return facts


def python_bindings(source):
    """Map a local name to the module or class it refers to."""
    return _python_facts(source).bound


def _return_owner(db_path, owner, returns):
    """Qualified class for a stored return annotation."""
    returns = (returns or "").strip()
    if not returns or returns in ("None", "NoneType"):
        return ""
    if returns in ("Self", "self", "cls") or returns == owner:
        return owner
    if "." in returns:
        return returns
    if returns in PYTHON_BUILTINS:
        return PYTHON_BUILTINS[returns]
    module = owner.rsplit(".", 1)[0] if "." in owner else ""
    if module and module_exists(db_path, "python", module + "." + returns):
        return module + "." + returns
    if owner.endswith("." + returns):
        return owner
    return ""


def _call_result(db_path, qualified):
    """Instance type of a call, when the callee is a class or a typed function."""
    if module_exists(db_path, "python", qualified):
        return qualified
    if "." not in qualified:
        return ""
    owner, name = qualified.rsplit(".", 1)
    info = symbol_info(db_path, "python", owner, name)
    if not info or info[0] != "function" or not info[1]:
        return ""
    return _return_owner(db_path, owner, info[1])


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


def _split_member(qualified):
    positions = []
    for sep in ("::", ".", "\\"):
        index = qualified.rfind(sep)
        if index > 0:
            positions.append((index, sep))
    if not positions:
        return "", qualified
    index, sep = max(positions)
    return qualified[:index], qualified[index + len(sep):]


def _member_ref(db_path, lang, qualified, called):
    """Owner of a type, or the return type of a call."""
    if module_exists(db_path, lang, qualified):
        return qualified
    owner, name = _split_member(qualified)
    if not owner or not name:
        return None
    info = symbol_info(db_path, lang, owner, name)
    if not info:
        return None
    if called and info[1]:
        return info[1]
    if info[0] == "type":
        return qualified
    return None


def _qualify(expr, bound, prelude=None):
    names = dict(prelude or ())
    names.update(bound or ())
    for sep in ("::", "\\", "."):
        if sep in expr:
            head, rest = expr.split(sep, 1)
            if head in names:
                return names[head] + sep + rest
            return expr
    return names.get(expr, expr)


def _resolve_script(db_path, lang, expr, bound, assigned, calls, prelude=None):
    called = False
    if expr in assigned:
        called = expr in calls
        expr = assigned[expr]
    expr = _qualify(expr, bound, prelude)
    if not any(sep in expr for sep in (".", "::", "\\")):
        info = symbol_info(db_path, lang, "global", expr)
        if info and info[0] != "function" and info[1]:
            return info[1]
    found = _member_ref(db_path, lang, expr, called)
    if found:
        return found
    if module_exists(db_path, lang, expr):
        return expr
    return None


def js_bindings(source):
    bound = {}
    patterns = (
        r"""import\s+\*\s+as\s+(\w+)\s+from\s+['"]([^'"]+)['"]""",
        r"""import\s+(\w+)\s+from\s+['"]([^'"]+)['"]""",
        r"""(?:const|let|var)\s+(\w+)\s*=\s*require\(\s*['"]([^'"]+)['"]\s*\)""",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, source):
            bound[match.group(1)] = match.group(2)
    for match in re.finditer(
        r"""import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"]""", source
    ):
        mod = match.group(2)
        for part in match.group(1).split(","):
            found = re.match(r"\s*(\w+)(?:\s+as\s+(\w+))?\s*$", part.strip())
            if not found:
                continue
            local = found.group(2) or found.group(1)
            bound[local] = mod + "." + found.group(1)
    return bound


def js_assigns(source):
    assigned = {}
    calls = set()
    for match in re.finditer(
        r"(?:const|let|var)\s+(\w+)\s*(?::\s*([A-Za-z_][\w.]*))?\s*=\s*(?:new\s+)?"
        r"([A-Za-z_][\w.]*)\s*(\()?",
        source,
    ):
        name, annotation, rhs, call = match.groups()
        if rhs in ("require", "function", "async", "await", "class", "import"):
            if annotation:
                assigned[name] = annotation
            continue
        if annotation:
            assigned[name] = annotation
        else:
            assigned[name] = rhs
            if call or "new " in match.group(0):
                calls.add(name)
    for match in re.finditer(
        r"(?:const|let|var)\s+(\w+)\s*:\s*([A-Za-z_][\w.]*)", source
    ):
        assigned.setdefault(match.group(1), match.group(2))
    return assigned, calls


def cs_uses(source):
    """Namespaces in scope.

    NOTE: ``System`` is always included. That is wrong for a project that
    disables implicit usings.
    """
    found = ["System"]
    for match in re.finditer(r"(?m)^\s*using\s+(?:static\s+)?([\w.]+)\s*;", source):
        name = match.group(1)
        if name not in found:
            found.append(name)
    return found


def cs_assigns(source):
    """Map a local to its type name.

    NOTE: a nested generic (``Dictionary<string, List<int>>``) is not read.
    Upgrade path: a bracket matcher.
    """
    assigned = {}
    calls = set()
    for match in re.finditer(
        r"(?:^|[\s;{(])([A-Z][\w.]*)(?:\s*<[^>\n]*>)?\s+([A-Za-z_]\w*)\s*[=;,)]",
        source,
    ):
        assigned[match.group(2)] = match.group(1)
    for match in re.finditer(r"\bvar\s+(\w+)\s*=\s*new\s+([A-Z]\w*)", source):
        assigned[match.group(1)] = match.group(2)
        calls.add(match.group(1))
    for match in re.finditer(r"\bvar\s+(\w+)\s*=\s*([A-Za-z_][\w.]*)\s*\(", source):
        assigned.setdefault(match.group(1), match.group(2))
        calls.add(match.group(1))
    return assigned, calls


def _cs_owner(db_path, namespaces, simple):
    if not simple:
        return None
    if "." in simple and module_exists(db_path, "csharp", simple):
        return simple
    leaf = simple.split(".")[-1]
    for namespace in namespaces:
        full = namespace + "." + leaf
        if module_exists(db_path, "csharp", full):
            return full
    if module_exists(db_path, "csharp", leaf):
        return leaf
    return None


def _resolve_csharp(db_path, source, expr):
    uses = cs_uses(source)
    assigned, calls = cs_assigns(source)
    called = False
    if expr in assigned:
        called = expr in calls
        expr = assigned[expr]
    if called and "." in expr:
        found = _member_ref(db_path, "csharp", expr, True)
        if found:
            return found
    return _cs_owner(db_path, uses, expr)


def go_bindings(source):
    """Map the local name to the import path.

    NOTE: a dot-import (``import . "fmt"``) is not expanded. Upgrade path:
    treat that package's exported names as bare names.
    """
    bound = {}

    def take(alias, path):
        if not path or alias == "_":
            return
        if alias == ".":
            return
        bound[alias or path.rsplit("/", 1)[-1]] = path

    for match in re.finditer(r'(?m)^\s*import\s+(?:(\.|_|\w+)\s+)?"([^"]+)"', source):
        take(match.group(1), match.group(2))
    for block in re.finditer(r"(?m)^import\s*\((.*?)\)", source, re.S):
        for match in re.finditer(r'(?:(\.|_|\w+)\s+)?"([^"]+)"', block.group(1)):
            take(match.group(1), match.group(2))
    return bound


def go_assigns(source):
    assigned = {}
    calls = set()
    for match in re.finditer(
        r"(?m)^\s*(\w+)\s*:?=\s*([A-Za-z_][\w.]*)\s*(\(|\{)?", source
    ):
        name, rhs, tail = match.groups()
        assigned[name] = rhs
        if tail == "(":
            calls.add(name)
    for match in re.finditer(r"(?m)^\s*var\s+(\w+)\s+([A-Za-z_][\w.]*)", source):
        if match.group(1) not in assigned:
            assigned[match.group(1)] = match.group(2)
    return assigned, calls


def php_bindings(source):
    bound = {}
    for match in re.finditer(
        r"(?m)^\s*use\s+([\w\\]+)(?:\s+as\s+(\w+))?\s*;", source
    ):
        full, alias = match.group(1), match.group(2)
        bound[alias or full.split("\\")[-1]] = full
    return bound


def php_assigns(source):
    assigned = {}
    calls = set()
    for match in re.finditer(r"\$(\w+)\s*=\s*new\s+\\?([\w\\]+)", source):
        assigned[match.group(1)] = match.group(2)
        assigned["$" + match.group(1)] = match.group(2)
    for match in re.finditer(r"\$(\w+)\s*=\s*\\?([\w\\]+)::(\w+)\s*\(", source):
        target = match.group(2) + "::" + match.group(3)
        assigned[match.group(1)] = target
        assigned["$" + match.group(1)] = target
        calls.add(match.group(1))
        calls.add("$" + match.group(1))
    return assigned, calls


def rust_bindings(source):
    bound = {}
    for match in re.finditer(r"(?m)^\s*use\s+([^;]+);", source):
        clause = match.group(1).strip()
        group = re.match(r"([\w:]+)::\{([^}]+)\}", clause)
        if group:
            base, names = group.group(1), group.group(2)
            for part in names.split(","):
                part = part.strip()
                if not part or part == "self":
                    continue
                if " as " in part:
                    orig, local = [piece.strip() for piece in part.split(" as ", 1)]
                else:
                    orig = local = part
                bound[local] = base + "::" + orig
            continue
        if " as " in clause:
            orig, local = [piece.strip() for piece in clause.split(" as ", 1)]
            bound[local] = orig
            continue
        bound[clause.split("::")[-1]] = clause
    return bound


def rust_assigns(source):
    assigned = {}
    calls = set()
    for match in re.finditer(
        r"\blet\s+(?:mut\s+)?(\w+)\s*(?::\s*([A-Za-z_][\w:]*))?\s*=\s*"
        r"([A-Za-z_][\w:]*)\s*(\(|::|\{)?",
        source,
    ):
        name, annotation, rhs, tail = match.groups()
        if annotation:
            assigned[name] = annotation
        else:
            assigned[name] = rhs
            if tail in ("(", "::"):
                calls.add(name)
    for match in re.finditer(
        r"\blet\s+(?:mut\s+)?(\w+)\s*:\s*([A-Za-z_][\w:]*)", source
    ):
        assigned.setdefault(match.group(1), match.group(2))
    return assigned, calls


def cpp_assigns(source):
    """Map a local to a ``std::`` type or a capitalized type.

    NOTE: nested templates (``map<int, vector<int>>``) are not read.
    Upgrade path: a bracket matcher.
    """
    assigned = {}
    pattern = re.compile(
        r"(?:^|[\s;{(])([A-Za-z_][\w:]*)(?:\s*<[^>;\n]*>)?\s*[*&]*\s+"
        r"([A-Za-z_]\w*)\s*[=;,)]"
    )
    for match in pattern.finditer(source):
        type_name, var = match.group(1), match.group(2)
        if type_name in ("return", "if", "for", "while", "switch", "catch", "else", "throw"):
            continue
        if "::" not in type_name and not type_name[:1].isupper():
            continue
        assigned[var] = type_name
    return assigned


def _resolve_cpp(db_path, source, expr):
    assigned = cpp_assigns(source)
    if expr in assigned:
        expr = assigned[expr]
    if module_exists(db_path, "cpp", expr):
        return expr
    return _member_ref(db_path, "cpp", expr, False)


def _types_under(db_path, lang, namespace, prefix, add, sep="."):
    needle = namespace + sep + prefix
    end = _prefix_end(needle)
    base = namespace + sep
    for name, doc in module_names(db_path, lang):
        if name < needle:
            continue
        if end and name >= end:
            break
        if not name.startswith(base):
            continue
        rest = name[len(base):]
        if sep in rest:
            continue
        add(rest, name, "type", doc)


def resolve_owner(db_path, lang, source, expr):
    """Qualified owner for the name before a dot, or None."""
    if not expr:
        return None
    if lang == "python":
        facts = _python_facts(source)
        bound = facts.bound
        assigned, calls, annotated = facts.assigned, facts.calls, facts.annotated
        if expr in assigned:
            resolved = _resolve_python_expr(bound, assigned[expr]) or ""
            if expr in calls and resolved:
                followed = _call_result(db_path, resolved)
                if followed:
                    return followed
                # A call with no return type is not a class. The annotation is.
                info = None
                if "." in resolved and not module_exists(db_path, "python", resolved):
                    owner, name = resolved.rsplit(".", 1)
                    info = symbol_info(db_path, "python", owner, name)
                if info and info[0] == "function":
                    resolved = ""
            if resolved:
                return resolved
        if expr in annotated:
            return _resolve_python_expr(bound, annotated[expr])
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
    if lang in ("javascript", "typescript"):
        assigned, calls = js_assigns(source)
        return _resolve_script(db_path, lang, expr, js_bindings(source), assigned, calls)
    if lang == "csharp":
        return _resolve_csharp(db_path, source, expr)
    if lang == "go":
        assigned, calls = go_assigns(source)
        return _resolve_script(
            db_path, lang, expr, go_bindings(source), assigned, calls, GO_BUILTINS,
        )
    if lang == "php":
        assigned, calls = php_assigns(source)
        return _resolve_script(db_path, lang, expr, php_bindings(source), assigned, calls)
    if lang == "rust":
        assigned, calls = rust_assigns(source)
        return _resolve_script(
            db_path, lang, expr, rust_bindings(source), assigned, calls, RUST_PRELUDE,
        )
    if lang == "cpp":
        return _resolve_cpp(db_path, source, expr)
    if lang == "perl":
        assigned, calls = perl_assigns(source)
        return _resolve_script(db_path, lang, expr, {}, assigned, calls)
    if lang == "matlab":
        assigned, calls = matlab_assigns(source)
        return _resolve_script(db_path, lang, expr, {}, assigned, calls)
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
    elif lang in ("javascript", "typescript"):
        for local, owner in js_bindings(source).items():
            add(local, owner, "namespace", module_doc(db_path, lang, owner))
        for name, kind, signature, doc in query_symbols(db_path, lang, "global", prefix, 40):
            add(name, signature or name, kind, doc)
    elif lang == "csharp":
        uses = cs_uses(source)
        for namespace in uses:
            _types_under(db_path, lang, namespace, prefix, add)
        for simple in cs_assigns(source)[0].values():
            owner = _cs_owner(db_path, uses, simple)
            if owner:
                add(simple.split(".")[-1], owner, "type", module_doc(db_path, lang, owner))
    elif lang == "go":
        for local, owner in go_bindings(source).items():
            add(local, owner, "namespace", module_doc(db_path, lang, owner))
        for name, owner in GO_BUILTINS.items():
            add(name, owner, "type", module_doc(db_path, lang, owner))
    elif lang == "php":
        for local, owner in php_bindings(source).items():
            add(local, owner, "type", module_doc(db_path, lang, owner))
        for name, kind, signature, doc in query_symbols(db_path, lang, "php", prefix, 40):
            add(name, signature or name, kind, doc)
    elif lang == "rust":
        for local, owner in rust_bindings(source).items():
            add(local, owner, "type", module_doc(db_path, lang, owner))
        for name, owner in RUST_PRELUDE.items():
            add(name, owner, "type", module_doc(db_path, lang, owner))
    elif lang == "cpp":
        for name, kind, signature, doc in query_symbols(db_path, lang, "c", prefix, 40):
            add(name, signature or name, kind, doc)
        for simple in cpp_assigns(source).values():
            add(simple.split("::")[-1], simple, "type", module_doc(db_path, lang, simple))
    elif lang == "css":
        for name, kind, signature, doc in query_symbols(db_path, lang, "css", prefix, 40):
            add(name, signature or name, kind, doc)
    elif lang == "perl":
        for local, owner in perl_imports(source).items():
            add(local, owner, "function", module_doc(db_path, lang, owner))
        for name, kind, signature, doc in query_symbols(db_path, lang, "perl", prefix, 40):
            add(name, signature or name, kind, doc)
    elif lang == "matlab":
        for name, kind, signature, doc in query_symbols(db_path, lang, "matlab", prefix, 40):
            add(name, signature or name, kind, doc)
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
    return {"source": exe, "version": version + "|" + _INDEX_VERSION, "executable": exe}


def detect_ruby(executable=""):
    exe = executable or shutil.which("ruby")
    if not exe or not os.path.isfile(exe):
        return None
    version = _run_version([exe, "-e", "print RUBY_VERSION"])
    if not version:
        return None
    return {"source": exe, "version": version + "|" + _INDEX_VERSION, "executable": exe}


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
    version = _java_version(home) + "|" + ",".join(stamps) + "|" + _INDEX_VERSION
    return {"source": home, "version": version, "sources": sources}


def detect_javascript(executable=""):
    """Find Node. The indexer reads the global npm root, not a project."""
    exe = executable or shutil.which("node")
    if not exe or not os.path.isfile(exe):
        return None
    version = _run_version([exe, "-p", "process.version"])
    if not version:
        return None
    return {"source": exe, "version": version + "|" + _INDEX_VERSION, "executable": exe}


def _npm_root():
    npm = shutil.which("npm")
    if not npm:
        return ""
    root = _run_version([npm, "root", "-g"])
    if root and os.path.isdir(root):
        return root
    return ""


def _typescript_lib(tsc, npm_root):
    candidates = []
    if tsc and os.path.isfile(tsc):
        real = os.path.realpath(tsc)
        candidates.append(os.path.join(os.path.dirname(os.path.dirname(real)), "lib"))
    if npm_root:
        candidates.append(os.path.join(npm_root, "typescript", "lib"))
    for path in candidates:
        if os.path.isdir(path):
            return path
    return ""


def detect_typescript(tsc_executable=""):
    """Find the installed ``typescript`` package. A project tree is not read."""
    tsc = tsc_executable or shutil.which("tsc") or ""
    if tsc and not os.path.isfile(tsc):
        tsc = shutil.which(tsc) or ""
    npm_root = _npm_root()
    lib = _typescript_lib(tsc, npm_root)
    roots = []
    if lib:
        roots.append(lib)
    types = os.path.join(npm_root, "@types") if npm_root else ""
    if types and os.path.isdir(types) and types not in roots:
        roots.append(types)
    if npm_root and npm_root not in roots:
        roots.append(npm_root)
    if not any(os.path.isdir(path) for path in roots):
        return None
    version = _run_version([tsc, "--version"]) if tsc else ""
    return {
        "source": roots[0],
        "version": (version or "typescript") + "|" + _INDEX_VERSION,
        "roots": roots,
    }


def detect_csharp(dotnet_root=""):
    """Find a .NET targeting pack that ships XML docs."""
    root = dotnet_root or os.environ.get("DOTNET_ROOT") or ""
    if root:
        root = os.path.abspath(os.path.expanduser(root))
    else:
        exe = shutil.which("dotnet")
        root = os.path.dirname(os.path.realpath(exe)) if exe else ""
    if not root:
        return None
    packs = os.path.join(root, "packs", "Microsoft.NETCore.App.Ref")
    if not os.path.isdir(packs):
        return None
    versions = sorted(name for name in os.listdir(packs) if os.path.isdir(os.path.join(packs, name)))
    if not versions:
        return None
    pack = os.path.join(packs, versions[-1])
    has_xml = False
    for dirpath, dirnames, filenames in os.walk(pack):
        if any(name.endswith(".xml") for name in filenames):
            has_xml = True
            break
        if dirpath.count(os.sep) - pack.count(os.sep) > 3:
            dirnames[:] = []
    if not has_xml:
        return None
    roots = [pack]
    nuget = os.path.expanduser("~/.nuget/packages")
    if os.path.isdir(nuget):
        roots.append(nuget)
    return {
        "source": pack,
        "version": versions[-1] + "|" + _INDEX_VERSION,
        "roots": roots,
    }


def detect_go(executable=""):
    exe = executable or shutil.which("go")
    if not exe or not os.path.isfile(exe):
        return None
    goroot = _run_version([exe, "env", "GOROOT"])
    stdlib = os.path.join(goroot, "src") if goroot else ""
    if not os.path.isdir(stdlib):
        return None
    modules = _run_version([exe, "env", "GOMODCACHE"])
    if not modules or not os.path.isdir(modules):
        modules = ""
    version = _run_version([exe, "env", "GOVERSION"]) or "go"
    return {
        "source": goroot,
        "version": version + "|" + _INDEX_VERSION,
        "stdlib": stdlib,
        "modules": modules,
    }


def detect_php(executable=""):
    exe = executable or shutil.which("php")
    if not exe or not os.path.isfile(exe):
        return None
    version = _run_version([exe, "-r", "echo PHP_VERSION;"])
    if not version:
        return None
    return {"source": exe, "version": version + "|" + _INDEX_VERSION, "executable": exe}


def detect_rust(executable=""):
    """Find rust-src. Nothing is downloaded when the component is missing."""
    exe = executable or shutil.which("rustc")
    if not exe or not os.path.isfile(exe):
        return None
    sysroot = _run_version([exe, "--print", "sysroot"])
    library = ""
    for rel in ("lib/rustlib/src/rust/library", "lib/rustlib/src/library"):
        path = os.path.join(sysroot, rel) if sysroot else ""
        if path and os.path.isdir(path):
            library = path
            break
    if not library:
        return None
    cargo = os.path.expanduser("~/.cargo/registry/src")
    if not os.path.isdir(cargo):
        cargo = ""
    version = _run_version([exe, "--version"]) or "rustc"
    return {
        "source": library,
        "version": version + "|" + _INDEX_VERSION,
        "library": library,
        "cargo": cargo,
    }


def _include_dirs(compiler, language):
    try:
        proc = subprocess.run(
            [compiler, "-E", "-x", language, "-", "-v"],
            input="",
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    dirs = []
    grab = False
    for line in (proc.stderr or "").splitlines():
        if "search starts here" in line:
            grab = True
            continue
        if not grab:
            continue
        if line.startswith("End ") or not line.startswith(" "):
            grab = False
            continue
        path = line.strip()
        if not path.startswith("/") or path.endswith(")"):
            continue
        if os.path.isdir(path) and path not in dirs:
            dirs.append(path)
    return dirs


def detect_css():
    return {"source": "css", "version": "css|" + _INDEX_VERSION}


def detect_html():
    return {"source": "html", "version": "html|" + _INDEX_VERSION}


def _perl_inc(exe):
    try:
        proc = subprocess.run(
            [exe, "-e", r'print join("\n", @INC)'],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    roots = []
    for line in (proc.stdout or "").splitlines():
        path = line.strip()
        if path in ("", ".") or not os.path.isdir(path) or path in roots:
            continue
        roots.append(path)
    return roots


def detect_perl(executable=""):
    exe = executable or ""
    if exe and not os.path.isfile(exe):
        exe = shutil.which(exe) or ""
    if not exe:
        exe = shutil.which("perl") or ""
    if not exe:
        return None
    roots = _perl_inc(exe)
    if not roots:
        return None
    version = _run_version([exe, "-e", "print $^V"]) or "perl"
    return {
        "source": exe,
        "version": version + "|" + _INDEX_VERSION,
        "roots": roots,
        "executable": exe,
    }


def _command_lines(argv, timeout=15):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def _octave_roots(exe):
    cfg = os.path.join(os.path.dirname(exe), "octave-config")
    if not os.path.isfile(cfg):
        cfg = shutil.which("octave-config") or ""
    if not cfg:
        return []
    roots = []
    for key in ("FCNFILEDIR", "LOCALFCNFILEDIR", "LOCALVERFCNFILEDIR"):
        lines = _command_lines([cfg, "--print", key])
        if lines and os.path.isdir(lines[-1]) and lines[-1] not in roots:
            roots.append(lines[-1])
    return roots


def _matlab_roots(exe):
    lines = _command_lines([exe, "-batch", "disp(matlabroot)"], timeout=30)
    if not lines:
        return []
    core = os.path.join(lines[-1], "toolbox", "matlab")
    if os.path.isdir(core):
        return [core]
    return []


def detect_matlab(executable=""):
    exe = executable or ""
    if exe and not os.path.isfile(exe):
        exe = shutil.which(exe) or ""
    if not exe:
        exe = shutil.which("octave") or shutil.which("matlab") or ""
    if not exe:
        return None
    if "octave" in os.path.basename(exe).lower():
        roots = _octave_roots(exe)
        version = ""
        cfg = os.path.join(os.path.dirname(exe), "octave-config")
        if not os.path.isfile(cfg):
            cfg = shutil.which("octave-config") or ""
        if cfg:
            lines = _command_lines([cfg, "--print", "VERSION"])
            version = lines[-1] if lines else ""
    else:
        roots = _matlab_roots(exe)
        version = _run_version([exe, "-batch", "disp(version)"]) or ""
    if not roots:
        return None
    return {
        "source": exe,
        "version": (version or "matlab") + "|" + _INDEX_VERSION,
        "roots": roots,
        "executable": exe,
    }


def detect_cpp(compiler=""):
    exe = compiler or ""
    if exe and not os.path.isfile(exe):
        exe = shutil.which(exe) or ""
    if not exe:
        exe = shutil.which("clang++") or shutil.which("g++") or shutil.which("c++") or ""
    if not exe:
        return None
    cxx = _include_dirs(exe, "c++")
    c_dirs = _include_dirs(exe, "c")
    if not cxx and not c_dirs:
        return None
    version = _run_version([exe, "--version"]) or "c++"
    return {
        "source": exe,
        "version": version + "|" + _INDEX_VERSION,
        "cxx_dirs": cxx,
        "c_dirs": c_dirs,
    }


def detect_all(
    python_executable="",
    ruby_executable="",
    java_home="",
    java_sources=None,
    node_executable="",
    tsc_executable="",
    dotnet_root="",
    go_executable="",
    php_executable="",
    rustc_executable="",
    cxx_compiler="",
    perl_executable="",
    matlab_executable="",
):
    return {
        "python": detect_python(python_executable),
        "java": detect_java(java_home, java_sources),
        "ruby": detect_ruby(ruby_executable),
        "javascript": detect_javascript(node_executable),
        "typescript": detect_typescript(tsc_executable),
        "csharp": detect_csharp(dotnet_root),
        "go": detect_go(go_executable),
        "php": detect_php(php_executable),
        "rust": detect_rust(rustc_executable),
        "cpp": detect_cpp(cxx_compiler),
        "css": detect_css(),
        "html": detect_html(),
        "perl": detect_perl(perl_executable),
        "matlab": detect_matlab(matlab_executable),
    }


def status_text(db_path, detected):
    meta = read_meta(db_path)
    lines = []
    for lang in LANGS:
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
