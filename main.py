"""Completions, hover, and language-server features for ``core.LANGS``. An
installed server answers first, else a local SQLite index (one per Python
interpreter)."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import subprocess
import threading
from typing import Any, Callable, Iterator, Optional

import sublime
import sublime_plugin

from . import core, lsp
from .indexers import (
    cpp_src, cs_src, go_src, java_src, markup_src, matlab_src, perl_src,
    rust_src, ts_src,
)

_LOCK = threading.Lock()
_SETTINGS = "Intellekt.sublime-settings"
_SCOPE = ", ".join(selector for selector, _lang_name in core.LANG_SELECTORS)
_TRIGGERS = tuple(
    {"selector": selector, "characters": core.LANG_TRIGGERS[lang]}
    for selector, lang in core.LANG_SELECTORS
)
_SCRIPTS = {
    "python": "python_index.py",
    "ruby": "ruby_index.rb",
    "javascript": "js_index.js",
    "php": "php_index.php",
}
# INHIBIT_REORDER keeps our order: public names, then _private, then __dunder__.
_MEMBER_FLAGS = (
    sublime.INHIBIT_WORD_COMPLETIONS | sublime.DYNAMIC_COMPLETIONS | sublime.INHIBIT_REORDER
)
_BARE_FLAGS = sublime.DYNAMIC_COMPLETIONS | sublime.INHIBIT_REORDER
# One symbol per kind, in the spirit of VS Code's icons. KIND_ID_AMBIGUOUS
# has no theme tint, so every icon is drawn plain in the text color.
# None of these characters has an emoji form, so none turns into a color emoji.
_PLAIN = sublime.KIND_ID_AMBIGUOUS
_KINDS = {
    "type": (_PLAIN, "◈", "Class"),
    "interface": (_PLAIN, "◎", "Interface"),
    "enum": (_PLAIN, "≣", "Enum"),
    "method": (_PLAIN, "⬡", "Method"),
    "function": (_PLAIN, "ƒ", "Function"),
    "property": (_PLAIN, "◇", "Property"),
    "field": (_PLAIN, "◇", "Field"),
    # Index rows call class and module members "variable".
    "variable": (_PLAIN, "◇", "Property"),
    "local": (_PLAIN, "⊠", "Variable"),
    "constant": (_PLAIN, "π", "Constant"),
    "namespace": (_PLAIN, "⧉", "Module"),
    "keyword": (_PLAIN, "≡", "Keyword"),
    "snippet": (_PLAIN, "⬚", "Snippet"),
}
_KIND_FALLBACK = (_PLAIN, "•", "Symbol")
# NOTE: only the start of a very large buffer is scanned for imports and
# assignments. Upgrade path: a background parse of the whole buffer.
_SOURCE_CAP = 400_000
_ROOT_MARKERS = (
    ".git", "go.mod", "Cargo.toml", "pyproject.toml", "setup.py",
    "package.json", "compile_commands.json", "composer.json", "Gemfile",
)

_SQUIGGLY = sublime.DRAW_SQUIGGLY_UNDERLINE | sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE
# Severity -> (region key, color scope, gutter icon, label, flags).
# Hints (unused or deprecated code) get a gray squiggle (the theme's comment
# color) and a gray gutter circle, so they are distinct from colored errors.
_DIAG_STYLES = {
    1: ("intellekt.error", "region.redish", "circle", "Error", _SQUIGGLY),
    2: ("intellekt.warning", "region.yellowish", "dot", "Warning", _SQUIGGLY),
    3: ("intellekt.info", "region.bluish", "dot", "Info", _SQUIGGLY),
    4: ("intellekt.hint", "comment", "circle", "Hint", _SQUIGGLY),
}
_TAG_LABELS = {1: "Unused", 2: "Deprecated"}
_DIAGS = {}      # view id -> [(Region, severity, message, raw diagnostic, label)]

_CLIENTS = {}    # (window id, root, command, interpreter) -> lsp.Client
_COMMANDS = {}   # (lang, interpreter) -> server spec or None
_VENVS = {}      # (file, folders) -> interpreter
_PY_SEEN = set()  # interpreters whose index was checked this session


def plugin_loaded() -> None:
    _spawn(_startup)


def _startup() -> None:
    # NOTE: this widens PATH for the whole plugin host, other packages
    # included, so every tool lookup and child process (npm, go env, the
    # indexers) finds what the user's terminal finds.
    os.environ["PATH"] = lsp.search_path()
    core.drop_language(_db_path(), "python")
    ensure_indexes(False)


def plugin_unloaded() -> None:
    _stop_clients()


def _spawn(fn: Callable[[], Any]) -> None:
    # Indexing can take a minute. Its own thread keeps Sublime's async
    # thread free for completions and hover.
    threading.Thread(target=fn, daemon=True).start()


def _status(message: str) -> None:
    sublime.set_timeout(lambda: sublime.status_message(message), 0)


def _settings() -> sublime.Settings:
    return sublime.load_settings(_SETTINGS)


# Feature switches: a language block may override the global "lsp_<name>".
_FEATURE_DEFAULTS = {
    "diagnostics": True, "signature_help": True, "highlight_references": True,
    "show_hints": True, "inlay_hints": True, "code_lens": True, "document_links": True,
    "format_on_save": False, "format_on_type": False,
    "semantic_highlighting": False, "inactive_regions": False,
}


def _lang_block(lang: Optional[str]) -> dict[str, Any]:
    block = _settings().get(lang) if lang else None
    return block if isinstance(block, dict) else {}


def _lang_value(lang: str, key: str, default: Any = "") -> Any:
    """A per-language setting such as ``"python": {"executable": ...}``."""
    value = _lang_block(lang).get(key)
    if value in (None, ""):
        return default
    return os.path.expanduser(value) if isinstance(value, str) else value


def _feature(lang: Optional[str], name: str) -> bool:
    """True when feature ``name`` is on for ``lang``."""
    block = _lang_block(lang)
    if isinstance(block.get(name), bool):
        return block[name]
    return bool(_settings().get("lsp_" + name, _FEATURE_DEFAULTS.get(name, True)))


def _cache_dir() -> str:
    return os.path.join(sublime.cache_path(), "Intellekt")


def _python_db(exe: str) -> str:
    """One cache per interpreter, so each venv keeps its own site-packages."""
    key = hashlib.sha1(exe.encode("utf-8")).hexdigest()[:12]
    return os.path.join(_cache_dir(), "python-%s.sqlite" % key)


def _db_path(view: Optional[sublime.View] = None, lang: Optional[str] = None) -> str:
    if lang == "python":
        return _python_db(_python_exe(view))
    return os.path.join(_cache_dir(), "index.sqlite")


def _python_exe(view: Optional[sublime.View] = None) -> str:
    """The interpreter for ``view``: the setting, its venv, or the system one."""
    chosen = _lang_value("python", "executable")
    if chosen:
        return chosen
    window = (view.window() if view else None) or sublime.active_window()
    if view is None and window:
        view = window.active_view()
    path = view.file_name() if view else None
    folders = tuple(window.folders()) if window else ()
    key = (path, folders)
    if key not in _VENVS:
        _VENVS[key] = (
            core.find_venv(path, folders)
            or core.system_python(lsp.search_path())
        )
    return _VENVS[key]


def _is_venv(exe: str) -> bool:
    return os.path.isfile(os.path.join(os.path.dirname(os.path.dirname(exe)), "pyvenv.cfg"))


def _detected(view: Optional[sublime.View] = None) -> dict[str, Any]:
    sources = _lang_value("java", "sources", [])
    if not isinstance(sources, list):
        sources = []
    return core.detect_all(
        python_executable=_python_exe(view),
        ruby_executable=_lang_value("ruby", "executable"),
        java_home=_lang_value("java", "java_home"),
        java_sources=sources,
        node_executable=_lang_value("javascript", "executable"),
        tsc_executable=_lang_value("typescript", "executable"),
        dotnet_root=_lang_value("csharp", "dotnet_root"),
        go_executable=_lang_value("go", "executable"),
        php_executable=_lang_value("php", "executable"),
        rustc_executable=_lang_value("rust", "executable"),
        cxx_compiler=_lang_value("cpp", "compiler"),
        perl_executable=_lang_value("perl", "executable"),
        matlab_executable=_lang_value("matlab", "executable"),
    )


def _produce(lang: str, info: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if lang == "java":
        for path in info["sources"]:
            yield from java_src.scan_zip(path)
        return
    if lang == "typescript":
        yield from ts_src.scan_roots(info["roots"])
        return
    if lang == "csharp":
        yield from cs_src.scan_roots(info["roots"])
        return
    if lang == "go":
        yield from go_src.scan_roots(info["stdlib"], info.get("modules") or "")
        return
    if lang == "rust":
        yield from rust_src.scan_roots(info["library"], info.get("cargo") or "")
        return
    if lang == "cpp":
        yield from cpp_src.scan_dirs(info["cxx_dirs"], info.get("c_dirs") or [])
        return
    if lang == "css":
        yield from markup_src.css_records()
        return
    if lang == "html":
        yield from markup_src.html_records()
        return
    if lang == "perl":
        yield from perl_src.scan_roots(info["roots"])
        return
    if lang == "matlab":
        yield from matlab_src.scan_roots(info["roots"])
        return
    yield from core.run_indexer(info["executable"], _SCRIPTS[lang])


def ensure_indexes(force: bool, python_exe: Optional[str] = None) -> None:
    """Build stale indexes (only ``python_exe``'s when given).

    A full build skips if one is running; a single-interpreter build waits for it.
    """
    if python_exe is None:
        if not _LOCK.acquire(blocking=False):
            _status("Intellekt: index already running")
            return
    else:
        _LOCK.acquire()
    try:
        if python_exe is None:
            detected = _detected()
        else:
            detected = {"python": core.detect_python(python_exe)}
        for lang in core.LANGS:
            info = detected.get(lang)
            if not info:
                continue
            db_path = _python_db(info["executable"]) if lang == "python" else _db_path()
            if not force and core.is_fresh(db_path, lang, info["source"], info["version"]):
                continue
            _status("Intellekt: indexing %s…" % lang)
            try:
                count = core.write_language(
                    db_path, lang, info["source"], info["version"], _produce(lang, info)
                )
            except Exception as err:
                _status("Intellekt: %s index failed" % lang)
                print("Intellekt %s: %s" % (lang, err))
                continue
            _status("Intellekt: indexed %s (%s symbols)" % (lang, count))
    finally:
        _LOCK.release()


def _ensure_python_index(view: sublime.View) -> None:
    exe = _python_exe(view)
    if exe and exe not in _PY_SEEN:
        _PY_SEEN.add(exe)
        _spawn(lambda: ensure_indexes(False, python_exe=exe))


# ---- language servers -------------------------------------------------------

def _server_spec(lang: str, interpreter: str) -> Optional[tuple[list[str], dict[str, str], dict[str, Any], dict[str, Any]]]:
    """``(argv, env, settings, init_options)`` for ``lang``, or None."""
    key = (lang, interpreter)
    if key in _COMMANDS:
        return _COMMANDS[key]
    # "<lang>": {"server": [...] or false, "settings": {...}, "initialization_options": {...}}
    block = _lang_block(lang)
    override = block.get("server")
    extra = block.get("settings") or {}
    init = dict(block.get("initialization_options") or {})
    bins = [os.path.dirname(interpreter)] if interpreter else []
    node = _lang_value("javascript", "executable") or shutil.which("node", path=lsp.search_path())
    if node:
        # typescript-language-server and friends start with "#!/usr/bin/env node".
        bins.append(os.path.dirname(node))
    path = lsp.search_path(bins)
    cmd = lsp.find_command(lang, override, path)
    spec = None
    if cmd:
        env = dict(os.environ, PATH=path)
        settings = {}
        if lang == "python" and interpreter:
            prefix = os.path.dirname(os.path.dirname(interpreter))
            if _is_venv(interpreter):
                env["VIRTUAL_ENV"] = prefix
            # Read by pyright/basedpyright, pylsp, and jedi-language-server.
            settings = {
                "python": {"pythonPath": interpreter},
                "pylsp": {"plugins": {"jedi": {"environment": interpreter}}},
            }
            init.setdefault("workspace", {"environmentPath": interpreter})
        if lang in ("javascript", "typescript") and "typescript-language-server" in os.path.basename(cmd[0]):
            # It only looks for TypeScript inside the project; point it at the
            # global install when there is one.
            tsserver = lsp.tsserver_path(path)
            if tsserver:
                init.setdefault("tsserver", {"path": tsserver})
        if lang == "go" and _feature(lang, "semantic_highlighting"):
            # gopls only sends semantic tokens when asked to at startup.
            init.setdefault("semanticTokens", True)
        settings.update(extra)
        spec = (cmd, env, settings, init)
    _COMMANDS[key] = spec
    return spec


def _root(window: sublime.Window, path: str) -> str:
    folders = [f for f in window.folders() if path.startswith(f.rstrip(os.sep) + os.sep)]
    if folders:
        return max(folders, key=len)
    current = os.path.dirname(path)
    while True:
        if any(os.path.exists(os.path.join(current, m)) for m in _ROOT_MARKERS):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.dirname(path)
        current = parent


def _client(view: sublime.View, lang: str) -> Optional[lsp.Client]:
    """A running server for ``view``, started on first use, or None."""
    path = view.file_name()
    window = view.window()
    if not path or not window or not _settings().get("lsp_enabled", True):
        return None
    interpreter = _python_exe(view) if lang == "python" else ""
    spec = _server_spec(lang, interpreter)
    if not spec:
        return None
    cmd, env, settings, init = spec
    root = _root(window, path)
    key = (window.id(), root, tuple(cmd), interpreter)
    client = _CLIENTS.get(key)
    if client is None:
        client = lsp.Client(cmd, root, env, settings, init)
        if _feature(lang, "diagnostics"):
            client.on_diagnostics = _on_diagnostics
        client.on_apply_edit = lambda workspace_edit, _none: sublime.set_timeout(
            lambda: _apply_workspace_edit(sublime.active_window(), workspace_edit), 0
        )
        client.on_notify = lambda method, params, c=client: _on_notify(c, method, params)
        _CLIENTS[key] = client
        client.start()
    # NOTE: a server that exited is not restarted, so a crash loop cannot
    # spin. "Intellekt: Restart Language Servers" starts it again.
    return None if client.dead else client


def _stop_clients(window_id: Optional[int] = None) -> None:
    for key, client in list(_CLIENTS.items()):
        if window_id is None or key[0] == window_id:
            client.stop()
            del _CLIENTS[key]
    if window_id is None:
        for window in sublime.windows():
            for view in window.views():
                _draw_diagnostics(view, [])


def _point(view: sublime.View, line: int, char: int) -> int:
    start = view.text_point(line, 0)
    return start + lsp.utf16_col(view.substr(view.line(start)), char)


def _on_diagnostics(uri: str, diagnostics: list[dict[str, Any]]) -> None:
    rows = lsp.diagnostic_rows(diagnostics)
    target = lsp.uri_path(uri)

    def apply() -> None:
        for window in sublime.windows():
            for view in window.views():
                path = view.file_name()
                if path and lsp.same_path(path, target):
                    _draw_diagnostics(view, rows)

    sublime.set_timeout(apply, 0)


def _draw_diagnostics(view: sublime.View, rows: list[tuple[Any, ...]]) -> None:
    found = []
    by_severity = {severity: [] for severity in _DIAG_STYLES}
    show_hints = _feature(_lang(view, 0), "show_hints")
    for start, end, severity, message, raw in rows:
        if severity not in _DIAG_STYLES or (severity == 4 and not show_hints):
            continue
        a, b = _point(view, *start), _point(view, *end)
        if a == b:
            # An empty range still needs something to underline.
            b = view.word(a).b if view.word(a).b > a else min(a + 1, view.size())
        region = sublime.Region(a, b)
        by_severity[severity].append(region)
        tags = raw.get("tags") or ()
        label = next((_TAG_LABELS[t] for t in tags if t in _TAG_LABELS), _DIAG_STYLES[severity][3])
        found.append((region, severity, message, raw, label))
    hint_labels = [entry[4].lower() for entry in found if entry[1] == 4]
    for severity, (key, scope, icon, _label, flags) in _DIAG_STYLES.items():
        view.add_regions(key, by_severity[severity], scope, icon, flags)
    _DIAGS[view.id()] = found
    errors = len(by_severity[1])
    warnings = len(by_severity[2])
    unused = sum(1 for label in hint_labels if label == "unused")
    parts = (["✖ %d" % errors] if errors else []) + (["⚠ %d" % warnings] if warnings else [])
    parts += ["◌ %d unused" % unused] if unused else []
    view.set_status("intellekt", "  ".join(parts))


def _diagnostics_at(view: sublime.View, region: sublime.Region) -> str:
    """Markdown for the diagnostics touching ``region``, or ""."""
    lines = []
    for found, _severity, message, _raw, label in _DIAGS.get(view.id(), ()):
        if found.intersects(region) or found.contains(region.a):
            # Backticks are kept: servers use them for code ("field `x` is never read").
            text = re.sub(r"([\\*_\[\]<>])", r"\\\1", message)
            lines.append("**%s:** %s" % (label, text))
    return "\n\n".join(lines)


def _sync(client: lsp.Client, view: sublime.View, lang: str) -> str:
    text = view.substr(sublime.Region(0, view.size()))
    return client.sync(view.file_name(), lang, text, view.change_count())


def _position(view: sublime.View, point: int) -> dict[str, int]:
    row, _col = view.rowcol(point)
    start = view.text_point(row, 0)
    return {"line": row, "character": lsp.utf16_len(view.substr(sublime.Region(start, point)))}


def _items_from_rows(
    rows: list[tuple[Any, ...]], word_start: Optional[tuple[int, int]] = None,
) -> list[sublime.CompletionItem]:
    """CompletionItems; ``word_start`` ``(line, utf16_char)`` keeps edits starting earlier (postfix)."""
    items = []
    for trigger, annotation, text, snippet, kind, doc, extra, start in rows:
        icon = _icon(kind, trigger)
        # The details line is plain text, so code fences would show literally.
        details = _details(re.sub(r"```\w*", "", doc)[:300])
        replace_from = list(start) if start and word_start and tuple(start) < word_start else None
        if extra or replace_from:
            # Auto-import, postfix and similar: a command makes every edit.
            items.append(sublime.CompletionItem.command_completion(
                trigger, "intellekt_complete",
                {"text": text, "snippet": snippet, "edits": extra, "replace_from": replace_from},
                annotation=annotation, kind=icon, details=details,
            ))
        elif snippet:
            items.append(sublime.CompletionItem.snippet_completion(
                trigger, text, annotation=annotation, kind=icon, details=details,
            ))
        else:
            items.append(sublime.CompletionItem(
                trigger, annotation=annotation, completion=text,
                completion_format=sublime.COMPLETION_FORMAT_TEXT,
                kind=icon, details=details,
            ))
    return items


def _module_items(view: sublime.View, point: int, prefix: str, lang: str) -> list[sublime.CompletionItem]:
    """Unimported Python modules matching a bare name; picking one adds its ``import``."""
    if lang != "python" or len(prefix) < 2:
        return []
    start = point - len(prefix)
    before = view.substr(sublime.Region(view.line(point).a, start))
    if core.member_op(before)[0] is not None or re.match(r"\s*(?:import|from)\b", before):
        return []
    source = _source(view)
    rows = core.module_suggestions(_db_path(view, lang), source, prefix)
    if not rows:
        return []
    at = {"line": core.python_import_line(source), "character": 0}
    return [
        sublime.CompletionItem.command_completion(
            name, "intellekt_complete",
            {"text": name, "snippet": False,
             "edits": [{"range": {"start": at, "end": at}, "newText": "import %s\n" % name}]},
            annotation="import", kind=_KINDS["namespace"], details=_details(doc[:200]),
        )
        for name, doc in rows
    ]


def _with_modules(
    items: list[sublime.CompletionItem], modules: list[sublime.CompletionItem],
) -> list[sublime.CompletionItem]:
    have = {item.trigger for item in items}
    return list(items) + [item for item in modules if item.trigger not in have]


def _lsp_completions(view: sublime.View, point: int, prefix: str, lang: str, client: lsp.Client) -> sublime.CompletionList:
    """Ask the server; the index answers if it fails, is empty, or is slow."""
    result = sublime.CompletionList()
    lock = threading.Lock()
    state = {"done": False}
    start = point - len(prefix)
    member = core.member_op(view.substr(sublime.Region(max(0, start - 3), start)))[0] is not None
    modules = _module_items(view, point, prefix, lang)
    position = _position(view, start)
    word_start = (position["line"], position["character"])

    def finish(items: list[sublime.CompletionItem], flags: int) -> None:
        with lock:
            if state["done"]:
                return
            state["done"] = True
        items = _with_modules(items, modules)
        sublime.set_timeout(lambda: result.set_completions(items, flags), 0)

    def use_index() -> None:
        if state["done"]:
            return
        found = _index_completions(view, point, prefix, lang)
        if found:
            finish(found.completions, found.flags)
        else:
            finish([], 0)

    def on_reply(reply: Any, error: Any) -> None:
        items, incomplete = lsp.completion_items(reply, lsp.case_first(lang)) if not error else ([], False)
        if not items:
            use_index()
            return
        state["replied"] = True
        flags = sublime.INHIBIT_REORDER
        if member:
            flags |= sublime.INHIBIT_WORD_COMPLETIONS
        if incomplete:
            flags |= sublime.DYNAMIC_COMPLETIONS

        def done() -> None:
            finish(_items_from_rows([lsp.completion_row(item) for item in items], word_start), flags)

        # Servers such as pyright send docs only when an item is resolved.
        # The first 40 are resolved, with a 0.3 s budget.
        resolvable = (client.capabilities.get("completionProvider") or {}).get("resolveProvider")
        wanted = [i for i, item in enumerate(items[:40]) if not item.get("documentation")] if resolvable else []
        if not wanted:
            done()
            return
        left = [len(wanted)]

        def resolved(index: int, value: Any) -> None:
            if isinstance(value, dict):
                merged = dict(items[index])
                merged.update(value)
                items[index] = merged
            with lock:
                left[0] -= 1
                last = left[0] == 0
            if last:
                done()

        for index in wanted:
            client.request("completionItem/resolve", items[index],
                           lambda value, _error, index=index: resolved(index, value))
        threading.Timer(0.3, done).start()

    trigger = view.substr(start - 1) if start else ""
    triggers = (client.capabilities.get("completionProvider") or {}).get("triggerCharacters") or []
    context = {"triggerKind": 1}
    if not prefix and trigger in triggers:
        context = {"triggerKind": 2, "triggerCharacter": trigger}
    rid = client.request("textDocument/completion", {
        "textDocument": {"uri": _sync(client, view, lang)},
        "position": _position(view, point),
        "context": context,
    }, on_reply)

    def timed_out() -> None:
        if state.get("replied"):
            return
        if rid is not None and not state["done"]:
            client.cancel(rid)
        use_index()

    timeout = _settings().get("lsp_completion_timeout_ms", 1500) / 1000.0
    threading.Timer(timeout, timed_out).start()
    return result


# ---- index completions ------------------------------------------------------

def _lang(view: sublime.View, point: int) -> Optional[str]:
    for selector, lang in core.LANG_SELECTORS:
        if view.match_selector(point, selector):
            return lang
    return None


def _source(view: sublime.View) -> str:
    size = min(view.size(), _SOURCE_CAP)
    return view.substr(sublime.Region(0, size))


def _details(doc: Optional[str]) -> str:
    text = re.sub(r"\s+", " ", (doc or "")).strip()
    if not text:
        return ""
    return _escape(text)


def _open_link(href: str) -> None:
    if href.startswith("https://") or href.startswith("http://"):
        sublime.run_command("open_url", {"url": href})


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _icon(kind: str, name: str = "") -> tuple[int, str, str]:
    if kind in ("variable", "local") and len(name) > 1 and name.isupper():
        kind = "constant"
    return _KINDS.get(kind, _KIND_FALLBACK)


def _annotation(signature: Optional[str]) -> str:
    """Show argument names in the panel without snippet placeholders."""
    text = re.sub(r"\$\{\d+:([^}]*)\}", r"\1", signature or "")
    return text[:60]


def _items_from_symbols(rows: list[tuple[Any, ...]], lang: str) -> list[sublime.CompletionItem]:
    items = []
    by_case = lsp.case_first(lang)
    for name, kind, signature, doc in sorted(rows, key=lambda row: lsp.name_rank(row[0], by_case)):
        snippet = signature or name
        items.append(sublime.CompletionItem.snippet_completion(
            name,
            snippet,
            annotation=_annotation(snippet),
            kind=_icon(kind, name),
            details=_details(doc),
        ))
    return items


def _member_completions(
    view: sublime.View, point: int, prefix: str, lang: str, db_path: str,
) -> Optional[sublime.CompletionList]:
    start = point - len(prefix)
    before = view.substr(sublime.Region(max(0, start - 300), start))
    left, _op = core.member_op(before)
    if left is None:
        return None
    owner = core.member_owner(db_path, lang, _source(view), left)
    if not owner:
        return None
    rows = core.query_symbols(db_path, lang, owner, prefix)
    if not rows:
        return None
    return sublime.CompletionList(_items_from_symbols(rows, lang), _MEMBER_FLAGS)


def _markup_completions(view: sublime.View, point: int, lang: str, db_path: str) -> Optional[sublime.CompletionList]:
    if lang not in ("css", "html"):
        return None
    line = view.substr(sublime.Region(view.line(point).a, point))
    if lang == "css":
        found = core.css_value(line)
        if not found:
            return None
        prop, partial = found
        rows = core.query_symbols(db_path, lang, prop, partial, 40)
    else:
        found = core.html_context(line)
        if not found:
            return None
        if found[0] == "tag":
            rows = core.query_symbols(db_path, lang, "html", found[1], 120)
        else:
            tag, partial = found[1], found[2]
            rows = core.query_symbols(db_path, lang, tag, partial, 40)
            have = {row[0] for row in rows}
            for row in core.query_symbols(db_path, lang, "global", partial, 40):
                if row[0] not in have:
                    rows.append(row)
    if not rows:
        return None
    return sublime.CompletionList(_items_from_symbols(rows, lang), _MEMBER_FLAGS)


def _import_completions(view: sublime.View, point: int, lang: str, db_path: str) -> Optional[sublime.CompletionList]:
    line = view.substr(sublime.Region(view.line(point).a, point))
    typed = core.import_typed(lang, line)
    if typed is None:
        return None
    sep = core.SEP[lang]
    symbols = []
    if lang == "python" and re.match(r"\s*from\s+\S", line) and sep in typed:
        owner = typed[:-len(sep)] if typed.endswith(sep) else typed.rsplit(sep, 1)[0]
        partial = "" if typed.endswith(sep) else typed.rsplit(sep, 1)[1]
        if owner:
            symbols = core.query_symbols(
                db_path, lang, owner, partial, public_only=True,
            )
    segments = core.from_import_segments(
        core.module_names(db_path, lang), symbols, typed, sep,
    )
    if not segments:
        return None
    items = []
    segments = sorted(segments[:80], key=lambda seg: lsp.name_rank(seg["trigger"], lsp.case_first(lang)))
    for segment in segments:
        items.append(sublime.CompletionItem(
            segment["trigger"],
            annotation=segment["annotation"],
            completion=segment["trigger"],
            kind=_icon(segment["kind"], segment["trigger"]),
            details=_details(segment["doc"]),
        ))
    return sublime.CompletionList(items, _MEMBER_FLAGS)


def _bare_completions(view: sublime.View, prefix: str, lang: str, db_path: str) -> Optional[sublime.CompletionList]:
    if not prefix:
        return None
    rows = core.bare_names(db_path, lang, _source(view), prefix)
    if not rows:
        return None
    items = []
    for trigger, annotation, kind, doc in sorted(
        rows[:40], key=lambda row: lsp.name_rank(row[0], lsp.case_first(lang))
    ):
        items.append(sublime.CompletionItem(
            trigger,
            annotation=annotation,
            completion=trigger,
            kind=_icon(kind, trigger),
            details=_details(doc),
        ))
    return sublime.CompletionList(items, _BARE_FLAGS)


def _index_completions(view: sublime.View, point: int, prefix: str, lang: str) -> Optional[sublime.CompletionList]:
    db_path = _db_path(view, lang)
    return (
        _markup_completions(view, point, lang, db_path)
        or _import_completions(view, point, lang, db_path)
        or _member_completions(view, point, prefix, lang, db_path)
        or _bare_completions(view, prefix, lang, db_path)
    )


# ---- hover ------------------------------------------------------------------

def _popup(
    view: sublime.View, point: int, title: str, markdown: str, flags: int = sublime.HIDE_ON_MOUSE_MOVE_AWAY,
) -> None:
    if not view.is_valid():
        return
    if flags == sublime.HIDE_ON_MOUSE_MOVE_AWAY:
        _SIG_VIEWS.pop(view.id(), None)
    head = "<b>%s</b><br><br>" % _escape(title) if title else ""
    view.show_popup(
        '<body style="font-size: 0.85em">%s%s</body>' % (head, core.markdown_html(markdown)),
        flags=flags,
        location=point,
        max_width=480,
        max_height=320,
        on_navigate=_open_link,
    )


def _index_hover(view: sublime.View, point: int, lang: str) -> None:
    word_region = view.word(point)
    word = view.substr(word_region)
    if not word or not (word[0].isalpha() or word[0] == "_"):
        return
    before = view.substr(sublime.Region(max(0, word_region.a - 300), word_region.a))
    db_path = _db_path(view, lang)
    source = _source(view)
    title = ""
    doc = ""
    left, op = core.member_op(before.rstrip())
    if left is not None:
        owner = core.member_owner(db_path, lang, source, left)
        if owner:
            doc = core.symbol_doc(db_path, lang, owner, word) or core.module_doc(
                db_path, lang, owner + op + word
            )
            title = owner + op + word
    else:
        owner = core.resolve_owner(db_path, lang, source, word)
        if owner:
            doc = core.module_doc(db_path, lang, owner) or core.symbol_doc(
                db_path, lang, owner, word
            )
            title = owner
        if not doc:
            doc = core.module_doc(db_path, lang, word)
            title = word
    if doc:
        _popup(view, point, title, doc)


def _pull_diagnostics(client: lsp.Client, view: sublime.View, lang: str, tries: int = 0) -> None:
    """Pull diagnostics (``textDocument/diagnostic``) from servers that only answer when asked (ruby-lsp)."""
    if not _feature(lang, "diagnostics") or client.dead or not view.is_valid():
        return
    if not client.capabilities:
        if tries < 40:  # still starting
            sublime.set_timeout_async(lambda: _pull_diagnostics(client, view, lang, tries + 1), 500)
        return
    if not client.supports("diagnosticProvider"):
        return
    uri = _sync(client, view, lang)

    def received(result: Any, error: Any) -> None:
        if not error and isinstance(result, dict) and result.get("kind") == "full":
            _on_diagnostics(uri, result.get("items") or [])

    client.request("textDocument/diagnostic", {"textDocument": {"uri": uri}}, received)


def _warm(view: sublime.View) -> None:
    """Index the venv and start the server early so the first completion is fast."""
    lang = _lang(view, 0)
    if not lang:
        return
    if lang == "python":
        _ensure_python_index(view)
    client = _client(view, lang)
    if client:
        _sync(client, view, lang)
        _pull_diagnostics(client, view, lang)
        _decorate(view)


class IntellektListener(sublime_plugin.EventListener):
    def on_activated(self, view: sublime.View) -> None:
        self._ensure_triggers(view)

    def on_activated_async(self, view: sublime.View) -> None:
        _warm(view)

    def on_load(self, view: sublime.View) -> None:
        self._ensure_triggers(view)

    def on_modified_async(self, view: sublime.View) -> None:
        _signature_help(view)
        _format_on_type(view)
        # Send edits shortly after typing stops, so errors update live.
        count = view.change_count()

        def flush() -> None:
            if view.is_valid() and view.change_count() == count:
                lang = _lang(view, 0)
                client = _client(view, lang) if lang else None
                if client:
                    _sync(client, view, lang)
                    _pull_diagnostics(client, view, lang)
                    _decorate(view)

        sublime.set_timeout_async(flush, 400)

    def on_selection_modified_async(self, view: sublime.View) -> None:
        if view.id() in _SIG_VIEWS and view.sel():
            # Leaving the line ends the call being typed.
            if view.rowcol(view.sel()[0].b)[0] != _SIG_VIEWS[view.id()]:
                _SIG_VIEWS.pop(view.id(), None)
                view.hide_popup()
        if not view.file_name() or not _feature(_lang(view, 0), "highlight_references"):
            return
        token = object()
        _HL_TOKENS[view.id()] = token
        sublime.set_timeout_async(
            lambda: _HL_TOKENS.get(view.id()) is token and view.is_valid() and _highlight(view), 250
        )

    def on_window_command(
        self, window: sublime.Window, name: str, args: Optional[dict[str, Any]],
    ) -> Optional[tuple[str, dict[str, Any]]]:
        """F12 and Shift+F12 use the language server when there is one."""
        if name not in ("goto_definition", "goto_reference"):
            return None
        if name in _BYPASS:
            _BYPASS.discard(name)
            return None
        args = args or {}
        # A symbol named by Sublime's own popups goes to Sublime's lookup.
        if args.get("symbol") or not _has_server(window.active_view()):
            return None
        if name == "goto_definition":
            return ("intellekt_goto", {
                "kind": "definition", "event": args.get("event"),
                "fallback": name, "fallback_args": args,
            })
        return ("intellekt_references", {"fallback": name, "fallback_args": args})

    def on_pre_save(self, view: sublime.View) -> None:
        """Format on save (off by default), waiting briefly for the server."""
        lang = _lang(view, 0) if view.file_name() else None
        if not lang or not _feature(lang, "format_on_save"):
            return
        client = _client(view, lang) if _has_server(view) else None
        if lang == "python" and not (client and client.supports("documentFormattingProvider")):
            _python_tool(view, ["ruff", "black"], core.format_python, "format", quiet=True)
            return
        if not client:
            return
        done, box = threading.Event(), {}
        if _format_request(client, view, lang, False, lambda edits, error: (box.update(edits=edits), done.set())):
            timeout = _settings().get("lsp_format_on_save_timeout_ms", 1500) / 1000.0
            if done.wait(timeout) and box.get("edits"):
                view.run_command("intellekt_apply_edits", {"edits": box["edits"]})

    def on_close(self, view: sublime.View) -> None:
        for kind in ("inlay", "lens"):
            _PHANTOMS.pop((view.id(), kind), None)
        for table in (_LENSES, _LINKS, _SEM_KEYS, _SEL_STACK):
            table.pop(view.id(), None)
        _DIAGS.pop(view.id(), None)
        _SIG_VIEWS.pop(view.id(), None)
        _HL_TOKENS.pop(view.id(), None)
        path = view.file_name()
        if path:
            for client in list(_CLIENTS.values()):
                client.close(path)

    def on_post_save_async(self, view: sublime.View) -> None:
        path = view.file_name()
        if path:
            for client in list(_CLIENTS.values()):
                client.saved(path)
            lang = _lang(view, 0)
            client = _client(view, lang) if lang else None
            if client:
                _pull_diagnostics(client, view, lang)

    def on_pre_close_window(self, window: sublime.Window) -> None:
        _stop_clients(window.id())

    def _ensure_triggers(self, view: sublime.View) -> None:
        current = view.settings().get("auto_complete_triggers") or []
        if not isinstance(current, list):
            return
        have = set()
        for item in current:
            if isinstance(item, dict):
                have.add((item.get("selector"), item.get("characters")))
        extra = [
            item for item in _TRIGGERS
            if (item["selector"], item["characters"]) not in have
        ]
        if extra:
            view.settings().set("auto_complete_triggers", list(current) + extra)

    def on_query_completions(
        self, view: sublime.View, prefix: str, locations: list[int],
    ) -> Optional[sublime.CompletionList]:
        if not locations:
            return None
        point = locations[0]
        if not view.match_selector(point, _SCOPE):
            return None
        lang = _lang(view, point)
        if not lang:
            return None
        client = _client(view, lang)
        if client:
            return _lsp_completions(view, point, prefix, lang, client)
        found = _index_completions(view, point, prefix, lang)
        modules = _module_items(view, point, prefix, lang)
        if not modules:
            return found
        if found is None:
            return sublime.CompletionList(modules, _BARE_FLAGS)
        return sublime.CompletionList(_with_modules(found.completions, modules), found.flags)

    def on_hover(self, view: sublime.View, point: int, zone: int) -> None:
        if zone == sublime.HOVER_TEXT and _link_popup(view, point):
            return
        if zone == sublime.HOVER_GUTTER:
            problems = _diagnostics_at(view, view.line(point))
            if problems:
                _popup(view, point, "", problems)
            return
        if zone != sublime.HOVER_TEXT:
            return
        if not view.match_selector(point, _SCOPE):
            return
        lang = _lang(view, point)
        if not lang:
            return
        problems = _diagnostics_at(view, sublime.Region(point, point))
        client = _client(view, lang)
        if not client:
            if problems:
                _popup(view, point, "", problems)
            else:
                _index_hover(view, point, lang)
            return

        def on_reply(reply: Any, error: Any) -> None:
            text = "" if error else lsp.hover_text(reply)
            if problems:
                text = problems + ("\n\n---\n\n" + text if text else "")
            if text:
                sublime.set_timeout(lambda: _popup(view, point, "", text), 0)
            else:
                sublime.set_timeout(lambda: _index_hover(view, point, lang), 0)

        client.request("textDocument/hover", {
            "textDocument": {"uri": _sync(client, view, lang)},
            "position": _position(view, point),
        }, on_reply)

# ---- language server features -----------------------------------------------

_BYPASS = set()      # window commands to let through once, after a server gave nothing
_SIG_VIEWS = {}      # view id -> line of the open signature popup
_HL_TOKENS = {}      # view id -> latest caret move, to debounce highlights
_HIGHLIGHT_KEY = "intellekt.highlight"


# Sublime's own command for each key Intellekt binds, by platform ("*" = all).
# The bindings have no context (so menus show them); in a file without a
# server the key does what it normally does.
_SWITCH_FILE = ("switch_file", {
    "extensions": ["cpp", "cxx", "cc", "c", "hpp", "hxx", "hh", "h", "ipp", "inl", "m", "mm"],
    "side_by_side": True,
})
_KEY_FALLBACKS = {
    "intellekt_rename": {"*": ("next_bookmark", {})},
    "intellekt_quick_fix": {"windows": ("next_modification", {}), "linux": ("next_modification", {})},
    "intellekt_organize_imports": {"windows": _SWITCH_FILE, "linux": _SWITCH_FILE},
    "intellekt_workspace_symbols": {
        "osx": ("show_overlay", {"overlay": "goto", "show_files": True}),
        "windows": ("transpose", {}), "linux": ("transpose", {}),
    },
    "intellekt_signature_help": {"*": ("expand_selection", {"to": "scope"})},
    "intellekt_format": {"linux": ("show_panel", {"panel": "incremental_find", "reverse": True})},
    "intellekt_goto_implementation": {"*": ("goto_definition", {"side_by_side": True, "clear_to_right": True})},
    "intellekt_expand_selection": {
        "windows": ("move", {"by": "subword_ends", "forward": True, "extend": True}),
        "linux": ("move", {"by": "subword_ends", "forward": True, "extend": True}),
    },
    "intellekt_shrink_selection": {
        "windows": ("move", {"by": "subwords", "forward": False, "extend": True}),
        "linux": ("move", {"by": "subwords", "forward": False, "extend": True}),
    },
}


def _key_fallback(window: Optional[sublime.Window], view: Optional[sublime.View], name: str) -> bool:
    """True (after running Sublime's own command) when ``view`` has no server."""
    if _has_server(view):
        return False
    options = _KEY_FALLBACKS.get(name, {})
    found = options.get(sublime.platform()) or options.get("*")
    if found and window:
        window.run_command(*found)
    return True


def _has_server(view: Optional[sublime.View]) -> bool:
    """True when a server is configured for ``view``. Does not start it."""
    if not view or not view.file_name() or not _settings().get("lsp_enabled", True):
        return False
    lang = _lang(view, 0)
    interpreter = _python_exe(view) if lang == "python" else ""
    return bool(lang) and _server_spec(lang, interpreter) is not None


def _server_view(
    view: Optional[sublime.View], capability: Optional[str] = None, quiet: bool = False,
) -> Optional[tuple[lsp.Client, str]]:
    """``(client, lang)`` when ``view``'s server supports ``capability``."""
    lang = _lang(view, 0) if view and view.file_name() else None
    client = _client(view, lang) if lang else None
    if client and (capability is None or client.supports(capability)):
        return client, lang
    if not quiet:
        reason = "is still starting" if client and not client.capabilities else "cannot do that"
        sublime.status_message(
            "Intellekt: the language server %s" % reason if client
            else "Intellekt: no language server for this file"
        )
    return None


def _ask(client: lsp.Client, method: str, params: Any, handle: Callable[[Any], Any]) -> None:
    """Send a request; ``handle(result)`` runs on the main thread (None on failure)."""
    def reply(result: Any, error: Any) -> None:
        if error:
            # "Content modified" / "request cancelled" are routine while
            # typing: the next request answers. Other errors are shown.
            code = error.get("code") if isinstance(error, dict) else None
            if code not in (-32801, -32800):
                message = error.get("message") if isinstance(error, dict) else error
                sublime.set_timeout(lambda: sublime.status_message("Intellekt: %s" % message), 0)
            result = None
        sublime.set_timeout(lambda: handle(result), 0)

    client.request(method, params, reply)


def _doc_params(client: lsp.Client, view: sublime.View, lang: str, point: int) -> dict[str, Any]:
    return {
        "textDocument": {"uri": _sync(client, view, lang)},
        "position": _position(view, point),
    }


def _event_point(view: sublime.View, event: Optional[dict[str, Any]]) -> int:
    if event and "x" in event:
        return view.window_to_text((event["x"], event["y"]))
    return view.sel()[0].b if view.sel() else 0


def _range_points(view: sublime.View, rng: dict[str, Any]) -> tuple[int, int]:
    start, end = rng.get("start") or {}, rng.get("end") or {}
    return (
        _point(view, start.get("line", 0), start.get("character", 0)),
        _point(view, end.get("line", 0), end.get("character", 0)),
    )


def _run_bypassed(window: sublime.Window, name: str, args: Optional[dict[str, Any]]) -> None:
    _BYPASS.add(name)
    window.run_command(name, args or {})


def _give_up(
    window: sublime.Window, fallback: Optional[str], fallback_args: Optional[dict[str, Any]], what: str,
) -> None:
    """Hand the request to Sublime's own command, or say nothing was found."""
    if fallback:
        _run_bypassed(window, fallback, fallback_args)
    else:
        sublime.status_message("Intellekt: no %s found" % what)


def _short_path(window: sublime.Window, path: str) -> str:
    for folder in window.folders():
        if path.startswith(folder.rstrip(os.sep) + os.sep):
            return os.path.relpath(path, folder)
    home = os.path.expanduser("~")
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _open_location(window: sublime.Window, path: str, line: int, char: int, transient: bool = False) -> None:
    view = window.find_open_file(path)
    if view and not view.is_loading():
        point = _point(view, line, char)
        window.focus_view(view)
        view.sel().clear()
        view.sel().add(point)
        view.show_at_center(point)
        return
    flags = sublime.ENCODED_POSITION | (sublime.TRANSIENT if transient else 0)
    window.open_file("%s:%d:%d" % (path, line + 1, char + 1), flags)


def _pick(
    window: sublime.Window, origin: sublime.View, items: list[sublime.QuickPanelItem],
    places: list[tuple[str, int, int]], empty: Callable[[], Any],
) -> None:
    """Jump to one place, or let the user pick with a live preview."""
    if not places:
        empty()
        return
    if len(places) == 1:
        _open_location(window, *places[0])
        return
    saved = [(region.a, region.b) for region in origin.sel()]

    def chosen(index: int) -> None:
        if index >= 0:
            _open_location(window, *places[index])
            return
        window.focus_view(origin)
        origin.sel().clear()
        for a, b in saved:
            origin.sel().add(sublime.Region(a, b))
        if saved:
            origin.show(saved[0][0])

    window.show_quick_panel(
        items, chosen,
        on_highlight=lambda index: _open_location(window, *places[index], transient=True),
    )


def _pick_locations(
    window: sublime.Window, origin: sublime.View, found: list[tuple[str, int, int]], empty: Callable[[], Any],
) -> None:
    lines = {}

    def text(path: str, line: int) -> str:
        view = window.find_open_file(path)
        if view:
            return view.substr(view.line(view.text_point(line, 0))).strip()
        if path not in lines:
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    lines[path] = handle.read().splitlines()
            except OSError:
                lines[path] = []
        return lines[path][line].strip() if line < len(lines[path]) else ""

    items = [
        sublime.QuickPanelItem(
            text(path, line) or os.path.basename(path),
            annotation="%s:%d" % (_short_path(window, path), line + 1),
        )
        for path, line, _char in found
    ]
    _pick(window, origin, items, found, empty)


def _pick_symbols(window: sublime.Window, origin: sublime.View, rows: list[tuple[Any, ...]], what: str) -> None:
    items, places = [], []
    for name, kind, path, line, char, depth, detail in rows:
        path = path or origin.file_name()
        items.append(sublime.QuickPanelItem(
            "    " * depth + name,
            annotation=detail[:40] if detail else "%s:%d" % (os.path.basename(path), line + 1),
            kind=_icon(kind, name),
        ))
        places.append((path, line, char))
    _pick(window, origin, items, places,
          lambda: sublime.status_message("Intellekt: no %s found" % what))


def _apply_workspace_edit(window: Optional[sublime.Window], workspace_edit: Optional[dict[str, Any]]) -> None:
    """Apply a rename or fix: open files in Sublime (undoable), closed ones on disk."""
    changed = 0
    for path, edits in lsp.workspace_edit_files(workspace_edit).items():
        if not edits:
            continue
        view = None
        for win in [window] + sublime.windows() if window else sublime.windows():
            view = win.find_open_file(path)
            if view:
                break
        if view:
            view.run_command("intellekt_apply_edits", {"edits": edits})
        else:
            try:
                with open(path, encoding="utf-8", newline="") as handle:
                    text = handle.read()
                with open(path, "w", encoding="utf-8", newline="") as handle:
                    handle.write(lsp.apply_text_edits(text, edits))
            except (OSError, ValueError) as err:
                print("Intellekt: could not edit %s: %s" % (path, err))
                continue
        changed += 1
    sublime.status_message("Intellekt: changed %d file%s" % (changed, "" if changed == 1 else "s"))


def _run_action(client: lsp.Client, window: sublime.Window, action: dict[str, Any]) -> None:
    """Run a code action's edit, then its command (resolving it first if it has neither)."""
    if "edit" not in action and "command" not in action and action.get("data") is not None:
        _ask(client, "codeAction/resolve", action,
             lambda full: full and ("edit" in full or "command" in full) and _run_action(client, window, full))
        return
    command = action if isinstance(action.get("command"), str) else action.get("command")
    if action.get("edit"):
        _apply_workspace_edit(window, action["edit"])
    if isinstance(command, dict) and command.get("command"):
        client.request("workspace/executeCommand", {
            "command": command["command"], "arguments": command.get("arguments") or [],
        }, lambda _result, error: error and print("Intellekt: %s" % error))


def _signature_help(view: sublime.View, force: bool = False) -> None:
    """Parameter help after "(" or ","; ``force`` shows it anywhere."""
    if not (force or _feature(_lang(view, 0), "signature_help")) or len(view.sel()) != 1:
        return
    point = view.sel()[0].b
    char = view.substr(point - 1) if point else ""
    if char == ")" and view.id() in _SIG_VIEWS:
        _SIG_VIEWS.pop(view.id(), None)
        sublime.set_timeout(view.hide_popup, 0)
        return
    found = _server_view(view, "signatureHelpProvider", quiet=not force)
    if not found:
        return
    client, lang = found
    triggers = (client.capabilities.get("signatureHelpProvider") or {}).get("triggerCharacters")
    if not force and (char == ")" or char not in (triggers or ["(", ","])):
        return

    def show(result: Any) -> None:
        text = lsp.signature_markdown(result)
        if text and view.sel() and view.sel()[0].b == point:
            _SIG_VIEWS[view.id()] = view.rowcol(point)[0]
            _popup(view, point, "", text, sublime.COOPERATE_WITH_AUTO_COMPLETE)
        elif view.id() in _SIG_VIEWS:
            _SIG_VIEWS.pop(view.id(), None)
            view.hide_popup()

    _ask(client, "textDocument/signatureHelp", _doc_params(client, view, lang, point), show)


def _highlight(view: sublime.View) -> None:
    """Outline every use of the name under the caret."""
    sel = view.sel()
    if len(sel) != 1 or not sel[0].empty():
        view.erase_regions(_HIGHLIGHT_KEY)
        return
    point = sel[0].b
    if not re.match(r"\w", view.substr(view.word(point))):
        view.erase_regions(_HIGHLIGHT_KEY)
        return
    found = _server_view(view, "documentHighlightProvider", quiet=True)
    if not found:
        return
    client, lang = found

    def draw(result: Any) -> None:
        if not view.is_valid() or not view.sel() or view.sel()[0].b != point:
            return
        regions = [sublime.Region(*_range_points(view, item.get("range") or {})) for item in result or ()]
        # One hit is the name itself, which needs no outline.
        view.add_regions(_HIGHLIGHT_KEY, regions if len(regions) > 1 else [], "comment", "",
                         sublime.DRAW_NO_FILL)

    _ask(client, "textDocument/documentHighlight", _doc_params(client, view, lang, point), draw)


def _replace_edits(view: sublime.View, edit: sublime.Edit, edits: list[dict[str, Any]]) -> None:
    """Apply LSP TextEdits inside a running TextCommand, last edit first."""
    spans = []
    for item in edits:
        a, b = _range_points(view, item["range"])
        spans.append((a, b, (item.get("newText") or "").replace("\r\n", "\n")))
    for a, b, text in sorted(spans, key=lambda span: span[:2], reverse=True):
        view.replace(edit, sublime.Region(a, b), text)


class IntellektSignatureHelpCommand(sublime_plugin.TextCommand):
    def run(self, edit: sublime.Edit) -> None:
        if not _key_fallback(self.view.window(), self.view, "intellekt_signature_help"):
            _signature_help(self.view, force=True)


class IntellektApplyEditsCommand(sublime_plugin.TextCommand):
    """Apply LSP TextEdits to this view in one undo step."""

    def run(self, edit: sublime.Edit, edits: list[dict[str, Any]]) -> None:
        _replace_edits(self.view, edit, edits)


class IntellektCompleteCommand(sublime_plugin.TextCommand):
    """Insert a completion that also edits elsewhere, such as adding an import."""

    def run(
        self, edit: sublime.Edit, text: str, snippet: bool = False,
        edits: list[dict[str, Any]] = (), replace_from: Optional[list[int]] = None,
    ) -> None:
        view = self.view
        if replace_from and view.sel():
            # The server's edit starts before the typed word (postfix
            # completion): remove from there to the caret. Done before the
            # extra edits, which sit above and would shift this line.
            caret = view.sel()[0].b
            point = _point(view, *replace_from)
            if point < caret:
                view.erase(edit, sublime.Region(point, caret))
        # Edits sit above the caret (imports), so apply them next. Sublime
        # moves the caret down with them. They use this command's own edit:
        # running a second text command from inside this one corrupts text.
        if edits:
            _replace_edits(view, edit, edits)
        # Sublime normally removes the typed prefix. If it is still there,
        # remove it so it is not doubled.
        for region in [] if replace_from else reversed(list(view.sel())):
            start = region.b
            while start > 0 and re.match(r"\w", view.substr(start - 1)):
                start -= 1
            if start < region.b:
                view.erase(edit, sublime.Region(start, region.b))
        if not snippet:
            text = text.replace("\\", "\\\\").replace("$", "\\$")
        view.run_command("insert_snippet", {"contents": text})


class IntellektGotoCommand(sublime_plugin.WindowCommand):
    """Go to definition, type definition, or declaration."""

    _METHODS = {
        "definition": ("textDocument/definition", "definitionProvider"),
        "type_definition": ("textDocument/typeDefinition", "typeDefinitionProvider"),
        "declaration": ("textDocument/declaration", "declarationProvider"),
        "implementation": ("textDocument/implementation", "implementationProvider"),
    }

    def want_event(self) -> bool:
        return True

    def is_visible(self, kind: str = "definition", event: Optional[dict[str, Any]] = None, **_args: Any) -> bool:
        return _has_server(self.window.active_view())

    def run(
        self, kind: str = "definition", event: Optional[dict[str, Any]] = None,
        fallback: Optional[str] = None, fallback_args: Optional[dict[str, Any]] = None,
    ) -> None:
        view = self.window.active_view()
        if kind == "implementation" and _key_fallback(self.window, view, "intellekt_goto_implementation"):
            return
        method, capability = self._METHODS[kind]
        found = _server_view(view, capability, quiet=bool(fallback))
        what = kind.replace("_", " ")
        if not found:
            _give_up(self.window, fallback, fallback_args, what)
            return
        client, lang = found
        params = _doc_params(client, view, lang, _event_point(view, event))
        _ask(client, method, params, lambda result: _pick_locations(
            self.window, view, lsp.locations(result),
            lambda: _give_up(self.window, fallback, fallback_args, what),
        ))


class IntellektReferencesCommand(sublime_plugin.WindowCommand):
    def want_event(self) -> bool:
        return True

    def is_visible(self, event: Optional[dict[str, Any]] = None, **_args: Any) -> bool:
        return _has_server(self.window.active_view())

    def run(
        self, event: Optional[dict[str, Any]] = None,
        fallback: Optional[str] = None, fallback_args: Optional[dict[str, Any]] = None,
    ) -> None:
        view = self.window.active_view()
        found = _server_view(view, "referencesProvider", quiet=bool(fallback))
        if not found:
            _give_up(self.window, fallback, fallback_args, "references")
            return
        client, lang = found
        params = _doc_params(client, view, lang, _event_point(view, event))
        params["context"] = {"includeDeclaration": True}
        _ask(client, "textDocument/references", params, lambda result: _pick_locations(
            self.window, view, lsp.locations(result),
            lambda: _give_up(self.window, fallback, fallback_args, "references"),
        ))


class IntellektRenameCommand(sublime_plugin.TextCommand):
    def want_event(self) -> bool:
        return True

    def is_visible(self, event: Optional[dict[str, Any]] = None) -> bool:
        return _has_server(self.view)

    def run(self, edit: sublime.Edit, event: Optional[dict[str, Any]] = None) -> None:
        view = self.view
        if _key_fallback(view.window(), view, "intellekt_rename"):
            return
        found = _server_view(view, "renameProvider")
        if not found:
            return
        client, lang = found
        point = _event_point(view, event)
        word = view.substr(view.word(point))

        def done(name: str) -> None:
            if not name or name == word:
                return
            params = _doc_params(client, view, lang, point)
            params["newName"] = name
            _ask(client, "textDocument/rename", params, lambda result: (
                _apply_workspace_edit(view.window(), result) if result
                else sublime.status_message("Intellekt: nothing to rename here")
            ))

        view.window().show_input_panel("Rename to:", word, done, None, None)


class IntellektQuickFixCommand(sublime_plugin.TextCommand):
    """Code actions for the caret or selection; ``only`` narrows the kind (e.g. ["source.organizeImports"])."""

    def want_event(self) -> bool:
        return True

    def is_visible(self, event: Optional[dict[str, Any]] = None, **_args: Any) -> bool:
        return _has_server(self.view)

    def run(
        self, edit: sublime.Edit, event: Optional[dict[str, Any]] = None,
        only: Optional[list[str]] = None, whole_file: bool = False,
    ) -> None:
        view = self.view
        if not only and _key_fallback(view.window(), view, "intellekt_quick_fix"):
            return
        found = _server_view(view, "codeActionProvider")
        if not found:
            return
        client, lang = found
        if whole_file:
            region = sublime.Region(0, view.size())
        elif event:
            point = _event_point(view, event)
            region = sublime.Region(point, point)
        else:
            region = view.sel()[0]
        diagnostics = [
            entry[3] for entry in _DIAGS.get(view.id(), ())
            if entry[0].intersects(region) or entry[0].contains(region.a)
        ]
        context = {"diagnostics": diagnostics}
        if only:
            context["only"] = only
        params = {
            "textDocument": {"uri": _sync(client, view, lang)},
            "range": {"start": _position(view, region.begin()), "end": _position(view, region.end())},
            "context": context,
        }
        window = view.window()

        def pick(actions: Any) -> None:
            actions = [action for action in actions or () if not action.get("disabled")]
            if not actions and lang == "python" and "source.organizeImports" in (only or ()):
                _python_organize(view)
                return
            if not actions:
                sublime.status_message("Intellekt: nothing to fix here" if not only else "Intellekt: nothing to change")
            elif only and len(actions) == 1:
                _run_action(client, window, actions[0])
            else:
                window.show_quick_panel(
                    [action.get("title", "?") for action in actions],
                    lambda index: index >= 0 and _run_action(client, window, actions[index]),
                )

        _ask(client, "textDocument/codeAction", params, pick)


def _python_organize(view: sublime.View) -> None:
    """Sort imports with ruff, else isort (venv first); pyright has no organize imports."""
    interpreter = _python_exe(view)
    path = lsp.search_path([os.path.dirname(interpreter)] if interpreter else [])
    tool = shutil.which("ruff", path=path) or shutil.which("isort", path=path)
    if not tool:
        sublime.status_message(
            "Intellekt: no import sorter found. Install ruff or isort (pip install ruff)"
        )
        return
    name = os.path.basename(tool).split(".")[0]
    text = view.substr(sublime.Region(0, view.size()))
    try:
        tidy = core.sort_imports(tool, text, view.file_name() or "untitled.py")
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as err:
        sublime.status_message("Intellekt: %s could not sort imports: %s" % (name, err))
        return
    if tidy == text:
        sublime.status_message("Intellekt: imports are already organized")
        return
    view.run_command("intellekt_replace_text", {"text": tidy})
    sublime.status_message("Intellekt: imports organized with %s" % name)


class IntellektReplaceTextCommand(sublime_plugin.TextCommand):
    """Replace the buffer with ``text``, rewriting only the lines that differ."""

    def run(self, edit: sublime.Edit, text: str) -> None:
        view = self.view
        old = view.substr(sublime.Region(0, view.size())).splitlines(True)
        new = text.splitlines(True)
        starts = [0]
        for line in old:
            starts.append(starts[-1] + len(line))
        for tag, i1, i2, j1, j2 in reversed(
            difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
        ):
            if tag != "equal":
                view.replace(edit, sublime.Region(starts[i1], starts[i2]), "".join(new[j1:j2]))


class IntellektOrganizeImportsCommand(sublime_plugin.TextCommand):
    def is_visible(self, event: Optional[dict[str, Any]] = None) -> bool:
        return _has_server(self.view) or _lang(self.view, 0) == "python"

    def run(self, edit: sublime.Edit) -> None:
        if _lang(self.view, 0) == "python" and not _has_server(self.view):
            _python_organize(self.view)
            return
        if _key_fallback(self.view.window(), self.view, "intellekt_organize_imports"):
            return
        self.view.run_command("intellekt_quick_fix", {
            "only": ["source.organizeImports"], "whole_file": True,
        })


class IntellektDocumentSymbolsCommand(sublime_plugin.TextCommand):
    def is_visible(self) -> bool:
        return _has_server(self.view)

    def run(self, edit: sublime.Edit) -> None:
        view = self.view
        found = _server_view(view, "documentSymbolProvider")
        if not found:
            return
        client, lang = found
        _ask(client, "textDocument/documentSymbol", {"textDocument": {"uri": _sync(client, view, lang)}},
             lambda result: _pick_symbols(view.window(), view, lsp.symbol_rows(result, view.file_name()), "symbols"))


class IntellektWorkspaceSymbolsCommand(sublime_plugin.WindowCommand):
    def is_visible(self) -> bool:
        return _has_server(self.window.active_view())

    def run(self) -> None:
        view = self.window.active_view()
        if _key_fallback(self.window, view, "intellekt_workspace_symbols"):
            return
        found = _server_view(view, "workspaceSymbolProvider")
        if not found:
            return
        client, _lang = found

        def search(query: str) -> None:
            if query:
                _ask(client, "workspace/symbol", {"query": query},
                     lambda result: _pick_symbols(self.window, view, lsp.symbol_rows(result), "symbols"))

        self.window.show_input_panel("Find symbol in project:", "", search, None, None)


class IntellektCallHierarchyCommand(sublime_plugin.TextCommand):
    """Who calls the function under the caret (←), and what it calls (→)."""

    def want_event(self) -> bool:
        return True

    def is_visible(self, event: Optional[dict[str, Any]] = None) -> bool:
        return _has_server(self.view)

    def run(self, edit: sublime.Edit, event: Optional[dict[str, Any]] = None) -> None:
        view = self.view
        found = _server_view(view, "callHierarchyProvider")
        if not found:
            return
        client, lang = found
        window = view.window()

        def calls(items: Any) -> None:
            if not items:
                sublime.status_message("Intellekt: no function here")
                return
            item = items[0]
            rows = {}

            def collect(direction: str, result: Any) -> None:
                rows[direction] = result or []
                if len(rows) < 2:
                    return
                labels, places = [], []
                for direction, key, arrow in (("in", "from", "← "), ("out", "to", "→ ")):
                    for call in rows[direction]:
                        target = call.get(key) or {}
                        where = lsp.locations({"uri": target.get("uri"), "range": target.get("selectionRange") or {}})
                        if where:
                            labels.append(sublime.QuickPanelItem(
                                arrow + target.get("name", "?"),
                                annotation="%s:%d" % (os.path.basename(where[0][0]), where[0][1] + 1),
                                kind=_icon(lsp._SYMBOL_KINDS.get(target.get("kind"), ""), ""),
                            ))
                            places.append(where[0])
                _pick(window, view, labels, places,
                      lambda: sublime.status_message("Intellekt: no calls found"))

            _ask(client, "callHierarchy/incomingCalls", {"item": item}, lambda r: collect("in", r))
            _ask(client, "callHierarchy/outgoingCalls", {"item": item}, lambda r: collect("out", r))

        _ask(client, "textDocument/prepareCallHierarchy",
             _doc_params(client, view, lang, _event_point(view, event)), calls)

# ---- more server features ---------------------------------------------------
# Inlay hints, code lens, document links, semantic tokens and inactive regions
# are drawn after each sync ("decorations"). Formatting, implementation, type
# hierarchy, selection ranges and folding are commands.

_PHANTOMS = {}   # (view id, kind) -> PhantomSet
_LENSES = {}     # view id -> {href: (command, arguments)}
_LINKS = {}      # view id -> [(Region, target, raw link)]
_SEM_KEYS = {}   # view id -> token types drawn
_SEL_STACK = {}  # view id -> (expanded selection, [earlier selections])
_INLAY_HTML = (
    '<body style="margin: 0; padding: 0;"><span style="color: color(var(--foreground) a(0.45)); '
    'font-size: 0.9em; font-style: italic;">%s</span></body>'
)
_LENS_HTML = (
    '<body style="margin: 0; padding: 0; font-size: 0.8em;">'
    '<span style="color: color(var(--foreground) a(0.5));">%s%s</span></body>'
)
# Semantic token type -> the ordinary scope whose color it borrows when
# "Intellekt: Add Semantic Colors to Color Scheme" writes the rules.
_SEM_SCOPES = {
    "namespace": "entity.name.namespace", "type": "entity.name.type", "class": "entity.name.class",
    "enum": "entity.name.enum", "interface": "entity.name.interface", "struct": "entity.name.struct",
    "typeParameter": "variable.parameter.type", "parameter": "variable.parameter",
    "variable": "variable.other", "property": "variable.other.member",
    "enumMember": "constant.other.enum", "event": "variable.other", "function": "entity.name.function",
    "method": "entity.name.function", "macro": "support.function", "keyword": "keyword",
    "modifier": "storage.modifier", "comment": "comment", "string": "string",
    "number": "constant.numeric", "regexp": "string.regexp", "operator": "keyword.operator",
    "decorator": "entity.name.function.decorator",
}
_INACTIVE_SCOPE = "meta.inactive.intellekt"


def _nudge(color: str) -> str:
    """``color`` one step off in blue. Sublime ignores a rule background equal
    to the editor's own and then fills the region with the text color."""
    value = color.lstrip("#")[:6]
    if len(value) != 6:
        return color
    blue = int(value[4:6], 16)
    return "#%s%02x" % (value[:4], blue + 1 if blue < 255 else blue - 1)


def _sem_scope(kind: str) -> str:
    return "meta.semantic-token.%s.intellekt" % kind.lower()


def _has_color_rule(view: sublime.View, scope: str) -> bool:
    """True when the color scheme has a rule for ``scope``; otherwise a fill would hide the code."""
    return view.style_for_scope(scope).get("foreground") != view.style().get("foreground")


def _phantom_set(view: sublime.View, kind: str) -> sublime.PhantomSet:
    key = (view.id(), kind)
    if key not in _PHANTOMS:
        _PHANTOMS[key] = sublime.PhantomSet(view, "intellekt." + kind)
    return _PHANTOMS[key]


def _whole_range(view: sublime.View) -> dict[str, dict[str, int]]:
    last = view.rowcol(view.size())[0]
    tail = view.substr(view.line(view.size()))
    return {"start": {"line": 0, "character": 0}, "end": {"line": last, "character": lsp.utf16_len(tail)}}


def _views_of(client: lsp.Client) -> Iterator[sublime.View]:
    for window in sublime.windows():
        for view in window.views():
            path = view.file_name()
            if path and lsp.file_uri(path) in client.docs:
                yield view


def _on_notify(client: lsp.Client, method: str, params: dict[str, Any]) -> None:
    """Server notifications and refresh requests (reader thread)."""
    if method == "textDocument/inactiveRegions":
        sublime.set_timeout(lambda: _draw_inactive(params), 0)
    elif method.startswith("workspace/") and method.endswith("/refresh"):
        sublime.set_timeout(lambda: [_decorate(view) for view in _views_of(client)], 0)


def _decorate(view: sublime.View, tries: int = 0) -> None:
    """Request every drawn feature for ``view`` (after a sync)."""
    lang = _lang(view, 0) if view.is_valid() else None
    client = _client(view, lang) if lang else None
    if not client:
        return
    if not client.capabilities:
        if tries < 40:  # server still starting
            sublime.set_timeout_async(lambda: _decorate(view, tries + 1), 500)
        return
    uri = _sync(client, view, lang)
    _inlay_hints(client, view, lang, uri)
    _code_lens(client, view, lang, uri)
    _document_links(client, view, lang, uri)
    _semantic_tokens(client, view, lang, uri)


def _inlay_hints(client: lsp.Client, view: sublime.View, lang: str, uri: str) -> None:
    """Faint inline type and parameter names (clangd, rust-analyzer)."""
    # NOTE: the whole file is requested, not just the visible part. Ceiling: very
    # large files cost more per edit. Upgrade path: request the visible region
    # and refresh on scroll.
    phantoms = _phantom_set(view, "inlay")
    if not (_feature(lang, "inlay_hints") and client.supports("inlayHintProvider")):
        phantoms.update([])
        return
    version = view.change_count()

    def draw(hints: Any) -> None:
        if not view.is_valid() or view.change_count() != version:
            return
        found = []
        for hint in hints or ():
            where = hint.get("position") or {}
            point = _point(view, where.get("line", 0), where.get("character", 0))
            found.append(sublime.Phantom(
                sublime.Region(point, point), _INLAY_HTML % _escape(lsp.inlay_label(hint)),
                sublime.LAYOUT_INLINE,
            ))
        phantoms.update(found)

    _ask(client, "textDocument/inlayHint", {"textDocument": {"uri": uri}, "range": _whole_range(view)}, draw)


def _code_lens(client: lsp.Client, view: sublime.View, lang: str, uri: str) -> None:
    """Links above a line: "Run", "3 references" (rust-analyzer, ruby-lsp)."""
    phantoms = _phantom_set(view, "lens")
    if not (_feature(lang, "code_lens") and client.supports("codeLensProvider")):
        phantoms.update([])
        return
    version = view.change_count()
    resolvable = (client.capabilities.get("codeLensProvider") or {}).get("resolveProvider")

    def draw(lenses: list[dict[str, Any]]) -> None:
        if not view.is_valid() or view.change_count() != version:
            return
        by_line, actions = {}, {}
        for lens in lenses:
            found = lsp.lens_command(lens)
            # Debugging needs a debugger Intellekt does not have.
            if found and "debug" not in (found[1] + found[0]).lower():
                by_line.setdefault((lens.get("range") or {}).get("start", {}).get("line", 0), []).append(found)
        shown = []
        for line, commands in sorted(by_line.items()):
            links = []
            for title, command, arguments in commands:
                href = str(len(actions))
                actions[href] = (command, arguments)
                links.append('<a href="%s">%s</a>' % (href, _escape(title)))
            start = view.text_point(line, 0)
            text = view.substr(view.line(start))
            indent = "&nbsp;" * (len(text) - len(text.lstrip()))
            # A block phantom shows under its line, so anchor on the line above.
            anchor = view.line(view.text_point(line - 1, 0)).b if line else start
            shown.append(sublime.Phantom(
                sublime.Region(anchor, anchor), _LENS_HTML % (indent, " | ".join(links)),
                sublime.LAYOUT_BLOCK, on_navigate=lambda href, v=view: _run_lens(client, v, href),
            ))
        _LENSES[view.id()] = actions
        phantoms.update(shown)

    def received(lenses: Any) -> None:
        lenses = list(lenses or [])
        pending = [i for i, lens in enumerate(lenses) if not lens.get("command")] if resolvable else []
        if not pending:
            draw(lenses)
            return
        left = [len(pending)]

        def resolved(index: int, value: Any) -> None:
            if isinstance(value, dict):
                lenses[index] = value
            left[0] -= 1
            if not left[0]:
                draw(lenses)

        for index in pending:
            _ask(client, "codeLens/resolve", lenses[index], lambda value, index=index: resolved(index, value))

    _ask(client, "textDocument/codeLens", {"textDocument": {"uri": uri}}, received)


def _run_lens(client: lsp.Client, view: sublime.View, href: str) -> None:
    """Run a code lens; common rust-analyzer/ruby-lsp editor commands are emulated here."""
    command, args = _LENSES.get(view.id(), {}).get(href, ("", []))
    window = view.window()
    if not window or not command:
        return
    env = {"PATH": lsp.search_path()}
    if command in ("rust-analyzer.runSingle", "rust-analyzer.run") and args:
        runnable = (args[0] or {}).get("args") or {}
        env.update(runnable.get("environment") or {})
        cmd = [runnable.get("overrideCargo") or "cargo"] + list(runnable.get("cargoArgs") or [])
        if runnable.get("executableArgs"):
            cmd += ["--"] + list(runnable["executableArgs"])
        cwd = runnable.get("workspaceRoot") or runnable.get("cwd") or client.root
        window.run_command("exec", {"cmd": cmd, "working_dir": cwd, "env": env})
    elif command == "rust-analyzer.gotoLocation" and args:
        where = lsp.locations(args[0])
        if where:
            _open_location(window, *where[0])
    elif command in ("rust-analyzer.showReferences", "editor.action.showReferences") and len(args) >= 3:
        _pick_locations(window, view, lsp.locations(args[2]),
                        lambda: sublime.status_message("Intellekt: nothing found"))
    elif command.startswith("rubyLsp.run") and len(args) >= 3 and isinstance(args[2], str):
        window.run_command("exec", {"shell_cmd": args[2], "working_dir": client.root, "env": env})
    elif command in ((client.capabilities.get("executeCommandProvider") or {}).get("commands") or ()):
        client.request("workspace/executeCommand", {"command": command, "arguments": args},
                       lambda _result, error: error and print("Intellekt: %s" % error))
    else:
        sublime.status_message("Intellekt: %s needs editor support Intellekt does not have" % command)


def _document_links(client: lsp.Client, view: sublime.View, lang: str, uri: str) -> None:
    """Underline links such as C/C++ #include paths; hover one to open it."""
    if not (_feature(lang, "document_links") and client.supports("documentLinkProvider")):
        view.erase_regions("intellekt.links")
        _LINKS.pop(view.id(), None)
        return
    version = view.change_count()

    def draw(links: Any) -> None:
        if not view.is_valid() or view.change_count() != version:
            return
        found = []
        for link in links or ():
            a, b = _range_points(view, link.get("range") or {})
            found.append((sublime.Region(a, b), link.get("target") or "", link))
        _LINKS[view.id()] = found
        view.add_regions("intellekt.links", [item[0] for item in found], "markup.underline.link", "",
                         sublime.DRAW_SOLID_UNDERLINE | sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE)

    _ask(client, "textDocument/documentLink", {"textDocument": {"uri": uri}}, draw)


def _open_target(window: sublime.Window, target: str) -> None:
    """Open a document link: a file (optionally ``#L12``) or a web page."""
    if target.startswith("file:"):
        line = re.search(r"#L(\d+)", target)
        path = lsp.uri_path(target.split("#", 1)[0])
        window.open_file("%s:%s" % (path, line.group(1)) if line else path, sublime.ENCODED_POSITION)
    elif target.startswith(("http://", "https://")):
        sublime.run_command("open_url", {"url": target})


def _link_popup(view: sublime.View, point: int) -> bool:
    """Show "Open ..." for a document link under ``point``. True if shown."""
    for region, target, raw in _LINKS.get(view.id(), ()):
        if region.contains(point):
            lang = _lang(view, point)
            client = _client(view, lang) if lang else None

            def open_link(href: str, target: str = target, raw: dict[str, Any] = raw) -> None:
                if href != "open":
                    return
                if target:
                    _open_target(view.window(), target)
                elif client and (client.capabilities.get("documentLinkProvider") or {}).get("resolveProvider"):
                    _ask(client, "documentLink/resolve", raw,
                         lambda full: full and full.get("target") and _open_target(view.window(), full["target"]))

            name = os.path.basename(lsp.uri_path(target.split("#", 1)[0])) if target else view.substr(region)
            view.show_popup(
                '<body style="font-size: 0.85em"><a href="open">Open %s</a></body>' % _escape(name),
                flags=sublime.HIDE_ON_MOUSE_MOVE_AWAY, location=point, on_navigate=open_link,
            )
            return True
    return False


def _semantic_tokens(client: lsp.Client, view: sublime.View, lang: str, uri: str) -> None:
    """Server-based coloring (off by default; needs "Add Semantic Colors" rules)."""
    keys = _SEM_KEYS.pop(view.id(), set())
    if not (_feature(lang, "semantic_highlighting") and client.supports("semanticTokensProvider")):
        for kind in keys:
            view.erase_regions("intellekt.sem." + kind)
        return
    _SEM_KEYS[view.id()] = keys
    legend = (client.capabilities.get("semanticTokensProvider") or {}).get("legend") or {}
    version = view.change_count()

    def draw(result: Any) -> None:
        if not view.is_valid() or view.change_count() != version:
            return
        by_kind = {}
        for line, char, length, kind, _mods in lsp.semantic_tokens((result or {}).get("data"), legend):
            if kind:
                by_kind.setdefault(kind, []).append(
                    sublime.Region(_point(view, line, char), _point(view, line, char + length))
                )
        for kind in _SEM_KEYS.get(view.id(), set()) - set(by_kind):
            view.erase_regions("intellekt.sem." + kind)
        drawn = set()
        for kind, regions in by_kind.items():
            scope = _sem_scope(kind)
            if _has_color_rule(view, scope):
                view.add_regions("intellekt.sem." + kind, regions, scope, "", sublime.DRAW_NO_OUTLINE)
                drawn.add(kind)
        _SEM_KEYS[view.id()] = drawn

    _ask(client, "textDocument/semanticTokens/full", {"textDocument": {"uri": uri}}, draw)


def _draw_inactive(params: dict[str, Any]) -> None:
    """clangd's inactive regions: code an #if / #ifdef leaves out."""
    target = lsp.uri_path((params.get("textDocument") or {}).get("uri", ""))
    for window in sublime.windows():
        for view in window.views():
            path = view.file_name()
            if not path or not lsp.same_path(path, target):
                continue
            if not _feature(_lang(view, 0), "inactive_regions"):
                view.erase_regions("intellekt.inactive")
                continue
            regions = [sublime.Region(*_range_points(view, rng)) for rng in params.get("regions") or ()]
            if _has_color_rule(view, _INACTIVE_SCOPE):
                view.add_regions("intellekt.inactive", regions, _INACTIVE_SCOPE, "", sublime.DRAW_NO_OUTLINE)
            else:
                view.add_regions("intellekt.inactive", regions, "comment", "",
                                 sublime.DRAW_STIPPLED_UNDERLINE | sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE)


def _format_options(view: sublime.View) -> dict[str, Any]:
    return {
        "tabSize": int(view.settings().get("tab_size", 4)),
        "insertSpaces": bool(view.settings().get("translate_tabs_to_spaces", False)),
    }


def _format_request(
    client: lsp.Client, view: sublime.View, lang: str, selection: bool, callback: Callable[[Any, Any], None],
) -> bool:
    """Send a (range) formatting request. False when the server cannot."""
    uri = _sync(client, view, lang)
    region = view.sel()[0] if selection and len(view.sel()) == 1 and not view.sel()[0].empty() else None
    if region and client.supports("documentRangeFormattingProvider"):
        client.request("textDocument/rangeFormatting", {
            "textDocument": {"uri": uri}, "options": _format_options(view),
            "range": {"start": _position(view, region.begin()), "end": _position(view, region.end())},
        }, callback)
        return True
    if client.supports("documentFormattingProvider"):
        client.request("textDocument/formatting", {
            "textDocument": {"uri": uri}, "options": _format_options(view),
        }, callback)
        return True
    return False


def _python_tool(
    view: sublime.View, names: list[str], run: Callable[[str, str, str], str], verb: str, quiet: bool = False,
) -> None:
    """Run the first of ``names`` found for the view's Python; ``run(tool, text, path)`` -> new text."""
    interpreter = _python_exe(view)
    path = lsp.search_path([os.path.dirname(interpreter)] if interpreter else [])
    tool = next((found for found in (shutil.which(name, path=path) for name in names) if found), None)
    if not tool:
        if not quiet:
            sublime.status_message("Intellekt: no tool found to %s. Install %s (pip install %s)"
                                   % (verb, " or ".join(names), names[0]))
        return
    name = os.path.basename(tool).split(".")[0]
    text = view.substr(sublime.Region(0, view.size()))
    try:
        new = run(tool, text, view.file_name() or "untitled.py")
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as err:
        sublime.status_message("Intellekt: %s could not %s: %s" % (name, verb, err))
        return
    if new != text:
        view.run_command("intellekt_replace_text", {"text": new})
    if not quiet:
        sublime.status_message("Intellekt: %s with %s" % ("done" if new != text else "nothing to change", name))


def _format_on_type(view: sublime.View) -> None:
    """Server fixes after typing a trigger such as ";" or Enter. Off by default."""
    lang = _lang(view, 0) if view.file_name() else None
    if not lang or len(view.sel()) != 1 or not _feature(lang, "format_on_type"):
        return
    client = _client(view, lang)
    provider = (client.capabilities.get("documentOnTypeFormattingProvider") or {}) if client else {}
    point = view.sel()[0].b
    char = view.substr(point - 1) if point else ""
    if not provider or char not in [provider.get("firstTriggerCharacter")] + list(provider.get("moreTriggerCharacter") or ()):
        return
    params = _doc_params(client, view, lang, point)
    params.update({"ch": char, "options": _format_options(view)})
    version = view.change_count()
    _ask(client, "textDocument/onTypeFormatting", params, lambda edits: (
        edits and view.change_count() == version and view.run_command("intellekt_apply_edits", {"edits": edits})
    ))


class IntellektFormatCommand(sublime_plugin.TextCommand):
    """Format the file, or the selection when the server supports that."""

    def is_visible(self, selection: bool = True) -> bool:
        return _has_server(self.view) or _lang(self.view, 0) == "python"

    def run(self, edit: sublime.Edit, selection: bool = True) -> None:
        view = self.view
        lang = _lang(view, 0)
        client = _client(view, lang) if lang and _has_server(view) else None
        can = client and (client.supports("documentFormattingProvider") or client.supports("documentRangeFormattingProvider"))
        if lang == "python" and not can:
            # pyright cannot format; ruff or black can.
            _python_tool(view, ["ruff", "black"], core.format_python, "format")
            return
        if _key_fallback(view.window(), view, "intellekt_format"):
            return
        if not client:
            return

        def apply(edits: Any, error: Any) -> None:
            def done() -> None:
                if error:
                    sublime.status_message("Intellekt: %s" % (error.get("message") if isinstance(error, dict) else error))
                elif edits:
                    view.run_command("intellekt_apply_edits", {"edits": edits})
                    sublime.status_message("Intellekt: formatted")
                else:
                    sublime.status_message("Intellekt: already formatted")
            sublime.set_timeout(done, 0)

        if not _format_request(client, view, lang, selection, apply):
            sublime.status_message("Intellekt: this language server cannot format")


class IntellektTypeHierarchyCommand(sublime_plugin.TextCommand):
    """Parent (↑) and child (↓) types of the type under the caret."""

    def want_event(self) -> bool:
        return True

    def is_visible(self, event: Optional[dict[str, Any]] = None) -> bool:
        return _has_server(self.view)

    def run(self, edit: sublime.Edit, event: Optional[dict[str, Any]] = None) -> None:
        view = self.view
        found = _server_view(view, "typeHierarchyProvider")
        if not found:
            return
        client, lang = found
        window = view.window()

        def types(items: Any) -> None:
            if not items:
                sublime.status_message("Intellekt: no type here")
                return
            rows = {}

            def collect(direction: str, result: Any) -> None:
                rows[direction] = result or []
                if len(rows) < 2:
                    return
                labels, places = [], []
                for direction, arrow in (("super", "↑ "), ("sub", "↓ ")):
                    for item in rows[direction]:
                        where = lsp.locations({"uri": item.get("uri"), "range": item.get("selectionRange") or {}})
                        if where:
                            labels.append(sublime.QuickPanelItem(
                                arrow + item.get("name", "?"),
                                annotation="%s:%d" % (os.path.basename(where[0][0]), where[0][1] + 1),
                                kind=_icon(lsp._SYMBOL_KINDS.get(item.get("kind"), ""), ""),
                            ))
                            places.append(where[0])
                _pick(window, view, labels, places,
                      lambda: sublime.status_message("Intellekt: no parent or child types"))

            _ask(client, "typeHierarchy/supertypes", {"item": items[0]}, lambda r: collect("super", r))
            _ask(client, "typeHierarchy/subtypes", {"item": items[0]}, lambda r: collect("sub", r))

        _ask(client, "textDocument/prepareTypeHierarchy",
             _doc_params(client, view, lang, _event_point(view, event)), types)


class IntellektExpandSelectionCommand(sublime_plugin.TextCommand):
    """Grow each selection to the next enclosing piece of syntax."""

    def run(self, edit: sublime.Edit) -> None:
        view = self.view
        if _key_fallback(view.window(), view, "intellekt_expand_selection"):
            return
        found = _server_view(view, "selectionRangeProvider")
        if not found:
            return
        client, lang = found
        before = list(view.sel())
        params = {
            "textDocument": {"uri": _sync(client, view, lang)},
            "positions": [_position(view, region.b) for region in before],
        }

        def apply(result: Any) -> None:
            grown = []
            for region, item in zip(before, result or ()):
                for rng in lsp.selection_chain(item):
                    a, b = _range_points(view, rng)
                    if a <= region.begin() and b >= region.end() and b - a > region.size():
                        grown.append(sublime.Region(a, b))
                        break
                else:
                    grown.append(region)
            if not grown or grown == before:
                return
            _, history = _SEL_STACK.get(view.id(), (None, []))
            if _SEL_STACK.get(view.id(), (None,))[0] != [(r.a, r.b) for r in before]:
                history = []
            history.append([(r.a, r.b) for r in before])
            _SEL_STACK[view.id()] = ([(r.a, r.b) for r in grown], history)
            view.sel().clear()
            view.sel().add_all(grown)

        _ask(client, "textDocument/selectionRange", params, apply)


class IntellektShrinkSelectionCommand(sublime_plugin.TextCommand):
    """Undo the last "expand selection"."""

    def run(self, edit: sublime.Edit) -> None:
        view = self.view
        if _key_fallback(view.window(), view, "intellekt_shrink_selection"):
            return
        current, history = _SEL_STACK.get(view.id(), (None, []))
        if not history or current != [(r.a, r.b) for r in view.sel()]:
            return
        previous = history.pop()
        _SEL_STACK[view.id()] = (previous, history)
        view.sel().clear()
        view.sel().add_all([sublime.Region(a, b) for a, b in previous])


class IntellektFoldCommand(sublime_plugin.TextCommand):
    """Fold using the server's block ranges. ``which``: here, all, imports, comments."""

    def is_visible(self, which: str = "here") -> bool:
        return _has_server(self.view)

    def run(self, edit: sublime.Edit, which: str = "here") -> None:
        view = self.view
        found = _server_view(view, "foldingRangeProvider")
        if not found:
            return
        client, lang = found

        def apply(ranges: Any) -> None:
            ranges = [r for r in ranges or () if r.get("endLine", 0) > r.get("startLine", 0)]
            if which in ("imports", "comments"):
                ranges = [r for r in ranges if r.get("kind") == which]
            if which == "here":
                line = view.rowcol(view.sel()[0].b)[0] if view.sel() else 0
                ranges = sorted(
                    (r for r in ranges if r["startLine"] <= line <= r["endLine"]),
                    key=lambda r: r["endLine"] - r["startLine"],
                )[:1]
            regions = [
                sublime.Region(view.line(view.text_point(r["startLine"], 0)).b,
                               view.line(view.text_point(r["endLine"], 0)).b)
                for r in ranges
            ]
            # Fold only the outermost ranges; nested ones are inside them.
            outer = [r for r in regions if not any(o != r and o.contains(r) for o in regions)]
            if outer:
                view.fold(outer)
            else:
                sublime.status_message("Intellekt: nothing to fold here")

        _ask(client, "textDocument/foldingRange", {"textDocument": {"uri": _sync(client, view, lang)}}, apply)


class IntellektSwitchSourceHeaderCommand(sublime_plugin.WindowCommand):
    """C/C++ (clangd): jump between foo.cpp and foo.h."""

    def is_visible(self) -> bool:
        view = self.window.active_view()
        return bool(view) and _lang(view, 0) == "cpp" and _has_server(view)

    def run(self) -> None:
        view = self.window.active_view()
        found = _server_view(view)
        if not found:
            return
        client, lang = found
        _ask(client, "textDocument/switchSourceHeader", {"uri": _sync(client, view, lang)},
             lambda uri: self.window.open_file(lsp.uri_path(uri)) if uri
             else sublime.status_message("Intellekt: no matching source or header"))


class IntellektToggleCommand(sublime_plugin.ApplicationCommand):
    """Turn a feature (inlay_hints, code_lens, ...) on or off everywhere."""

    def run(self, name: str) -> None:
        settings = _settings()
        key = "lsp_" + name
        settings.set(key, not settings.get(key, _FEATURE_DEFAULTS.get(name, True)))
        sublime.save_settings(_SETTINGS)
        sublime.status_message("Intellekt: %s %s" % (name.replace("_", " "), "on" if settings.get(key) else "off"))
        window = sublime.active_window()
        view = window.active_view() if window else None
        if view:
            sublime.set_timeout_async(lambda: _decorate(view), 0)


class IntellektAddSemanticColorsCommand(sublime_plugin.WindowCommand):
    """Write semantic-token and inactive-code color rules into the user's color scheme."""

    def run(self) -> None:
        view = self.window.active_view()
        if not view:
            return
        scheme = view.settings().get("color_scheme") or "Mariana.sublime-color-scheme"
        name = os.path.splitext(os.path.basename(scheme))[0] + ".sublime-color-scheme"
        path = os.path.join(sublime.packages_path(), "User", name)
        data = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    data = sublime.decode_value(handle.read()) or {}
            except (OSError, ValueError) as err:
                sublime.error_message("Intellekt: could not read %s:\n%s" % (path, err))
                return
        background = _nudge(view.style().get("background") or "#000000")
        rules = [rule for rule in data.get("rules") or [] if not str(rule.get("scope", "")).endswith(".intellekt")]
        for kind, base in _SEM_SCOPES.items():
            rules.append({
                "name": "Intellekt: %s" % kind, "scope": _sem_scope(kind),
                "foreground": view.style_for_scope(base).get("foreground"), "background": background,
            })
        rules.append({
            "name": "Intellekt: inactive code", "scope": _INACTIVE_SCOPE,
            "foreground": view.style_for_scope("comment").get("foreground"), "background": background,
        })
        data["rules"] = rules
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(sublime.encode_value(data, pretty=True))
        sublime.message_dialog(
            "Intellekt added semantic colors to:\n%s\n\nTurn them on with "
            "\"lsp_semantic_highlighting\" and \"lsp_inactive_regions\" in the settings." % path
        )


def _forget() -> None:
    _COMMANDS.clear()
    _VENVS.clear()
    _PY_SEEN.clear()
    lsp._verified.clear()
    lsp._gem_dirs.clear()


class IntellektRebuildIndexCommand(sublime_plugin.ApplicationCommand):
    def run(self) -> None:
        _forget()
        _spawn(lambda: ensure_indexes(True))


class IntellektRestartServersCommand(sublime_plugin.ApplicationCommand):
    def run(self) -> None:
        _stop_clients()
        _forget()
        view = sublime.active_window().active_view()
        if view:
            sublime.set_timeout_async(lambda: _warm(view), 0)


class IntellektIndexStatusCommand(sublime_plugin.WindowCommand):
    def run(self) -> None:
        view = self.window.active_view()
        detected = _detected(view)
        exe = _python_exe(view)
        others = tuple(lang for lang in core.LANGS if lang != "python")
        lines = [
            core.status_text(_python_db(exe), detected, ("python",))
            + "  [%s]" % (exe or "no interpreter"),
            core.status_text(_db_path(), detected, others),
            "",
            "Language servers:",
        ]
        running = {key[2][0] for key, client in _CLIENTS.items() if not client.dead}
        for lang in lsp.SERVERS:
            spec = _server_spec(lang, exe if lang == "python" else "")
            if not spec:
                lines.append("%s: none found (index only)" % lang)
                continue
            state = "running" if spec[0][0] in running else "idle"
            lines.append("%s: %s (%s)" % (lang, spec[0][0], state))
        sublime.message_dialog("\n".join(lines))
