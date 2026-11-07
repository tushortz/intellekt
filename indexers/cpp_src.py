"""Scan C and C++ headers into symbol records. libc++/libstdc++ macros are
erased first, and file-scope C functions are owned by ``c``."""
# NOTE: a line scan, not a C++ parser. Unlisted macros, most templates, and
# headers past ``MAX_FILES`` are missed; ``ext``/``experimental`` are skipped;
# ``bits/`` is scanned but not offered as an include. Upgrade path: ``clang -ast-dump``.
# NOTE: framework directories reported by the compiler are not walked.

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Iterator, Optional

from . import merge_modules, read_text, walk_files

MAX_FILES = 5000
_SKIP = {
    "test", "tests", "testing", "testdata", "experimental", "ext",
    "debug", "tr1", "tr2", "backward", "node_modules", "private",
}
_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "sizeof", "else",
    "do", "case", "new", "delete", "throw", "defined", "class", "struct",
    "namespace", "template", "typedef", "using", "operator",
}
_FN_RE = re.compile(
    r"^(?:(?:virtual|static|inline|explicit|constexpr|friend)\s+)*"
    r"(?:[\w:<>,\*&]+\s+)*([A-Za-z_]\w*)\s*\((.*)\)\s*(?:const)?\s*"
    r"(?:noexcept\b[^;]*)?\s*;?\s*$"
)
_PROTO_RE = re.compile(r"([A-Za-z_]\w*)\s*\((.*)\)\s*;?\s*$")
_CLASS_RE = re.compile(r"\b(class|struct)\s+([A-Za-z_]\w*)\b")
_NS_RE = re.compile(r"(?:inline\s+)?namespace\s+([A-Za-z_]\w*)\b")


def _prepare(text: str) -> str:
    text = text.replace("_LIBCPP_BEGIN_NAMESPACE_STD", "namespace std {")
    text = text.replace("_LIBCPP_END_NAMESPACE_STD", "}")
    # libstdc++ opens "namespace std" itself. Its VERSION macro is an
    # optional inline namespace, erased below with the other macros.
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    text = re.sub(r"//.*?$", "", text, flags=re.M)
    text = re.sub(r"\[\[.*?\]\]", " ", text)
    text = re.sub(r"\b_(?:LIBCPP|GLIBCXX)_\w+\b", " ", text)
    text = re.sub(r"\b_[A-Z][A-Z0-9_]{2,}\b", " ", text)
    return text


def _snippet(name: str, args: Optional[str]) -> str:
    labels = []
    depth = 0
    buf = []
    parts = []
    for char in args or "":
        if char in "<(":
            depth += 1
        elif char in ">)":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(char)
    parts.append("".join(buf))
    for part in parts:
        part = part.strip()
        if not part or part == "void" or part.startswith("..."):
            if part.startswith("..."):
                labels.append("...")
            continue
        words = part.replace("*", " ").replace("&", " ").split()
        words = [word for word in words if word not in ("const", "volatile", "struct", "class")]
        if words:
            labels.append(words[-1][:40])
    if not labels:
        return name + "()"
    bits = ["${%s:%s}" % (index, label) for index, label in enumerate(labels, 1)]
    return "%s(%s)" % (name, ", ".join(bits))


def _symbol(owner: str, name: str, kind: str, signature: str) -> dict[str, Any]:
    return {
        "t": "s", "owner": owner, "name": name, "kind": kind,
        "signature": signature, "doc": "", "returns": "",
    }


def _names(frames: list[dict[str, Any]], kind: str) -> list[str]:
    return [frame["name"] for frame in frames if frame["kind"] == kind and frame["name"]]


def scan_source(text: str, header: str = "", c_only: bool = False) -> list[dict[str, Any]]:
    """Return records for one header (``header`` is the include spelling).

    ``c_only`` keeps file-scope prototypes and skips classes.
    """
    out = []
    parts = header.split("/")
    if header and "__" not in parts and "bits" not in parts and not os.path.basename(header).startswith("_"):
        out.append({"t": "m", "name": header, "doc": ""})
    if c_only:
        for raw in text.splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("typedef"):
                continue
            match = _PROTO_RE.search(stripped.split("{", 1)[0])
            if not match:
                continue
            name = match.group(1)
            if name in _KEYWORDS or name.startswith("_"):
                continue
            before = stripped[:match.start(1)]
            if before and not re.search(r"[\s\*]$", before):
                continue
            out.append(_symbol("c", name, "function", _snippet(name, match.group(2))))
        return out

    text = _prepare(text)
    depth = 0
    frames = []
    # A namespace or class whose "{" is on a later line (libstdc++ style).
    pending = None
    for raw in text.splitlines():
        frames[:] = [frame for frame in frames if frame["depth"] <= depth]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if pending:
            if "{" in stripped.split(";", 1)[0]:
                pending["depth"] = depth + 1
                frames.append(pending)
                pending = None
                depth += stripped.count("{") - stripped.count("}")
                continue
            if ";" in stripped:
                pending = None
        if re.match(r"public\s*:", stripped):
            for frame in reversed(frames):
                if frame["kind"] == "type":
                    frame["pub"] = True
                    break
        elif re.match(r"(?:private|protected)\s*:", stripped):
            for frame in reversed(frames):
                if frame["kind"] == "type":
                    frame["pub"] = False
                    break
        opened = stripped.count("{")
        namespace = _NS_RE.match(stripped)
        klass = _CLASS_RE.search(stripped)
        if namespace:
            name = namespace.group(1)
            visible = "" if name.startswith("_") or stripped.startswith("inline ") else name
            frame = {"kind": "ns", "name": visible, "depth": depth + 1}
            if opened:
                frames.append(frame)
            elif not stripped.endswith(";"):
                pending = frame
        elif klass and not klass.group(2).startswith("_"):
            simple = klass.group(2)
            prefix = [name for name in _names(frames, "ns")]
            owner = "::".join(prefix + [simple])
            out.append({"t": "m", "name": owner, "doc": ""})
            if prefix:
                out.append(_symbol("::".join(prefix), simple, "type", simple))
            frame = {
                "kind": "type",
                "name": owner,
                "depth": depth + 1,
                "pub": klass.group(1) == "struct",
            }
            if opened:
                frames.append(frame)
            elif not stripped.endswith(";"):
                pending = frame
        elif not stripped.startswith(("template", "using ", "typedef", "friend ")):
            signature_line = stripped.split("{", 1)[0].strip()
            match = _FN_RE.match(signature_line)
            name = match.group(1) if match else ""
            if match and name not in _KEYWORDS and not name.startswith("_"):
                holder = next((frame for frame in reversed(frames) if frame["kind"] == "type"), None)
                if holder and holder.get("pub") and holder["depth"] <= depth:
                    out.append(_symbol(holder["name"], name, "function", _snippet(name, match.group(2))))
                elif holder is None:
                    prefix = [item for item in _names(frames, "ns")]
                    owner = "::".join(prefix) if prefix else "c"
                    out.append(_symbol(owner, name, "function", _snippet(name, match.group(2))))
        depth += opened - stripped.count("}")
        if depth < 0:
            depth = 0
    return out


def _paths(dirs: list[str], limit: int, seen: set[str]) -> Iterator[str]:
    for path in walk_files(dirs, (".h", ".hh", ".hpp", ".hxx", ".inl"), limit, _SKIP, allow_plain=True):
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        yield path
        if len(seen) >= limit:
            return


def scan_dirs(
    cxx_dirs: Optional[Iterable[str]],
    c_dirs: Optional[Iterable[str]] = None,
    limit: int = MAX_FILES,
) -> Iterator[dict[str, Any]]:
    """Yield records from C++ include dirs, then C include dirs."""
    def records() -> Iterator[dict[str, Any]]:
        seen = set()
        cxx = [path for path in cxx_dirs or [] if path and os.path.isdir(path)]
        only_c = [
            path for path in (c_dirs or [])
            if path and os.path.isdir(path) and path not in cxx
        ]
        for path in _paths(cxx, limit, seen):
            root = next((item for item in cxx if path.startswith(item + os.sep) or path == item), cxx[0])
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            text = read_text(path)
            if text:
                for rec in scan_source(text, rel, False):
                    yield rec
        for path in _paths(only_c, limit, seen):
            root = next((item for item in only_c if path.startswith(item + os.sep) or path == item), only_c[0])
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            text = read_text(path)
            if text:
                for rec in scan_source(text, rel, True):
                    yield rec
    yield from merge_modules(records())
