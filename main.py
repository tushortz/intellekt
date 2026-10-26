"""Offline completions for Python, Java, and Ruby.

On load, Intellekt indexes the interpreters already installed on this
machine into a SQLite cache. Completions and hover text are read from that
cache. Nothing is downloaded and no third-party Python package is imported.
"""

import os
import re
import threading

import sublime
import sublime_plugin

from . import core
from .indexers import java_src

_LOCK = threading.Lock()
_SETTINGS = "Intellekt.sublime-settings"
_SCOPE = "source.python, source.java, source.ruby"
_TRIGGERS = (
    {"selector": "source.python", "characters": "."},
    {"selector": "source.java", "characters": "."},
    {"selector": "source.ruby", "characters": "."},
)
_MEMBER_FLAGS = sublime.INHIBIT_WORD_COMPLETIONS | sublime.DYNAMIC_COMPLETIONS
_BARE_FLAGS = sublime.DYNAMIC_COMPLETIONS
_KINDS = {
    "function": sublime.KIND_FUNCTION,
    "type": sublime.KIND_TYPE,
    "variable": sublime.KIND_VARIABLE,
    "namespace": sublime.KIND_NAMESPACE,
}
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
    )


def _produce(lang, info):
    if lang == "java":
        for path in info["sources"]:
            yield from java_src.scan_zip(path)
        return
    script = "python_index.py" if lang == "python" else "ruby_index.rb"
    yield from core.run_indexer(info["executable"], script)


def ensure_indexes(force):
    if not _LOCK.acquire(blocking=False):
        _status("Intellekt: index already running")
        return
    try:
        detected = _detected()
        db_path = _db_path()
        for lang in ("python", "java", "ruby"):
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
    if view.match_selector(point, "source.python"):
        return "python"
    if view.match_selector(point, "source.java"):
        return "java"
    if view.match_selector(point, "source.ruby"):
        return "ruby"
    return None


def _source(view):
    size = min(view.size(), _SOURCE_CAP)
    return view.substr(sublime.Region(0, size))


def _details(doc):
    text = (doc or "").strip()
    if not text:
        return ""
    return _escape(text[:160])


def _escape(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


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
            kind=_KINDS.get(kind, sublime.KIND_AMBIGUOUS),
            details=_details(doc),
        ))
    return items


def _member_completions(view, point, prefix, lang):
    start = point - len(prefix)
    before = view.substr(sublime.Region(max(0, start - 300), start))
    if not before.rstrip().endswith("."):
        return None
    receiver = core.receiver_from(before.rstrip()[:-1])
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


def _import_completions(view, point, lang):
    line = view.substr(sublime.Region(view.line(point).a, point))
    typed = core.import_typed(lang, line)
    if not typed:
        return None
    rows = core.module_names(_db_path(), lang)
    segments = core.import_segments(rows, typed, core.SEP[lang])
    if not segments:
        return None
    items = []
    for segment in segments[:80]:
        items.append(sublime.CompletionItem(
            segment["trigger"],
            annotation=segment["annotation"],
            completion=segment["trigger"],
            kind=_KINDS.get(segment["kind"], sublime.KIND_NAMESPACE),
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
            kind=_KINDS.get(kind, sublime.KIND_TYPE),
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
        if stripped.endswith(".") or stripped.endswith("::"):
            cut = 2 if stripped.endswith("::") else 1
            receiver = core.receiver_from(stripped[:-cut])
            owner = core.resolve_owner(db_path, lang, source, receiver) if receiver else None
            if owner:
                sep = "::" if stripped.endswith("::") else "."
                doc = core.symbol_doc(db_path, lang, owner, word) or core.module_doc(
                    db_path, lang, owner + sep + word
                )
                title = owner + sep + word
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
        body = _escape(doc).replace("\n", "<br>")
        view.show_popup(
            "<b>%s</b><br><br>%s" % (_escape(title), body),
            flags=sublime.HIDE_ON_MOUSE_MOVE_AWAY,
            location=point,
            max_width=480,
        )


class IntellektRebuildIndexCommand(sublime_plugin.ApplicationCommand):
    def run(self):
        sublime.set_timeout_async(lambda: ensure_indexes(True), 0)


class IntellektIndexStatusCommand(sublime_plugin.WindowCommand):
    def run(self):
        text = core.status_text(_db_path(), _detected())
        sublime.message_dialog(text)
