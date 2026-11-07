"""A small Language Server Protocol client. Standard library only.

Nothing here imports ``sublime``, so ``tests/check.py`` can drive a real
server. One process serves one (window, root, command). Documents are opened
and synced lazily, just before a request, with their full text.

NOTE: full-text sync resends the whole buffer when it changed since the last
request. Ceiling: multi-megabyte files cost one copy per request. Upgrade
path: incremental sync from ``on_text_changed``.
NOTE: ``completionItem/resolve`` is not sent, so servers that only attach
docs on resolve show none in the panel. Hover still shows them.
"""

import glob
import json
import os
import pathlib
import shutil
import subprocess
import threading

# First command found on PATH wins. A language missing here uses the index.
SERVERS = {
    "python": (
        ["pyright-langserver", "--stdio"], ["basedpyright-langserver", "--stdio"],
        ["pylsp"], ["jedi-language-server"],
    ),
    "javascript": (["typescript-language-server", "--stdio"],),
    "typescript": (["typescript-language-server", "--stdio"],),
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
    2: "function", 3: "function", 4: "function",
    5: "variable", 6: "variable", 10: "variable", 12: "variable", 20: "variable",
    23: "variable", 21: "constant",
    7: "type", 8: "type", 13: "type", 22: "type", 25: "type",
    9: "namespace", 17: "namespace", 19: "namespace",
    14: "keyword", 15: "snippet",
}

_verified = {}
_login = []


def login_path():
    """PATH from the user's login shell, read once.

    Sublime started from the Dock gets ``/usr/bin:/bin:/usr/sbin:/sbin``,
    which misses Homebrew, nvm, Go, and Cargo. ``-i`` is needed because nvm
    and most PATH edits live in ``.zshrc`` / ``.bashrc``.
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


def search_path(extra=()):
    """PATH plus the login shell's PATH and the usual tool folders."""
    home = os.path.expanduser("~")
    nvm = sorted(glob.glob(os.path.join(home, ".nvm", "versions", "node", "*", "bin")))
    dirs = list(extra) + login_path().split(os.pathsep) + os.environ.get("PATH", "").split(os.pathsep) + [
        os.path.join(home, ".local", "bin"),
        os.path.join(os.environ.get("GOPATH") or os.path.join(home, "go"), "bin"),
        os.path.join(home, ".cargo", "bin"),
        os.path.join(home, ".dotnet", "tools"),
        os.path.join(home, ".volta", "bin"),
        os.path.join(os.environ.get("APPDATA", ""), "npm") if os.name == "nt" else "",
        "/opt/homebrew/bin", "/usr/local/bin", "/usr/local/go/bin",
    ] + nvm[-1:]
    seen = []
    for d in dirs:
        if d and d not in seen:
            seen.append(d)
    return os.pathsep.join(seen)


def _works(exe):
    """A rustup proxy is on PATH even when the component is not installed."""
    if os.path.basename(exe).split(".")[0] != "rust-analyzer":
        return True
    if exe not in _verified:
        try:
            _verified[exe] = subprocess.run(
                [exe, "--version"], capture_output=True, timeout=10,
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _verified[exe] = False
    return _verified[exe]


def find_command(lang, override=None, path=None):
    """Resolved argv for ``lang``'s server, or None.

    ``override`` is a user setting: a command list, or ``False`` to disable.
    """
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


def file_uri(path):
    return pathlib.Path(os.path.abspath(path)).as_uri()


def language_id(lang, path):
    return LANGUAGE_IDS.get(os.path.splitext(path)[1].lower(), lang)


def _markup(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "language" in value:
            return "```%s\n%s\n```" % (value["language"], value.get("value", ""))
        return value.get("value", "")
    if isinstance(value, list):
        return "\n\n".join(filter(None, (_markup(item) for item in value)))
    return ""


def hover_text(result):
    """Markdown from a ``textDocument/hover`` result, or ""."""
    if not isinstance(result, dict):
        return ""
    return _markup(result.get("contents")).strip()


def completion_rows(result):
    """``(rows, incomplete)`` from a ``textDocument/completion`` result.

    Each row is ``(trigger, annotation, text, is_snippet, kind, doc)``.
    NOTE: an edit's range is ignored. Sublime replaces the word before the
    caret, which matches what servers send in nearly every case.
    """
    if isinstance(result, dict):
        items, incomplete = result.get("items") or [], bool(result.get("isIncomplete"))
    else:
        items, incomplete = result or [], False
    rows = []
    for item in items:
        label = (item.get("label") or "").strip()
        if not label:
            continue
        edit = item.get("textEdit") or {}
        text = edit.get("newText") or item.get("insertText") or label
        rows.append((
            label,
            (item.get("detail") or "").strip()[:60],
            text,
            item.get("insertTextFormat") == 2,
            _KINDS.get(item.get("kind"), ""),
            _markup(item.get("documentation")),
        ))
    return rows, incomplete


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


class Client:
    """One running server. Messages sent before ``initialize`` returns are queued."""

    def __init__(self, cmd, root, env=None, settings=None, init_options=None):
        self.cmd = cmd
        self.root = root
        self.env = env
        self.settings = settings or {}
        self.init_options = init_options or {}
        self.capabilities = {}
        self.dead = False
        self.docs = {}
        self._ready = False
        self._queue = []
        self._pending = {}
        self._next_id = 0
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._proc = None

    def start(self):
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        try:
            # NOTE: stderr is discarded. A server that dies at start shows up
            # as "not running" in Index Status, not with its error text.
            self._proc = subprocess.Popen(
                self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=self.root, env=self.env,
                creationflags=flags,
            )
        except OSError as err:
            print("Intellekt: cannot start %s: %s" % (self.cmd[0], err))
            self.dead = True
            return
        threading.Thread(target=self._read, daemon=True).start()
        root_uri = file_uri(self.root)
        self._write(self._make_request("initialize", {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "rootPath": self.root,
            "workspaceFolders": [{"uri": root_uri, "name": os.path.basename(self.root)}],
            "initializationOptions": self.init_options,
            "capabilities": {
                "general": {"positionEncodings": ["utf-16"]},
                "workspace": {"configuration": True, "workspaceFolders": True},
                "textDocument": {
                    "synchronization": {"didSave": True},
                    "completion": {
                        "completionItem": {
                            "snippetSupport": True,
                            "documentationFormat": ["markdown", "plaintext"],
                        },
                        "contextSupport": True,
                    },
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                },
            },
        }, self._on_initialized))

    def _on_initialized(self, result, error):
        if error or not isinstance(result, dict):
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

    def _make_request(self, method, params, callback):
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            self._pending[rid] = callback
        return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}

    def _send(self, message):
        with self._lock:
            if not self._ready:
                self._queue.append(message)
                return
        self._write(message)

    def _write(self, message):
        if self.dead or not self._proc:
            return
        body = json.dumps(message).encode("utf-8")
        try:
            with self._write_lock:
                self._proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
                self._proc.stdin.flush()
        except (OSError, ValueError):
            self._die()

    def request(self, method, params, callback):
        """Send a request. ``callback(result, error)`` runs on the reader thread."""
        if self.dead:
            callback(None, {"message": "server not running"})
            return None
        message = self._make_request(method, params, callback)
        self._send(message)
        return message["id"]

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def cancel(self, rid):
        with self._lock:
            pending = self._pending.pop(rid, None)
        if pending is not None:
            self.notify("$/cancelRequest", {"id": rid})

    def sync(self, path, lang, text, version):
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

    def close(self, path):
        uri = file_uri(path)
        if self.docs.pop(uri, None) is not None:
            self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

    def saved(self, path):
        uri = file_uri(path)
        if uri in self.docs:
            self.notify("textDocument/didSave", {"textDocument": {"uri": uri}})

    def stop(self):
        if self.dead:
            return
        proc = self._proc

        def kill():
            if proc and proc.poll() is None:
                proc.kill()

        if self._ready:
            # NOTE: exit is sent without waiting for the shutdown reply. A
            # server still running after 2 seconds is killed.
            self._write(self._make_request("shutdown", None, lambda *_: None))
            self._write({"jsonrpc": "2.0", "method": "exit"})
        threading.Timer(2, kill).start()
        self._die()

    def _die(self):
        with self._lock:
            if self.dead:
                return
            self.dead = True
            pending, self._pending = self._pending, {}
        for callback in pending.values():
            self._call(callback, None, {"message": "server exited"})

    @staticmethod
    def _call(callback, result, error):
        try:
            callback(result, error)
        except Exception as err:
            print("Intellekt LSP callback: %r" % err)

    def _read(self):
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

    def _dispatch(self, message):
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

    def _answer(self, method, params):
        """Replies to requests the server sends us."""
        if method == "workspace/configuration":
            return [self._section(item.get("section")) for item in params.get("items", [])]
        if method == "workspace/workspaceFolders":
            return [{"uri": file_uri(self.root), "name": os.path.basename(self.root)}]
        return None

    def _section(self, section):
        value = self.settings
        for part in section.split(".") if section else ():
            value = value.get(part) if isinstance(value, dict) else None
        return value
