"""Completions and hover for the languages in ``core.LANGS``.

A language server is used when one is installed (see ``lsp.SERVERS``).
Otherwise, or when it is slow or returns nothing, completions and hover
text come from a SQLite index of the toolchains on this machine. Nothing
is downloaded and no third-party Python package is imported.

Python is indexed per interpreter. The virtual environment of the file
being edited is found by ``core.find_venv`` and gets its own cache file.
"""

import hashlib
import os
import re
import shutil
import threading

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
_MEMBER_FLAGS = sublime.INHIBIT_WORD_COMPLETIONS | sublime.DYNAMIC_COMPLETIONS
_BARE_FLAGS = sublime.DYNAMIC_COMPLETIONS
# One letter each. Sublime draws it small in the completion row, in the
# theme color for that kind, instead of a color emoji.
_KINDS = {
    "function": (sublime.KIND_ID_FUNCTION, "f", "Function"),
    "type": (sublime.KIND_ID_TYPE, "c", "Class"),
    "variable": (sublime.KIND_ID_VARIABLE, "p", "Property"),
    "namespace": (sublime.KIND_ID_NAMESPACE, "m", "Module"),
    "keyword": sublime.KIND_KEYWORD,
    "snippet": sublime.KIND_SNIPPET,
}
_KIND_CONSTANT = (sublime.KIND_ID_VARIABLE, "k", "Constant")
_KIND_FALLBACK = (sublime.KIND_ID_AMBIGUOUS, "•", "Symbol")
# NOTE: only the start of a very large buffer is scanned for imports and
# assignments. Upgrade path: a background parse of the whole buffer.
_SOURCE_CAP = 400_000
_ROOT_MARKERS = (
    ".git", "go.mod", "Cargo.toml", "pyproject.toml", "setup.py",
    "package.json", "compile_commands.json", "composer.json", "Gemfile",
)

_CLIENTS = {}    # (window id, root, command, interpreter) -> lsp.Client
_COMMANDS = {}   # (lang, interpreter) -> server spec or None
_VENVS = {}      # (file, folders) -> interpreter
_PY_SEEN = set()  # interpreters whose index was checked this session


def plugin_loaded():
    _spawn(_startup)


def _startup():
    # NOTE: this widens PATH for the whole plugin host, other packages
    # included, so every tool lookup and child process (npm, go env, the
    # indexers) finds what the user's terminal finds.
    os.environ["PATH"] = lsp.search_path()
    core.drop_language(_db_path(), "python")
    ensure_indexes(False)


def plugin_unloaded():
    _stop_clients()


def _spawn(fn):
    # Indexing can take a minute. Its own thread keeps Sublime's async
    # thread free for completions and hover.
    threading.Thread(target=fn, daemon=True).start()


def _status(message):
    sublime.set_timeout(lambda: sublime.status_message(message), 0)


def _settings():
    return sublime.load_settings(_SETTINGS)


def _cache_dir():
    return os.path.join(sublime.cache_path(), "Intellekt")


def _python_db(exe):
    """One cache per interpreter, so each venv keeps its own site-packages."""
    key = hashlib.sha1(exe.encode("utf-8")).hexdigest()[:12]
    return os.path.join(_cache_dir(), "python-%s.sqlite" % key)


def _db_path(view=None, lang=None):
    if lang == "python":
        return _python_db(_python_exe(view))
    return os.path.join(_cache_dir(), "index.sqlite")


def _python_exe(view=None):
    """The interpreter for ``view``: the setting, its venv, or the system one."""
    chosen = _settings().get("python_executable") or ""
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
            or shutil.which("python3", path=lsp.search_path())
            or shutil.which("python", path=lsp.search_path())
            or ""
        )
    return _VENVS[key]


def _is_venv(exe):
    return os.path.isfile(os.path.join(os.path.dirname(os.path.dirname(exe)), "pyvenv.cfg"))


def _detected(view=None):
    settings = _settings()
    sources = settings.get("java_sources") or []
    if not isinstance(sources, list):
        sources = []
    return core.detect_all(
        python_executable=_python_exe(view),
        ruby_executable=settings.get("ruby_executable") or "",
        java_home=settings.get("java_home") or "",
        java_sources=sources,
        node_executable=settings.get("node_executable") or "",
        tsc_executable=settings.get("tsc_executable") or "",
        dotnet_root=settings.get("dotnet_root") or "",
        go_executable=settings.get("go_executable") or "",
        php_executable=settings.get("php_executable") or "",
        rustc_executable=settings.get("rustc_executable") or "",
        cxx_compiler=settings.get("cxx_compiler") or "",
        perl_executable=settings.get("perl_executable") or "",
        matlab_executable=settings.get("matlab_executable") or "",
    )


def _produce(lang, info):
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


def ensure_indexes(force, python_exe=None):
    """Build stale indexes. With ``python_exe``, only that interpreter's.

    A full build skips when another is running. A single-interpreter build
    waits for it, so a venv opened during the first build is still indexed.
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


def _ensure_python_index(view):
    exe = _python_exe(view)
    if exe and exe not in _PY_SEEN:
        _PY_SEEN.add(exe)
        _spawn(lambda: ensure_indexes(False, python_exe=exe))


# ---- language servers -------------------------------------------------------

def _server_spec(lang, interpreter):
    """``(argv, env, settings, init_options)`` for ``lang``, or None."""
    key = (lang, interpreter)
    if key in _COMMANDS:
        return _COMMANDS[key]
    override = (_settings().get("lsp_servers") or {}).get(lang)
    extra, init = {}, {}
    if isinstance(override, dict):
        extra = override.get("settings") or {}
        init = dict(override.get("initialization_options") or {})
        override = override.get("command")
    bins = [os.path.dirname(interpreter)] if interpreter else []
    node = _settings().get("node_executable") or shutil.which("node", path=lsp.search_path())
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
        settings.update(extra)
        spec = (cmd, env, settings, init)
    _COMMANDS[key] = spec
    return spec


def _root(window, path):
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


def _client(view, lang):
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
        _CLIENTS[key] = client
        client.start()
    # NOTE: a server that exited is not restarted, so a crash loop cannot
    # spin. "Intellekt: Restart Language Servers" starts it again.
    return None if client.dead else client


def _stop_clients(window_id=None):
    for key, client in list(_CLIENTS.items()):
        if window_id is None or key[0] == window_id:
            client.stop()
            del _CLIENTS[key]


def _sync(client, view, lang):
    text = view.substr(sublime.Region(0, view.size()))
    return client.sync(view.file_name(), lang, text, view.change_count())


def _position(view, point):
    row, _col = view.rowcol(point)
    start = view.text_point(row, 0)
    return {"line": row, "character": lsp.utf16_len(view.substr(sublime.Region(start, point)))}


def _items_from_rows(rows):
    items = []
    for trigger, annotation, text, snippet, kind, doc in rows:
        icon = _icon(kind, trigger)
        details = _details(doc[:300])
        if snippet:
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


def _lsp_completions(view, point, prefix, lang, client):
    """Ask the server. The index answers if it fails, is empty, or is slow."""
    result = sublime.CompletionList()
    lock = threading.Lock()
    state = {"done": False}
    start = point - len(prefix)
    member = core.member_op(view.substr(sublime.Region(max(0, start - 3), start)))[0] is not None

    def finish(items, flags):
        with lock:
            if state["done"]:
                return
            state["done"] = True
        sublime.set_timeout(lambda: result.set_completions(items, flags), 0)

    def use_index():
        if state["done"]:
            return
        found = _index_completions(view, point, prefix, lang)
        if found:
            finish(found.completions, found.flags)
        else:
            finish([], 0)

    def on_reply(reply, error):
        rows, incomplete = lsp.completion_rows(reply) if not error else ([], False)
        if not rows:
            use_index()
            return
        flags = sublime.INHIBIT_WORD_COMPLETIONS if member else 0
        if incomplete:
            flags |= sublime.DYNAMIC_COMPLETIONS
        finish(_items_from_rows(rows), flags)

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

    def timed_out():
        if rid is not None and not state["done"]:
            client.cancel(rid)
        use_index()

    timeout = _settings().get("lsp_completion_timeout_ms", 1500) / 1000.0
    threading.Timer(timeout, timed_out).start()
    return result


# ---- index completions ------------------------------------------------------

def _lang(view, point):
    for selector, lang in core.LANG_SELECTORS:
        if view.match_selector(point, selector):
            return lang
    return None


def _source(view):
    size = min(view.size(), _SOURCE_CAP)
    return view.substr(sublime.Region(0, size))


def _details(doc):
    text = re.sub(r"\s+", " ", (doc or "")).strip()
    if not text:
        return ""
    return _escape(text)


def _open_link(href):
    if href.startswith("https://") or href.startswith("http://"):
        sublime.run_command("open_url", {"url": href})


def _escape(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _icon(kind, name=""):
    if kind == "constant" or (kind == "variable" and len(name) > 1 and name.isupper()):
        return _KIND_CONSTANT
    return _KINDS.get(kind, _KIND_FALLBACK)


def _annotation(signature):
    """Show argument names in the panel without snippet placeholders."""
    text = re.sub(r"\$\{\d+:([^}]*)\}", r"\1", signature or "")
    return text[:60]


def _items_from_symbols(rows):
    items = []
    for name, kind, signature, doc in rows:
        snippet = signature or name
        items.append(sublime.CompletionItem.snippet_completion(
            name,
            snippet,
            annotation=_annotation(snippet),
            kind=_icon(kind, name),
            details=_details(doc),
        ))
    return items


def _member_completions(view, point, prefix, lang, db_path):
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
    return sublime.CompletionList(_items_from_symbols(rows), _MEMBER_FLAGS)


def _markup_completions(view, point, lang, db_path):
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
    return sublime.CompletionList(_items_from_symbols(rows), _MEMBER_FLAGS)


def _import_completions(view, point, lang, db_path):
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
    for segment in segments[:80]:
        items.append(sublime.CompletionItem(
            segment["trigger"],
            annotation=segment["annotation"],
            completion=segment["trigger"],
            kind=_icon(segment["kind"], segment["trigger"]),
            details=_details(segment["doc"]),
        ))
    return sublime.CompletionList(items, _MEMBER_FLAGS)


def _bare_completions(view, prefix, lang, db_path):
    if not prefix:
        return None
    rows = core.bare_names(db_path, lang, _source(view), prefix)
    if not rows:
        return None
    items = []
    for trigger, annotation, kind, doc in rows[:40]:
        items.append(sublime.CompletionItem(
            trigger,
            annotation=annotation,
            completion=trigger,
            kind=_icon(kind, trigger),
            details=_details(doc),
        ))
    return sublime.CompletionList(items, _BARE_FLAGS)


def _index_completions(view, point, prefix, lang):
    db_path = _db_path(view, lang)
    return (
        _markup_completions(view, point, lang, db_path)
        or _import_completions(view, point, lang, db_path)
        or _member_completions(view, point, prefix, lang, db_path)
        or _bare_completions(view, prefix, lang, db_path)
    )


# ---- hover ------------------------------------------------------------------

def _popup(view, point, title, markdown):
    if not view.is_valid():
        return
    head = "<b>%s</b><br><br>" % _escape(title) if title else ""
    view.show_popup(
        '<body style="font-size: 0.85em">%s%s</body>' % (head, core.markdown_html(markdown)),
        flags=sublime.HIDE_ON_MOUSE_MOVE_AWAY,
        location=point,
        max_width=480,
        max_height=320,
        on_navigate=_open_link,
    )


def _index_hover(view, point, lang):
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


def _warm(view):
    """Index the venv and start the server early, so the first completion
    does not wait for the server to load the project."""
    lang = _lang(view, 0)
    if not lang:
        return
    if lang == "python":
        _ensure_python_index(view)
    client = _client(view, lang)
    if client:
        _sync(client, view, lang)


class IntellektListener(sublime_plugin.EventListener):
    def on_activated(self, view):
        self._ensure_triggers(view)

    def on_activated_async(self, view):
        _warm(view)

    def on_load(self, view):
        self._ensure_triggers(view)

    def on_close(self, view):
        path = view.file_name()
        if path:
            for client in list(_CLIENTS.values()):
                client.close(path)

    def on_post_save_async(self, view):
        path = view.file_name()
        if path:
            for client in list(_CLIENTS.values()):
                client.saved(path)

    def on_pre_close_window(self, window):
        _stop_clients(window.id())

    def _ensure_triggers(self, view):
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

    def on_query_completions(self, view, prefix, locations):
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
        return _index_completions(view, point, prefix, lang)

    def on_hover(self, view, point, zone):
        if zone != sublime.HOVER_TEXT:
            return
        if not view.match_selector(point, _SCOPE):
            return
        lang = _lang(view, point)
        if not lang:
            return
        client = _client(view, lang)
        if not client:
            _index_hover(view, point, lang)
            return

        def on_reply(reply, error):
            text = "" if error else lsp.hover_text(reply)
            if text:
                sublime.set_timeout(lambda: _popup(view, point, "", text), 0)
            else:
                sublime.set_timeout(lambda: _index_hover(view, point, lang), 0)

        client.request("textDocument/hover", {
            "textDocument": {"uri": _sync(client, view, lang)},
            "position": _position(view, point),
        }, on_reply)


def _forget():
    _COMMANDS.clear()
    _VENVS.clear()
    _PY_SEEN.clear()
    lsp._verified.clear()


class IntellektRebuildIndexCommand(sublime_plugin.ApplicationCommand):
    def run(self):
        _forget()
        _spawn(lambda: ensure_indexes(True))


class IntellektRestartServersCommand(sublime_plugin.ApplicationCommand):
    def run(self):
        _stop_clients()
        _forget()
        view = sublime.active_window().active_view()
        if view:
            sublime.set_timeout_async(lambda: _warm(view), 0)


class IntellektIndexStatusCommand(sublime_plugin.WindowCommand):
    def run(self):
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
