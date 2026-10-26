"""Scan .NET XML documentation files.

``T:`` is a type, ``M:`` a method, ``P:`` a property, ``F:`` a field.
``System.String.Substring(System.Int32)`` becomes owner ``System.String``,
name ``Substring``. Generic arity markers (``List`1``) are removed.

NOTE: NuGet packages under ``~/.nuget/packages`` stop after ``MAX_FILES``
XML files. Project ``bin`` folders are not read. Upgrade path: the
targeting pack's ref assemblies via System.Reflection.Metadata.

NOTE: ``System`` is treated as in scope by the resolver, which is wrong for
projects that disable implicit usings.
"""

import re
import xml.etree.ElementTree as ET

from . import merge_modules, read_text, walk_files

MAX_FILES = 600
_NUGET_SKIP = {
    "test", "tests", "testing", "runtimes", "tools", "build", "content",
    "analyzers", "images", "node_modules",
}


def _summary(member):
    for child in list(member):
        if not str(child.tag).endswith("summary"):
            continue
        text = "".join(child.itertext())
        return re.sub(r"\s+", " ", text).strip()
    return ""


def _snippet(name, params):
    labels = []
    for part in (params or "").split(","):
        part = re.sub(r"`\d+", "", part).strip()
        if not part:
            continue
        labels.append(part.split(".")[-1][:40])
    if not labels:
        return name + "()"
    bits = ["${%s:%s}" % (index, label) for index, label in enumerate(labels, 1)]
    return "%s(%s)" % (name, ", ".join(bits))


def _split_id(mid):
    if len(mid) < 3 or mid[1] != ":":
        return None
    kind, rest = mid[0], mid[2:]
    params = ""
    if "(" in rest:
        rest, params = rest.split("(", 1)
        params = params.rsplit(")", 1)[0]
    rest = re.sub(r"`\d+", "", rest)
    params = re.sub(r"`\d+", "", params)
    if kind == "T":
        return "type", rest, "", ""
    if kind not in "MPFE" or "." not in rest:
        return None
    owner, name = rest.rsplit(".", 1)
    if not name or name.startswith("#") or name in ("ctor", "cctor"):
        return None
    return kind, owner, name, params


def scan_xml(text):
    """Return records for one XML documentation document."""
    def generate():
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return
        for member in root.iter():
            if not str(member.tag).endswith("member"):
                continue
            parsed = _split_id(member.get("name") or "")
            if not parsed:
                continue
            kind, full, name, params = parsed
            doc = _summary(member)
            parts = [part for part in full.split(".") if part]
            if not parts:
                continue
            if kind == "type":
                for index in range(1, len(parts) + 1):
                    yield {
                        "t": "m",
                        "name": ".".join(parts[:index]),
                        "doc": doc if index == len(parts) else "",
                    }
                if len(parts) >= 2:
                    yield {
                        "t": "s",
                        "owner": ".".join(parts[:-1]),
                        "name": parts[-1],
                        "kind": "type",
                        "signature": parts[-1],
                        "doc": doc,
                        "returns": "",
                    }
                    for index in range(1, len(parts) - 1):
                        yield {
                            "t": "s",
                            "owner": ".".join(parts[:index]),
                            "name": parts[index],
                            "kind": "namespace",
                            "signature": parts[index],
                            "doc": "",
                            "returns": "",
                        }
                continue
            sym_kind = "function" if kind in "ME" else "variable"
            signature = _snippet(name, params) if sym_kind == "function" else name
            yield {
                "t": "s",
                "owner": full,
                "name": name,
                "kind": sym_kind,
                "signature": signature,
                "doc": doc,
                "returns": "",
            }
    return list(merge_modules(generate()))


def scan_roots(roots, limit=MAX_FILES):
    """Yield records from documentation XML under ``roots``.

    The first root is the targeting pack. Later roots (the NuGet cache)
    share whatever file budget is left.
    """
    def records():
        seen = 0
        for index, root in enumerate(roots):
            skip = _NUGET_SKIP if index else {"test", "tests", "testing"}
            for path in walk_files([root], (".xml",), limit - seen, skip):
                text = read_text(path, limit=8_000_000)
                if "<member" not in text:
                    continue
                seen += 1
                for rec in scan_xml(text):
                    yield rec
                if seen >= limit:
                    return
    yield from merge_modules(records())
