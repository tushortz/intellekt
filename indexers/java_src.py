"""Scan JDK ``src.zip`` for public types, methods, and fields.

NOTE: this is a line-oriented scan, not a Java parser. It misses unusual
formatting, most annotations, and generated sources. Methods written inside
anonymous classes are attributed to the enclosing type. Interface constants
that omit ``public`` are skipped. The upgrade path is ``javap`` over the
JDK's ``jmods`` if this scan drops members that matter.

NOTE: each zip stops after ``MAX_FILES`` compilation units, and directories
named ``test``, ``tests``, or ``testing`` are skipped. A normal JDK is under
the cap. The upgrade path is raising ``MAX_FILES``.
"""

import re
import zipfile

MAX_FILES = 20000
_SKIP_DIRS = {"test", "tests", "testing"}

_TYPE_BEFORE = re.compile(
    r"(?:(public|protected|private)\s+)?"
    r"(?:(?:abstract|final|sealed|static|strictfp|non-sealed)\s+)*"
    r"(class|interface|enum|record)\s+"
    r"([A-Za-z_]\w*)"
    r"(?:\s*<[^;{}]*>)?"
    r"(?:\s*\([^;{}]*\))?"
    r"(?:\s+extends\s+[^;{}]+)?"
    r"(?:\s+implements\s+[^;{}]+)?"
    r"(?:\s+permits\s+[^;{}]+)?"
    r"\s*$"
)

_MEMBER_RE = re.compile(
    r"^(?:(?P<access>public|protected|private)\s+)?"
    r"(?:(?:static|final|synchronized|native|abstract|default|strictfp)\s+)*"
    r"(?:<[^(){}=;]*>\s+)?"
    r"(?:(?P<ret>[\w.?]+(?:\s*<[^(){}=;]*>)?(?:\s*\[\])*)\s+)?"
    r"(?P<name>[A-Za-z_]\w*)\s*"
    r"\((?P<args>[^()]*)\)"
    r"\s*(?:throws\s+[\w.,\s|]+)?\s*[{;]?\s*$"
)

_FIELD_RE = re.compile(
    r"^(?:(?P<access>public|protected)\s+)"
    r"(?:(?:static|final|volatile|transient)\s+)*"
    r"(?P<typ>[\w.?]+(?:\s*<[^(){}=;]*>)?(?:\s*\[\])*)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*[=;]"
)

_NOT_NAMES = {
    "if", "for", "while", "switch", "catch", "return", "new", "class",
    "try", "do", "else", "throw", "case",
}


def _clean_javadoc(raw):
    if raw.startswith("/**"):
        raw = raw[3:]
    if raw.endswith("*/"):
        raw = raw[:-2]
    lines = []
    for line in raw.splitlines():
        lines.append(re.sub(r"^\s*\*\s?", "", line))
    text = " ".join(lines)
    text = re.sub(r"\{@\w+\s+([^}]*)\}", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def _skip_string(src, i, quote):
    n = len(src)
    i += 1
    while i < n:
        if src[i] == "\\":
            i += 2
            continue
        if src[i] == quote:
            return i + 1
        if src[i] == "\n":
            return i
        i += 1
    return n


def _prepare(src):
    """Blank strings and non-doc comments. Keep indexes aligned with ``src``.

    Javadoc text is returned as ``(end_index, cleaned)`` pairs.
    """
    out = []
    docs = []
    i = 0
    n = len(src)
    append = out.append
    while i < n:
        if src.startswith("/**", i):
            end_rel = src.find("*/", i + 3)
            end = n if end_rel < 0 else end_rel + 2
            raw = src[i:end]
            for char in raw:
                append("\n" if char == "\n" else " ")
            docs.append((len(out), _clean_javadoc(raw)))
            i = end
            continue
        if src.startswith("/*", i):
            end_rel = src.find("*/", i + 2)
            end = n if end_rel < 0 else end_rel + 2
            for char in src[i:end]:
                append("\n" if char == "\n" else " ")
            i = end
            continue
        if src.startswith("//", i):
            end = src.find("\n", i)
            end = n if end < 0 else end
            out.extend(" " * (end - i))
            i = end
            continue
        if src[i] in "\"'":
            quote = src[i]
            if src.startswith(quote * 3, i):
                end = src.find(quote * 3, i + 3)
                end = n if end < 0 else end + 3
            else:
                end = _skip_string(src, i, quote)
            for char in src[i:end]:
                append("\n" if char == "\n" else " ")
            i = end
            continue
        append(src[i])
        i += 1
    return "".join(out), docs


def _doc_before(docs, pos, floor):
    found = ""
    for end, text in docs:
        if end > pos:
            break
        if end > floor:
            found = text
    return found


def _at_keyword(src, i, word):
    if not src.startswith(word, i):
        return False
    after = i + len(word)
    if after < len(src) and (src[after].isalnum() or src[after] == "_"):
        return False
    return True


def _read_stmt(masked, i):
    """Read up to ``;`` or the ``{`` that opens a body. Return end index at ``{`` or after ``;``."""
    j = i
    n = len(masked)
    parens = 0
    while j < n:
        char = masked[j]
        if char == "(":
            parens += 1
        elif char == ")":
            parens = max(0, parens - 1)
        elif char == "{" and parens == 0:
            return masked[i:j + 1], j
        elif char == ";" and parens == 0:
            return masked[i:j + 1], j + 1
        j += 1
    return masked[i:], n


def _split_args(args):
    parts = []
    buf = []
    depth = 0
    for char in args:
        if char == "<":
            depth += 1
        elif char == ">":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(char)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [part for part in parts if part and part != "void"]


def _snippet(name, args):
    bits = []
    for index, part in enumerate(_split_args(args), 1):
        safe = part.replace("$", "").replace("{", "").replace("}", "")
        bits.append("${%s:%s}" % (index, safe))
    if not bits:
        return f"{name}()"
    return f"{name}({', '.join(bits)})"


def _parse_member(stmt, kind):
    compact = re.sub(r"\s+", " ", stmt).strip()
    is_iface = kind == "interface"
    if "(" in compact:
        match = _MEMBER_RE.match(compact)
        if not match:
            return None
        access = match.group("access")
        name = match.group("name")
        if name in _NOT_NAMES:
            return None
        if access == "private":
            return None
        if not access and not is_iface and not compact.startswith(("default ", "static ")):
            return None
        return {
            "t": "s",
            "name": name,
            "kind": "function",
            "signature": _snippet(name, match.group("args") or ""),
            "doc": "",
        }
    if not compact.startswith(("public", "protected")):
        return None
    match = _FIELD_RE.match(compact)
    if not match or match.group("name") in _NOT_NAMES:
        return None
    name = match.group("name")
    return {
        "t": "s",
        "name": name,
        "kind": "variable",
        "signature": name,
        "doc": "",
    }


def scan_source(text):
    """Return module and symbol records for one compilation unit."""
    masked, docs = _prepare(text)
    package = ""
    found = re.search(r"(?:^|\n)\s*package\s+([\w.]+)\s*;", masked)
    if found:
        package = found.group(1)

    out = []
    n = len(masked)
    i = 0
    depth = 0
    stack = []
    floor = 0
    while i < n:
        char = masked[i]
        if char.isspace():
            i += 1
            continue
        if char == "{":
            window = masked[max(0, i - 500):i]
            decl = _TYPE_BEFORE.search(window)
            depth += 1
            if decl:
                access, kind, simple = decl.group(1), decl.group(2), decl.group(3)
                parent = next((frame for frame in reversed(stack) if not frame["skip"]), None)
                skip = access == "private" or bool(parent and parent["skip"])
                doc = _doc_before(docs, i, floor)
                floor = i
                if skip:
                    stack.append({"depth": depth, "skip": True})
                else:
                    if parent:
                        fqcn = parent["fqcn"] + "." + simple
                    elif package:
                        fqcn = package + "." + simple
                    else:
                        fqcn = simple
                    stack.append({
                        "depth": depth,
                        "skip": False,
                        "fqcn": fqcn,
                        "simple": simple,
                        "kind": kind,
                    })
                    out.append({"t": "m", "name": fqcn, "doc": doc})
                    if parent:
                        out.append({
                            "t": "s",
                            "owner": parent["fqcn"],
                            "name": simple,
                            "kind": "type",
                            "signature": simple,
                            "doc": doc,
                        })
            i += 1
            continue
        if char == "}":
            if stack and stack[-1]["depth"] == depth:
                stack.pop()
            depth = max(0, depth - 1)
            i += 1
            continue

        frame = stack[-1] if stack else None
        if frame and not frame["skip"] and depth == frame["depth"]:
            starts = (
                _at_keyword(masked, i, "public")
                or _at_keyword(masked, i, "protected")
                or (
                    frame["kind"] == "interface"
                    and (
                        _at_keyword(masked, i, "default")
                        or _at_keyword(masked, i, "static")
                        or masked[i].isalpha()
                        or masked[i] == "_"
                    )
                )
            )
            if starts:
                _stmt, nxt = _read_stmt(masked, i)
                if nxt <= i:
                    i += 1
                    continue
                rec = _parse_member(_stmt, frame["kind"])
                if rec:
                    rec["owner"] = frame["fqcn"]
                    rec["doc"] = _doc_before(docs, i, floor)
                    floor = i
                    out.append(rec)
                    i = nxt
                    continue
        i += 1
    return out


def scan_zip(path, limit=MAX_FILES):
    """Yield records from a source zip. Stops after ``limit`` ``.java`` files."""
    seen = 0
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = info.filename
            if not name.endswith(".java"):
                continue
            base = name.rsplit("/", 1)[-1]
            if base in ("module-info.java", "package-info.java"):
                continue
            parts = name.split("/")
            if any(part in _SKIP_DIRS for part in parts):
                continue
            if seen >= limit:
                break
            seen += 1
            try:
                text = archive.read(info).decode("utf-8", "replace")
                yield from scan_source(text)
            except Exception:
                # One broken compilation unit should not discard the rest of the zip.
                continue
