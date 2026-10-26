"""Offline completions for the languages in ``core.LANGS``.

On load, Intellekt indexes the toolchains already installed on this
machine into a SQLite cache. Completions and hover text are read from that
cache. Nothing is downloaded and no third-party Python package is imported.
"""

import os
import re
import threading

import sublime
import sublime_plugin

from . import core
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
}
_KIND_CONSTANT = (sublime.KIND_ID_VARIABLE, "k", "Constant")
_KIND_FALLBACK = (sublime.KIND_ID_AMBIGUOUS, "•", "Symbol")
# NOTE: only the start of a very large buffer is scanned for imports and
# assignments. Upgrade path: a background parse of the whole buffer.
_SOURCE_CAP = 400_000


def plugin_loaded():
    sublime.set_timeout_async(lambda: ensure_indexes(False), 0)


def _status(message):
    sublime.set_timeout(lambda: sublime.status_message(message), 0)


def _db_path():
    return os.path.join(sublime.cache_path(), "Intellekt", "index.sqlite")


def _settings():
    return sublime.load_settings(_SETTINGS)


def _detected():
    settings = _settings()
    sources = settings.get("java_sources") or []
    if not isinstance(sources, list):
        sources = []
    return core.detect_all(
        python_executable=settings.get("python_executable") or "",
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


def ensure_indexes(force):
    if not _LOCK.acquire(blocking=False):
        _status("Intellekt: index already running")
        return
    try:
        detected = _detected()
        db_path = _db_path()
        for lang in core.LANGS:
            info = detected.get(lang)
            if not info:
                continue
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
    if kind == "variable" and len(name) > 1 and name.isupper():
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


def _member_completions(view, point, prefix, lang):
    start = point - len(prefix)
    before = view.substr(sublime.Region(max(0, start - 300), start))
    left, _op = core.member_op(before)
    if left is None:
        return None
    receiver = core.receiver_from(left)
    if not receiver:
        return None
    db_path = _db_path()
    owner = core.resolve_owner(db_path, lang, _source(view), receiver)
    if not owner:
        return None
    rows = core.query_symbols(db_path, lang, owner, prefix)
    if not rows:
        return None
    return sublime.CompletionList(_items_from_symbols(rows), _MEMBER_FLAGS)


def _markup_completions(view, point, lang):
    if lang not in ("css", "html"):
        return None
    line = view.substr(sublime.Region(view.line(point).a, point))
    db_path = _db_path()
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


def _import_completions(view, point, lang):
    line = view.substr(sublime.Region(view.line(point).a, point))
    typed = core.import_typed(lang, line)
    if typed is None:
        return None
    sep = core.SEP[lang]
    db_path = _db_path()
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


def _bare_completions(view, prefix, lang):
    if not prefix:
        return None
    rows = core.bare_names(_db_path(), lang, _source(view), prefix)
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


class IntellektListener(sublime_plugin.EventListener):
    def on_activated(self, view):
        self._ensure_triggers(view)

    def on_load(self, view):
        self._ensure_triggers(view)

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
        markup = _markup_completions(view, point, lang)
        if markup:
            return markup
        imported = _import_completions(view, point, lang)
        if imported:
            return imported
        members = _member_completions(view, point, prefix, lang)
        if members:
            return members
        return _bare_completions(view, prefix, lang)

    def on_hover(self, view, point, zone):
        if zone != sublime.HOVER_TEXT:
            return
        if not view.match_selector(point, _SCOPE):
            return
        lang = _lang(view, point)
        if not lang:
            return
        word_region = view.word(point)
        word = view.substr(word_region)
        if not word or not (word[0].isalpha() or word[0] == "_"):
            return
        before = view.substr(sublime.Region(max(0, word_region.a - 300), word_region.a))
        db_path = _db_path()
        source = _source(view)
        title = ""
        doc = ""
        stripped = before.rstrip()
        left, op = core.member_op(stripped)
        if left is not None:
            receiver = core.receiver_from(left)
            owner = core.resolve_owner(db_path, lang, source, receiver) if receiver else None
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
        if not doc:
            return
        view.show_popup(
            '<body style="font-size: 0.85em"><b>%s</b><br><br>%s</body>'
            % (_escape(title), core.markdown_html(doc)),
            flags=sublime.HIDE_ON_MOUSE_MOVE_AWAY,
            location=point,
            max_width=480,
            max_height=320,
            on_navigate=_open_link,
        )


class IntellektRebuildIndexCommand(sublime_plugin.ApplicationCommand):
    def run(self):
        sublime.set_timeout_async(lambda: ensure_indexes(True), 0)


class IntellektIndexStatusCommand(sublime_plugin.WindowCommand):
    def run(self):
        text = core.status_text(_db_path(), _detected())
        sublime.message_dialog(text)
