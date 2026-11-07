"""A small stdlib-only Language Server Protocol client (no ``sublime`` import).

One process per (window, root, command); documents sync lazily with full text.

NOTE: full-text sync resends the whole buffer when it changed since the last
request. Ceiling: multi-megabyte files cost one copy per request. Upgrade
path: incremental sync from ``on_text_changed``.
NOTE: ``completionItem/resolve`` is not sent, so servers that only attach
docs on resolve show none in the panel. Hover still shows them.
"""

from __future__ import annotations

import glob
import json
import os
import re
import pathlib
import shutil
import subprocess
import threading
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable, Optional

# First command found on PATH wins. A language missing here uses the index.
SERVERS = {
    "python": (
        ["pyright-langserver", "--stdio"], ["basedpyright-langserver", "--stdio"],
        ["pylsp"], ["jedi-language-server"],
    ),
    # TypeScript 7 (the native rewrite) has its own server and no tsserver.js,
    # which typescript-language-server needs. Older TypeScript uses that.
    "javascript": (["tsc", "--lsp", "--stdio"], ["typescript-language-server", "--stdio"]),
    "typescript": (["tsc", "--lsp", "--stdio"], ["typescript-language-server", "--stdio"]),
    "go": (["gopls"],),
    "rust": (["rust-analyzer"],),
    "cpp": (["clangd", "--header-insertion-decorators=0", "--completion-parse=always"],),
    "java": (["jdtls"],),
    "ruby": (["ruby-lsp"], ["solargraph", "stdio"]),
    "php": (["intelephense", "--stdio"], ["phpactor", "language-server"]),
    "csharp": (["csharp-ls"],),
    "css": (["vscode-css-language-server", "--stdio"],),
    "html": (["vscode-html-language-server", "--stdio"],),
    "perl": (["perlnavigator", "--stdio"],),
}

# Extensions whose LSP language id differs from the Intellekt language.
LANGUAGE_IDS = {
    ".tsx": "typescriptreact", ".jsx": "javascriptreact",
    ".c": "c", ".h": "c", ".m": "objective-c", ".mm": "objective-cpp",
}

# LSP CompletionItemKind number -> Intellekt kind name.
_KINDS = {
    2: "method", 3: "function", 4: "method",
    5: "field", 10: "property", 23: "property",
    6: "local", 12: "constant", 20: "constant", 21: "constant",
    7: "type", 22: "type", 25: "type", 8: "interface", 13: "enum",
    9: "namespace", 17: "namespace", 19: "namespace",
    14: "keyword", 15: "snippet",
}

_verified = {}
_login = []


def login_path() -> str:
    """PATH from the user's interactive login shell, read once.

    A Dock-launched Sublime misses Homebrew, nvm, Go, and Cargo; ``-i`` loads ``.zshrc``.
    """
    if not _login:
        found = ""
        if os.name != "nt":
            shell = os.environ.get("SHELL") or "/bin/sh"
            try:
                out = subprocess.run(
                    [shell, "-l", "-i", "-c", 'printf "__PATH__%s__PATH__" "$PATH"'],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
                ).stdout
                parts = out.split("__PATH__")
                if len(parts) >= 3:
                    found = parts[1]
            except (OSError, subprocess.TimeoutExpired):
                pass
        _login.append(found)
    return _login[0]


_gem_dirs = []


def _gem_bins(path: str) -> list[str]:
    """Folders where ``gem install`` puts commands (asks Ruby once; often off PATH)."""
    if not _gem_dirs:
        found = []
        ruby = shutil.which("ruby", path=path)
        if ruby:
            flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
            try:
                out = subprocess.run(
                    [ruby, "-e", 'puts Gem.bindir; puts File.join(Gem.user_dir, "bin")'],
                    capture_output=True, text=True, timeout=10, creationflags=flags,
                ).stdout
                found = [line.strip() for line in out.splitlines() if line.strip()]
            except (OSError, subprocess.TimeoutExpired):
                pass
        _gem_dirs.append(found)
    return _gem_dirs[0]


def _globbed(*patterns: str) -> list[str]:
    found = []
    for pattern in patterns:
        found.extend(sorted(glob.glob(os.path.expandvars(os.path.expanduser(pattern))), reverse=True))
    return found


def search_path(extra: Iterable[str] = ()) -> str:
    """PATH plus the login shell's PATH and the usual tool folders (gem, pip --user, ...)."""
    home = os.path.expanduser("~")
    nvm = sorted(glob.glob(os.path.join(home, ".nvm", "versions", "node", "*", "bin")))
    base = list(extra) + login_path().split(os.pathsep) + os.environ.get("PATH", "").split(os.pathsep)
    dirs = base + _gem_bins(os.pathsep.join(base)) + [
        os.path.join(home, ".rbenv", "shims"),
        os.path.join(home, ".asdf", "shims"),
        os.path.join(home, ".local", "bin"),
        os.path.join(os.environ.get("GOPATH") or os.path.join(home, "go"), "bin"),
        os.path.join(home, ".cargo", "bin"),
        os.path.join(home, ".dotnet", "tools"),
        os.path.join(home, ".volta", "bin"),
        os.path.join(os.environ.get("APPDATA", ""), "npm") if os.name == "nt" else "",
        "/opt/homebrew/bin", "/usr/local/bin", "/usr/local/go/bin",
    ] + nvm[-1:] + _globbed(
        "~/Library/Python/*/bin",                     # macOS pip install --user
        "%APPDATA%/Python/Python*/Scripts",           # Windows pip install --user
        "%LOCALAPPDATA%/Programs/Python/Python*/Scripts",
    )
    seen = []
    for d in dirs:
        if d and d not in seen:
            seen.append(d)
    return os.pathsep.join(seen)


def _works(exe: str) -> bool:
    """Checks for commands that can be on PATH without being usable: a rustup
    proxy without the component, and ``tsc`` older than TypeScript 7 (no --lsp)."""
    name = os.path.basename(exe).split(".")[0]
    if name not in ("rust-analyzer", "tsc"):
        return True
    if exe not in _verified:
        try:
            flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
            proc = subprocess.run(
                [exe, "--version"], capture_output=True, text=True, timeout=10, creationflags=flags,
            )
            ok = proc.returncode == 0
            if ok and name == "tsc":
                found = re.search(r"(\d+)\.", proc.stdout or "")
                ok = bool(found) and int(found.group(1)) >= 7
            _verified[exe] = ok
        except (OSError, subprocess.TimeoutExpired):
            _verified[exe] = False
    return _verified[exe]


def tsserver_path(path: Optional[str] = None) -> str:
    """The global TypeScript's ``tsserver.js`` (TypeScript 6 and older), or ""."""
    npm = shutil.which("npm", path=path)
    if not npm:
        return ""
    try:
        root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=15,
                              creationflags=0x08000000 if os.name == "nt" else 0).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
    found = os.path.join(root, "typescript", "lib", "tsserver.js")
    return found if os.path.isfile(found) else ""


def find_command(lang: str, override: Any = None, path: Optional[str] = None) -> Optional[list[str]]:
    """Resolved argv for ``lang``'s server, or None. ``override``: a command list, or ``False``."""
    if override is False:
        return None
    candidates = [override] if override else SERVERS.get(lang, ())
    for cmd in candidates:
        if not cmd:
            continue
        exe = shutil.which(cmd[0], path=path)
        if exe and _works(exe):
            return [exe] + list(cmd[1:])
    return None


def file_uri(path: str) -> str:
    return pathlib.Path(os.path.abspath(path)).as_uri()


def language_id(lang: str, path: str) -> str:
    return LANGUAGE_IDS.get(os.path.splitext(path)[1].lower(), lang)


def _markup(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "language" in value:
            return "```%s\n%s\n```" % (value["language"], value.get("value", ""))
        return value.get("value", "")
    if isinstance(value, list):
        return "\n\n".join(filter(None, (_markup(item) for item in value)))
    return ""


def hover_text(result: Any) -> str:
    """Markdown from a ``textDocument/hover`` result, or ""."""
    if not isinstance(result, dict):
        return ""
    return _markup(result.get("contents")).strip()


def completion_items(result: Any, by_case: bool = True) -> tuple[list[dict[str, Any]], bool]:
    """``(items, incomplete)``: raw items, public names first, then ``sortText`` order."""
    if isinstance(result, dict):
        items, incomplete = result.get("items") or [], bool(result.get("isIncomplete"))
    else:
        items, incomplete = result or [], False
    items = [item for item in items if (item.get("label") or "").strip()]
    items.sort(key=lambda item: (
        name_rank((item.get("filterText") or item.get("label") or "").strip(), by_case),
        item.get("sortText") or item.get("label") or "",
    ))
    return items, incomplete


def completion_row(item: dict[str, Any]) -> tuple[Any, ...]:
    """``(trigger, annotation, text, is_snippet, kind, doc, extra_edits, start)``.

    ``extra_edits``: e.g. an auto-import. ``start``: ``(line, utf16_char)`` where the
    edit begins, or None (may precede the word: rust-analyzer ``visa.ref`` -> ``&visa``).
    """
    label = (item.get("label") or "").strip()
    edit = item.get("textEdit") or {}
    details = item.get("labelDetails") or {}
    rng = edit.get("range") or edit.get("insert") or {}
    start = rng.get("start")
    return (
        label,
        (details.get("description") or item.get("detail") or "").strip()[:60],
        edit.get("newText") or item.get("insertText") or label,
        item.get("insertTextFormat") == 2,
        _KINDS.get(item.get("kind"), ""),
        _markup(item.get("documentation")),
        item.get("additionalTextEdits") or [],
        (start["line"], start["character"]) if start else None,
    )


def completion_rows(result: Any) -> tuple[list[tuple[Any, ...]], bool]:
    """``(rows, incomplete)`` from a ``textDocument/completion`` result."""
    items, incomplete = completion_items(result)
    return [completion_row(item) for item in items], incomplete


def signature_markdown(result: Any) -> str:
    """Markdown for a ``signatureHelp`` result (active parameter bold, escaped), or ""."""
    if not isinstance(result, dict) or not result.get("signatures"):
        return ""
    signatures = result["signatures"]
    index = result.get("activeSignature") or 0
    sig = signatures[index if 0 <= index < len(signatures) else 0]
    label = sig.get("label") or ""
    params = sig.get("parameters") or []
    active = sig.get("activeParameter", result.get("activeParameter"))
    start = end = 0
    if active is not None and 0 <= active < len(params):
        where = params[active].get("label")
        if isinstance(where, list) and len(where) == 2:
            start, end = where
        elif isinstance(where, str) and where in label:
            start = label.index(where)
            end = start + len(where)

    def esc(text: str) -> str:
        return re.sub(r"([\\`*_\[\]<>#])", r"\\\1", text)

    if end > start:
        head = esc(label[:start]) + "**" + esc(label[start:end]) + "**" + esc(label[end:])
    else:
        head = esc(label)
    parts = [head]
    if end > start:
        doc = _markup(params[active].get("documentation")).strip()
        if doc:
            parts.append(doc)
    doc = _markup(sig.get("documentation")).strip()
    if doc:
        parts.append(doc)
    return "\n\n".join(parts)


def uri_path(uri: Optional[str]) -> str:
    """Local path for a ``file:`` URI; a Windows drive letter is upper-cased to match Sublime."""
    parsed = urllib.parse.urlparse(uri or "")
    path = urllib.request.url2pathname(urllib.parse.unquote(parsed.path))
    if os.name == "nt" and re.match(r"[a-z]:", path):
        path = path[0].upper() + path[1:]
    return path


def same_path(a: str, b: str) -> bool:
    """True when two paths name the same file (case-insensitive on Windows)."""
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _offset(lines: list[str], line: int, char: int) -> int:
    """Character offset in ``lines`` (from ``splitlines(True)``) for an LSP position."""
    if line >= len(lines):
        return sum(len(text) for text in lines)
    before = sum(len(text) for text in lines[:line])
    return before + utf16_col(lines[line].rstrip("\r\n"), char)


def apply_text_edits(text: str, edits: list[dict[str, Any]]) -> str:
    """``text`` with LSP ``TextEdit``s applied (for files that are not open)."""
    lines = text.splitlines(True)
    spans = []
    for edit in edits:
        rng = edit["range"]
        a = _offset(lines, rng["start"]["line"], rng["start"]["character"])
        b = _offset(lines, rng["end"]["line"], rng["end"]["character"])
        spans.append((a, b, edit.get("newText") or ""))
    for a, b, new in sorted(spans, key=lambda span: span[:2], reverse=True):
        text = text[:a] + new + text[b:]
    return text


def workspace_edit_files(workspace_edit: Optional[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """``{path: [TextEdit]}`` from a ``WorkspaceEdit``.

    NOTE: file create, rename, and delete operations are skipped.
    """
    files = {}
    for uri, edits in ((workspace_edit or {}).get("changes") or {}).items():
        files.setdefault(uri_path(uri), []).extend(edits)
    for change in (workspace_edit or {}).get("documentChanges") or ():
        if "textDocument" in change:
            files.setdefault(uri_path(change["textDocument"]["uri"]), []).extend(change.get("edits") or [])
    return files


def locations(result: Any) -> list[tuple[str, int, int]]:
    """``[(path, line, utf16_char)]`` from a Location, Location[] or LocationLink[]."""
    if isinstance(result, dict):
        result = [result]
    found = []
    for loc in result or ():
        uri = loc.get("uri") or loc.get("targetUri")
        rng = loc.get("range") or loc.get("targetSelectionRange") or loc.get("targetRange") or {}
        start = rng.get("start") or {}
        if uri:
            found.append((uri_path(uri), start.get("line", 0), start.get("character", 0)))
    return found


# LSP SymbolKind number -> Intellekt kind name. These differ from completion kinds.
_SYMBOL_KINDS = {
    1: "namespace", 2: "namespace", 3: "namespace", 4: "namespace",
    5: "type", 6: "method", 7: "property", 8: "field", 9: "method",
    10: "enum", 11: "interface", 12: "function", 13: "local", 14: "constant",
    22: "constant", 23: "type", 24: "property", 26: "type",
}


def symbol_rows(result: Any, path: Optional[str] = None) -> list[tuple[Any, ...]]:
    """``[(name, kind, path, line, utf16_char, depth, detail)]``, nested symbols flattened."""
    rows = []

    def walk(symbols: Optional[list[dict[str, Any]]], depth: int) -> None:
        for sym in symbols or ():
            kind = _SYMBOL_KINDS.get(sym.get("kind"), "")
            if "location" in sym:
                where = locations(sym["location"])
                if where:
                    rows.append((sym.get("name", ""), kind) + where[0] + (depth, sym.get("containerName") or ""))
                continue
            start = (sym.get("selectionRange") or sym.get("range") or {}).get("start") or {}
            rows.append((sym.get("name", ""), kind, path, start.get("line", 0),
                         start.get("character", 0), depth, sym.get("detail") or ""))
            walk(sym.get("children"), depth + 1)

    walk(result, 0)
    return rows


SEMANTIC_TYPES = [
    "namespace", "type", "class", "enum", "interface", "struct", "typeParameter",
    "parameter", "variable", "property", "enumMember", "event", "function", "method",
    "macro", "keyword", "modifier", "comment", "string", "number", "regexp", "operator",
    "decorator",
]
SEMANTIC_MODIFIERS = [
    "declaration", "definition", "readonly", "static", "deprecated", "abstract",
    "async", "modification", "documentation", "defaultLibrary",
]


def semantic_tokens(data: Optional[list[int]], legend: dict[str, Any]) -> list[tuple[int, int, int, str, list[str]]]:
    """``[(line, utf16_char, length, type, [modifiers])]`` from ``semanticTokens/full`` data."""
    types = legend.get("tokenTypes") or []
    modifiers = legend.get("tokenModifiers") or []
    tokens, line, char = [], 0, 0
    for i in range(0, len(data or ()) - 4, 5):
        delta_line, delta_char, length, kind, bits = data[i:i + 5]
        line += delta_line
        char = char + delta_char if delta_line == 0 else delta_char
        names = [name for bit, name in enumerate(modifiers) if bits & (1 << bit)]
        tokens.append((line, char, length, types[kind] if kind < len(types) else "", names))
    return tokens


def inlay_label(hint: dict[str, Any]) -> str:
    """Text of an inlay hint, with the padding the server asked for."""
    label = hint.get("label")
    if isinstance(label, list):
        label = "".join(part.get("value", "") for part in label)
    text = (label or "").strip()
    return (" " if hint.get("paddingLeft") else "") + text + (" " if hint.get("paddingRight") else "")


def selection_chain(item: Any) -> list[dict[str, Any]]:
    """Ranges of a ``SelectionRange``, smallest first."""
    chain = []
    while isinstance(item, dict):
        chain.append(item.get("range") or {})
        item = item.get("parent")
    return chain


def lens_command(lens: dict[str, Any]) -> Optional[tuple[str, str, list[Any]]]:
    """``(title, command, arguments)`` of a resolved code lens, or None."""
    command = lens.get("command") or {}
    title = (command.get("title") or "").strip()
    if not title:
        return None
    return title, command.get("command") or "", command.get("arguments") or []


def name_rank(name: str, by_case: bool = True) -> tuple[int, bool]:
    """Sort key: public, ``_private``, ``__dunder__``; then lowercase first when ``by_case``."""
    underscore = 2 if name.startswith("__") else 1 if name.startswith("_") else 0
    return underscore, by_case and name.lstrip("_")[:1].isupper()


def case_first(lang: str) -> bool:
    """Lowercase-first ordering, except Java (capitalized names expected in A-Z order)."""
    return lang != "java"


def diagnostic_rows(diagnostics: Optional[list[dict[str, Any]]]) -> list[tuple[Any, ...]]:
    """``(start, end, severity, message, raw)`` per diagnostic; positions ``(line, utf16_char)``.

    Severity 1 error .. 4 hint. ``raw`` is sent back with quick-fix requests.
    """
    rows = []
    for diag in diagnostics or ():
        rng = diag.get("range") or {}
        start, end = rng.get("start") or {}, rng.get("end") or {}
        source = diag.get("source") or ""
        message = (diag.get("message") or "").strip()
        rows.append((
            (start.get("line", 0), start.get("character", 0)),
            (end.get("line", 0), end.get("character", 0)),
            diag.get("severity") or 1,
            "%s (%s)" % (message, source) if source else message,
            diag,
        ))
    return rows


def utf16_col(text: str, units: int) -> int:
    """Code-point index of ``units`` UTF-16 code units into ``text``."""
    count = 0
    for index, char in enumerate(text):
        if count >= units:
            return index
        count += 2 if ord(char) > 0xFFFF else 1
    return len(text)


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class Client:
    """One running server. Messages sent before ``initialize`` returns are queued."""

    def __init__(
        self, cmd: list[str], root: str, env: Optional[dict[str, str]] = None,
        settings: Optional[dict[str, Any]] = None, init_options: Optional[dict[str, Any]] = None,
    ) -> None:
        self.cmd = cmd
        self.root = root
        self.env = env
        self.settings = settings or {}
        self.init_options = init_options or {}
        self.capabilities = {}
        self.dead = False
        self.docs = {}
        # Called as on_diagnostics(uri, diagnostics) from the reader thread.
        self.on_diagnostics = None
        # Called as on_apply_edit(workspace_edit, None) when the server asks
        # us to change files, for example after "organize imports".
        self.on_apply_edit = None
        # Called as on_notify(method, params) for other server notifications
        # (clangd's inactive regions) and for "workspace/*/refresh" requests.
        self.on_notify = None
        self._ready = False
        self._queue = []
        self._pending = {}
        self._next_id = 0
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._proc = None
        self._stopping = False
        self.stderr_tail = b""

    def start(self) -> None:
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        try:
            self._proc = subprocess.Popen(
                self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=self.root, env=self.env,
                creationflags=flags,
            )
        except OSError as err:
            print("Intellekt: cannot start %s: %s" % (self.cmd[0], err))
            self.dead = True
            return
        threading.Thread(target=self._read, daemon=True).start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()
        root_uri = file_uri(self.root)
        self._write(self._make_request("initialize", {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "rootPath": self.root,
            "workspaceFolders": [{"uri": root_uri, "name": os.path.basename(self.root)}],
            "initializationOptions": self.init_options,
            "capabilities": {
                "general": {"positionEncodings": ["utf-16"]},
                # rust-analyzer only sends code lenses for editor commands
                # the client says it can run; main.py runs these.
                "experimental": {"commands": {"commands": [
                    "rust-analyzer.runSingle", "rust-analyzer.showReferences",
                    "rust-analyzer.gotoLocation",
                ]}},
                "workspace": {
                    "configuration": True, "workspaceFolders": True,
                    "applyEdit": True, "workspaceEdit": {"documentChanges": True},
                    "symbol": {}, "executeCommand": {},
                },
                "textDocument": {
                    "synchronization": {"didSave": True},
                    "completion": {
                        "completionItem": {
                            "snippetSupport": True,
                            "documentationFormat": ["markdown", "plaintext"],
                            "labelDetailsSupport": True,
                            "resolveSupport": {"properties": ["documentation", "detail"]},
                        },
                        "contextSupport": True,
                    },
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "signatureHelp": {"signatureInformation": {
                        "documentationFormat": ["markdown", "plaintext"],
                        "parameterInformation": {"labelOffsetSupport": True},
                        "activeParameterSupport": True,
                    }},
                    "definition": {"linkSupport": True},
                    "typeDefinition": {"linkSupport": True},
                    "declaration": {"linkSupport": True},
                    "references": {},
                    "documentHighlight": {},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "rename": {"prepareSupport": False},
                    "codeAction": {"codeActionLiteralSupport": {"codeActionKind": {
                        "valueSet": ["quickfix", "refactor", "source", "source.organizeImports"],
                    }}},
                    "callHierarchy": {},
                    "typeHierarchy": {},
                    "implementation": {"linkSupport": True},
                    "formatting": {},
                    "rangeFormatting": {},
                    "onTypeFormatting": {},
                    "selectionRange": {},
                    "foldingRange": {"lineFoldingOnly": True},
                    "inlayHint": {},
                    "codeLens": {},
                    "documentLink": {"tooltipSupport": True},
                    "semanticTokens": {
                        "requests": {"full": True},
                        "tokenTypes": SEMANTIC_TYPES,
                        "tokenModifiers": SEMANTIC_MODIFIERS,
                        "formats": ["relative"],
                    },
                    # clangd: dims code disabled by #if / #ifdef.
                    "inactiveRegionsCapabilities": {"inactiveRegions": True},
                    "publishDiagnostics": {"relatedInformation": False, "tagSupport": {"valueSet": [1, 2]}},
                    "diagnostic": {"dynamicRegistration": False},
                },
            },
        }, self._on_initialized))

    def _on_initialized(self, result: Any, error: Optional[dict[str, Any]]) -> None:
        if error or not isinstance(result, dict):
            # When the process is gone, _die already said why, or we stopped
            # it ourselves (plugin reload, restart, window closed).
            if not self.dead:
                print("Intellekt: %s failed to initialize: %s" % (self.cmd[0], error))
                self.stop()
            return
        self.capabilities = result.get("capabilities") or {}
        self._write({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        if self.settings:
            self._write({
                "jsonrpc": "2.0", "method": "workspace/didChangeConfiguration",
                "params": {"settings": self.settings},
            })
        with self._lock:
            self._ready = True
            queued, self._queue = self._queue, []
        for message in queued:
            self._write(message)

    def _make_request(self, method: str, params: Any, callback: Callable[[Any, Any], None]) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            self._pending[rid] = callback
        return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}

    def _send(self, message: dict[str, Any]) -> None:
        with self._lock:
            if not self._ready:
                self._queue.append(message)
                return
        self._write(message)

    def _write(self, message: dict[str, Any]) -> None:
        if self.dead or not self._proc:
            return
        body = json.dumps(message).encode("utf-8")
        try:
            with self._write_lock:
                self._proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
                self._proc.stdin.flush()
        except (OSError, ValueError):
            self._die()

    def request(self, method: str, params: Any, callback: Callable[[Any, Any], None]) -> Optional[int]:
        """Send a request. ``callback(result, error)`` runs on the reader thread."""
        if self.dead:
            callback(None, {"message": "server not running"})
            return None
        message = self._make_request(method, params, callback)
        self._send(message)
        return message["id"]

    def notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def cancel(self, rid: int) -> None:
        with self._lock:
            pending = self._pending.pop(rid, None)
        if pending is not None:
            self.notify("$/cancelRequest", {"id": rid})

    def sync(self, path: str, lang: str, text: str, version: int) -> str:
        """Open the document, or send its text when ``version`` changed."""
        uri = file_uri(path)
        known = self.docs.get(uri)
        if known == version:
            return uri
        if known is None:
            self.notify("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": language_id(lang, path),
                "version": version, "text": text,
            }})
        else:
            self.notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            })
        self.docs[uri] = version
        return uri

    def close(self, path: str) -> None:
        uri = file_uri(path)
        if self.docs.pop(uri, None) is not None:
            self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

    def saved(self, path: str) -> None:
        uri = file_uri(path)
        if uri in self.docs:
            self.notify("textDocument/didSave", {"textDocument": {"uri": uri}})

    def _read_stderr(self) -> None:
        # Drained so a chatty server never blocks on a full pipe. The tail is
        # printed if the server dies on its own.
        for line in iter(self._proc.stderr.readline, b""):
            self.stderr_tail = (self.stderr_tail + line)[-2000:]

    def stop(self) -> None:
        if self.dead:
            return
        self._stopping = True
        proc = self._proc

        def kill() -> None:
            if proc and proc.poll() is None:
                proc.kill()

        if self._ready:
            # NOTE: exit is sent without waiting for the shutdown reply. A
            # server still running after 2 seconds is killed.
            self._write(self._make_request("shutdown", None, lambda *_: None))
            self._write({"jsonrpc": "2.0", "method": "exit"})
        threading.Timer(2, kill).start()
        self._die()

    def _die(self) -> None:
        with self._lock:
            if self.dead:
                return
            self.dead = True
            pending, self._pending = self._pending, {}
        if not self._stopping:
            if self._proc:
                self._stderr_thread.join(1)
            code = self._proc.poll() if self._proc else None
            print("Intellekt: %s exited (code %s, root %s)\n%s" % (
                self.cmd[0], code, self.root,
                self.stderr_tail.decode("utf-8", "replace").strip(),
            ))
        for callback in pending.values():
            self._call(callback, None, {"message": "server exited"})

    @staticmethod
    def _call(callback: Callable[[Any, Any], None], result: Any, error: Any) -> None:
        try:
            callback(result, error)
        except Exception as err:
            print("Intellekt LSP callback: %r" % err)

    def _read(self) -> None:
        stream = self._proc.stdout
        try:
            while True:
                length = None
                while True:
                    line = stream.readline()
                    if not line:
                        return
                    line = line.strip()
                    if not line:
                        break
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"content-length":
                        length = int(value)
                if length is None:
                    continue
                body = stream.read(length)
                if len(body) < length:
                    return
                try:
                    message = json.loads(body.decode("utf-8"))
                except ValueError:
                    continue
                self._dispatch(message)
        except (OSError, ValueError):
            pass
        finally:
            self._die()

    def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method is None:
            with self._lock:
                callback = self._pending.pop(message.get("id"), None)
            if callback:
                self._call(callback, message.get("result"), message.get("error"))
            return
        if "id" in message:
            self._write({
                "jsonrpc": "2.0", "id": message["id"],
                "result": self._answer(method, message.get("params") or {}),
            })
        elif method == "textDocument/publishDiagnostics" and self.on_diagnostics:
            params = message.get("params") or {}
            self._call(self.on_diagnostics, params.get("uri"), params.get("diagnostics") or [])
        elif self.on_notify:
            self._call(self.on_notify, method, message.get("params") or {})

    def _answer(self, method: str, params: dict[str, Any]) -> Any:
        """Replies to requests the server sends us."""
        if method == "workspace/configuration":
            return [self._section(item.get("section")) for item in params.get("items", [])]
        if method == "workspace/workspaceFolders":
            return [{"uri": file_uri(self.root), "name": os.path.basename(self.root)}]
        if method.startswith("workspace/") and method.endswith("/refresh"):
            # inlayHint, codeLens, semanticTokens: the server wants a redraw.
            if self.on_notify:
                self._call(self.on_notify, method, params)
            return None
        if method == "workspace/applyEdit":
            if not self.on_apply_edit:
                return {"applied": False}
            # NOTE: replies "applied" before the edit runs on Sublime's main
            # thread. A failure there is printed, not reported to the server.
            self._call(self.on_apply_edit, params.get("edit") or {}, None)
            return {"applied": True}
        return None

    def supports(self, capability: str) -> bool:
        """True when the server announced ``capability`` (e.g. "renameProvider")."""
        return bool(self.capabilities.get(capability))

    def _section(self, section: Optional[str]) -> Any:
        value = self.settings
        for part in section.split(".") if section else ():
            value = value.get(part) if isinstance(value, dict) else None
        return value
