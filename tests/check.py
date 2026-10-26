"""One check for the index, the Java scanner, and the external indexers.

Run from anywhere:

    python3 tests/check.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import core
from indexers import (
    cpp_src, cs_src, go_src, java_src, markup_src, matlab_src, perl_src,
    rust_src, ts_src,
)

JAVA_FIXTURE = """
package demo;

/**
 * A sample list.
 * Second sentence.
 */
public class Widget {
    /** How many. */
    public int count;

    /**
     * Adds a name.
     */
    public void add(String name, int times) {
        String skip = "{";
        return;
    }

    public String get(int index) {
        return null;
    }

    public static class Inner {
        public void ping() {}
    }
}
"""

JAVA_INTERFACE = """
package demo;

public interface Bucket {
    /** Puts an item. */
    void put(String item);
}
"""


def check(cond, message):
    if not cond:
        raise SystemExit("FAIL: " + message)


def symbols(records, name=None, owner=None):
    rows = [rec for rec in records if rec.get("t") == "s"]
    if name is not None:
        rows = [rec for rec in rows if rec["name"] == name]
    if owner is not None:
        rows = [rec for rec in rows if rec["owner"] == owner]
    return rows


def check_cache():
    directory = tempfile.mkdtemp(prefix="intellekt-check-")
    try:
        db_path = os.path.join(directory, "index.sqlite")
        core.write_language(db_path, "python", "/usr/bin/python3", "3", [
            {"t": "m", "name": "json", "doc": "JSON encoder"},
            {"t": "s", "owner": "json", "name": "dump", "kind": "function",
             "signature": "dump()", "doc": "Dump"},
            {"t": "s", "owner": "json", "name": "dumps", "kind": "function",
             "signature": "dumps(${1:obj})", "doc": "Dumps"},
            {"t": "s", "owner": "json", "name": "load", "kind": "function",
             "signature": "load()", "doc": ""},
            {"t": "s", "owner": "json", "name": "dump", "kind": "function",
             "signature": "dump(${1:obj}, ${2:fp})", "doc": "Dump to a file"},
            {"t": "s", "owner": "json", "name": "_private", "kind": "function",
             "signature": "_private()", "doc": ""},
        ])
        names = [row[0] for row in core.query_symbols(db_path, "python", "json", "du")]
        check(names == ["dump", "dump", "dumps"], "prefix query got %s" % names)
        public = [row[0] for row in core.query_symbols(
            db_path, "python", "json", "", public_only=True,
        )]
        check("_private" not in public, "public import names got %s" % public)
        check(core.is_fresh(db_path, "python", "/usr/bin/python3", "3"), "cache should be fresh")
        check(not core.is_fresh(db_path, "python", "/usr/bin/python3", "4"), "version mismatch")
        core.write_language(db_path, "ruby", "/usr/bin/ruby", "3", [
            {"t": "m", "name": "set", "doc": "sets"},
        ])
        check(core.module_exists(db_path, "python", "json"), "python module survived the ruby write")
        check(core.query_symbols(db_path, "python", "json", "load"), "python symbols survived")
        segments = core.import_segments(
            [
                ("java.util.ArrayList", "list"),
                ("java.util.Arrays", ""),
                ("java.util.concurrent.Future", ""),
            ],
            "java.ut",
        )
        check([item["trigger"] for item in segments] == ["util"], "import segment got %s" % segments)
        check(segments[0]["kind"] == "namespace", "package segment should be a namespace")
        classes = core.import_segments(
            [("java.util.ArrayList", "A list."), ("java.util.Arrays", "")],
            "java.util.",
        )
        triggers = [item["trigger"] for item in classes]
        check(triggers == ["ArrayList", "Arrays"], "class segments got %s" % triggers)
        check(classes[0]["doc"] == "A list.", "exact class keeps its doc")
        imported = core.from_import_segments(
            [("random", ""), ("random.Random", "generator")],
            [
                ("Random", "type", "Random", "generator"),
                ("randint", "function", "randint()", "an int"),
                ("randrange", "function", "randrange()", ""),
                ("_randbelow", "function", "_randbelow()", ""),
            ],
            "random.",
        )
        triggers = [item["trigger"] for item in imported]
        check(triggers == ["randint", "Random", "randrange"], "from import got %s" % triggers)
        check(imported[0]["doc"] == "an int", "from import keeps the function doc")
        narrowed = core.from_import_segments(
            [("random.Random", "")],
            [
                ("randint", "function", "randint()", ""),
                ("randrange", "function", "randrange()", ""),
            ],
            "random.randr",
        )
        check(
            [item["trigger"] for item in narrowed] == ["randrange"],
            "from import prefix got %s" % narrowed,
        )
    finally:
        shutil.rmtree(directory)


def check_resolve():
    directory = tempfile.mkdtemp(prefix="intellekt-check-")
    try:
        db_path = os.path.join(directory, "index.sqlite")
        core.write_language(db_path, "java", "/jdk", "17", [
            {"t": "m", "name": "java.util.ArrayList", "doc": "A list."},
            {"t": "m", "name": "java.lang.String", "doc": "Text."},
            {"t": "s", "owner": "java.util.ArrayList", "name": "add",
             "kind": "function", "signature": "add(${1:E e})", "doc": "Appends."},
        ])
        python_src = "import json\nfrom json import JSONDecoder\ndata = JSONDecoder()\n"
        check(core.resolve_owner(db_path, "python", python_src, "json") == "json", "import json")
        check(
            core.resolve_owner(db_path, "python", python_src, "JSONDecoder") == "json.JSONDecoder",
            "from import",
        )
        check(
            core.resolve_owner(db_path, "python", python_src, "data") == "json.JSONDecoder",
            "assignment",
        )
        check(core.resolve_owner(db_path, "python", python_src, "str") == "builtins.str", "builtin")
        core.write_language(db_path, "python", "fixture", "1", [
            {"t": "m", "name": "datetime.datetime", "doc": ""},
            {"t": "s", "owner": "datetime.datetime", "name": "now", "kind": "function",
             "signature": "now()", "doc": "", "returns": "datetime.datetime"},
            {"t": "s", "owner": "datetime.datetime", "name": "year", "kind": "variable",
             "signature": "year", "doc": ""},
        ])
        clock = "from datetime import datetime\na = datetime.now()\n"
        check(
            core.resolve_owner(db_path, "python", clock, "a") == "datetime.datetime",
            "now() returns the class",
        )
        members = [row[0] for row in core.query_symbols(db_path, "python", "datetime.datetime", "")]
        check("year" in members, "year missing: %s" % members)
        bare = "from datetime import datetime\na = datetime.now\n"
        check(
            core.resolve_owner(db_path, "python", bare, "a") == "datetime.datetime.now",
            "now without a call stays the function",
        )
        typed = (
            "from datetime import datetime\n"
            "def show(when: datetime | None, other: Optional[datetime]):\n"
            "    pass\n"
            "a: datetime\n"
            "items: list[datetime]\n"
            "for item in items:\n"
            "    pass\n"
            "b: datetime\n"
            "b = mystery()\n"
        )
        check(core.resolve_owner(db_path, "python", typed, "when") == "datetime.datetime",
              "parameter annotation")
        check(core.resolve_owner(db_path, "python", typed, "other") == "datetime.datetime",
              "Optional annotation")
        check(core.resolve_owner(db_path, "python", typed, "a") == "datetime.datetime",
              "variable annotation")
        check(core.resolve_owner(db_path, "python", typed, "items") == "builtins.list",
              "list[datetime] is a list")
        check(core.resolve_owner(db_path, "python", typed, "item") == "datetime.datetime",
              "for-loop element type")
        check(core.resolve_owner(db_path, "python", typed, "b") == "datetime.datetime",
              "annotation survives an untyped call")
        broken = "from datetime import datetime\ndef show(when: datetime):\n    when.\n"
        check(core.resolve_owner(db_path, "python", broken, "when") == "datetime.datetime",
              "half-typed line still parses")
        shim = "from datetime import datetime\n" + core._rewrite_unions(
            "def show(when: datetime | None):\n    pass\n"
        )
        check(core.resolve_owner(db_path, "python", shim, "when") == "datetime.datetime",
              "3.8-style Union annotation")
        check(core.import_typed("python", "import os.pa") == "os.pa", "python import path")
        check(core.import_typed("python", "from json import JSO") == "json.JSO", "from import path")
        check(core.import_typed("python", "from random import") == "random.", "from import with no name")
        check(core.import_typed("python", "from col") == "col", "from package path")
        check(core.import_typed("python", "from collections.") == "collections.", "from package dot")
        check(core.import_typed("python", "from ") == "", "from with no name yet")
        packages = core.import_segments(
            [("collections", ""), ("collections.abc", "containers"), ("json", "")],
            "col",
        )
        check(
            [item["trigger"] for item in packages] == ["collections"],
            "from package got %s" % packages,
        )
        children = core.import_segments(
            [("collections", ""), ("collections.abc", "containers"), ("json", "")],
            "collections.",
        )
        check(
            [item["trigger"] for item in children] == ["abc"],
            "from package child got %s" % children,
        )
        check(core.receiver_from("items = ArrayList(); items") == "items", "receiver")
        check(core.receiver_from("Foo::Bar") == "Foo::Bar", "ruby constant receiver")
        check(core.receiver_from("self") is None, "skip self")

        java_src = "package app;\nimport java.util.*;\nArrayList items;\n"
        check(
            core.resolve_owner(db_path, "java", java_src, "items") == "java.util.ArrayList",
            "java variable",
        )
        check(
            core.resolve_owner(db_path, "java", java_src, "ArrayList") == "java.util.ArrayList",
            "java star import",
        )
        check(
            core.resolve_owner(db_path, "java", java_src, "String") == "java.lang.String",
            "java.lang",
        )
        check(core.import_typed("java", "import java.util.Arr") == "java.util.Arr", "java import path")
        ruby_src = "items = Widget.new\n"
        check(core.resolve_owner(db_path, "ruby", ruby_src, "items") == "Widget", "ruby assignment")
        check(core.resolve_owner(db_path, "ruby", ruby_src, "Array") == "Array", "ruby constant")
        rows = core.query_symbols(db_path, "java", "java.util.ArrayList", "")
        check(rows and rows[0][0] == "add", "java member lookup")
        check(core.symbol_doc(db_path, "java", "java.util.ArrayList", "add") == "Appends.", "symbol doc")
        check(core.module_doc(db_path, "java", "java.util.ArrayList") == "A list.", "module doc")
        check(core.member_op("items->") == ("items", "->"), "php arrow")
        check(core.member_op("obj?.") == ("obj", "?."), "optional chain")
        check(core.member_op("Foo::") == ("Foo", "::"), "colon colon")
        check(core.receiver_from("$items") == "$items", "php receiver")

        core.write_language(db_path, "javascript", "node", "1", [
            {"t": "m", "name": "fs", "doc": "files"},
            {"t": "s", "owner": "fs", "name": "readFile", "kind": "function",
             "signature": "readFile()", "doc": ""},
            {"t": "m", "name": "fs.Stats", "doc": ""},
            {"t": "s", "owner": "fs", "name": "Stats", "kind": "type",
             "signature": "Stats", "doc": ""},
            {"t": "s", "owner": "fs.Stats", "name": "isFile", "kind": "function",
             "signature": "isFile()", "doc": ""},
        ])
        core.write_language(db_path, "typescript", "tsc", "1", [
            {"t": "m", "name": "Console", "doc": "log sink"},
            {"t": "s", "owner": "Console", "name": "log", "kind": "function",
             "signature": "log(${1:message})", "doc": "Writes."},
            {"t": "s", "owner": "global", "name": "console", "kind": "variable",
             "signature": "console", "doc": "", "returns": "Console"},
        ])
        js = "import fs from 'fs'\nimport { Stats } from 'fs'\nconst file = new Stats()\n"
        check(core.resolve_owner(db_path, "javascript", js, "fs") == "fs", "js default import")
        check(core.resolve_owner(db_path, "javascript", js, "Stats") == "fs.Stats", "js named import")
        check(core.resolve_owner(db_path, "javascript", js, "file") == "fs.Stats", "js new")
        check(core.resolve_owner(db_path, "typescript", "", "console") == "Console", "ts console")
        check(core.import_typed("javascript", "import fs from 'f") == "f", "js import path")
        check(core.import_typed("javascript", "const fs = require('no") == "no", "js require path")

        core.write_language(db_path, "csharp", "dotnet", "1", [
            {"t": "m", "name": "System", "doc": ""},
            {"t": "m", "name": "System.String", "doc": "Text."},
            {"t": "s", "owner": "System", "name": "String", "kind": "type",
             "signature": "String", "doc": "Text."},
            {"t": "s", "owner": "System.String", "name": "Substring", "kind": "function",
             "signature": "Substring(${1:Int32})", "doc": "Slice.", "returns": "System.String"},
        ])
        cs = "using System;\nString name;\nvar items = new String();\n"
        check(core.resolve_owner(db_path, "csharp", cs, "name") == "System.String", "csharp var")
        check(core.resolve_owner(db_path, "csharp", cs, "String") == "System.String", "csharp System")
        check(core.resolve_owner(db_path, "csharp", cs, "items") == "System.String", "csharp new")
        check(core.import_typed("csharp", "using System.Col") == "System.Col", "csharp using")
        bare = [row[0] for row in core.bare_names(db_path, "csharp", cs, "Str")]
        check("String" in bare, "csharp bare String: %s" % bare)

        core.write_language(db_path, "go", "go", "1", [
            {"t": "m", "name": "bytes", "doc": ""},
            {"t": "m", "name": "bytes.Buffer", "doc": "A buffer."},
            {"t": "s", "owner": "bytes", "name": "NewBuffer", "kind": "function",
             "signature": "NewBuffer()", "doc": "", "returns": "bytes.Buffer"},
            {"t": "s", "owner": "bytes.Buffer", "name": "Write", "kind": "function",
             "signature": "Write()", "doc": ""},
            {"t": "m", "name": "error", "doc": ""},
            {"t": "s", "owner": "error", "name": "Error", "kind": "function",
             "signature": "Error()", "doc": ""},
        ])
        go = 'import "bytes"\nbuf := bytes.NewBuffer()\nvar err error\n'
        check(core.resolve_owner(db_path, "go", go, "bytes") == "bytes", "go import")
        check(core.resolve_owner(db_path, "go", go, "buf") == "bytes.Buffer", "go call return")
        check(core.resolve_owner(db_path, "go", go, "err") == "error", "go builtin")
        check(core.import_typed("go", 'import "net/ht') == "net/ht", "go import path")

        core.write_language(db_path, "php", "php", "1", [
            {"t": "m", "name": "DateTime", "doc": ""},
            {"t": "s", "owner": "DateTime", "name": "format", "kind": "function",
             "signature": "format(${1:format})", "doc": ""},
            {"t": "s", "owner": "php", "name": "strlen", "kind": "function",
             "signature": "strlen(${1:string})", "doc": "Length."},
        ])
        php = "$when = new DateTime();\n"
        check(core.resolve_owner(db_path, "php", php, "$when") == "DateTime", "php new")
        check(core.import_typed("php", "use Foo\\Ba") == "Foo\\Ba", "php use")
        php_bare = [row[0] for row in core.bare_names(db_path, "php", php, "str")]
        check("strlen" in php_bare, "php function: %s" % php_bare)

        core.write_language(db_path, "rust", "rustc", "1", [
            {"t": "m", "name": "alloc::vec::Vec", "doc": ""},
            {"t": "s", "owner": "alloc::vec::Vec", "name": "new", "kind": "function",
             "signature": "new()", "doc": "", "returns": "alloc::vec::Vec"},
            {"t": "s", "owner": "alloc::vec::Vec", "name": "push", "kind": "function",
             "signature": "push(${1:value})", "doc": ""},
        ])
        rust = "let items = Vec::new();\n"
        check(core.resolve_owner(db_path, "rust", rust, "items") == "alloc::vec::Vec", "rust prelude call")
        check(core.resolve_owner(db_path, "rust", rust, "Vec") == "alloc::vec::Vec", "rust prelude")
        check(core.import_typed("rust", "use std::vec::V") == "std::vec::V", "rust use")

        core.write_language(db_path, "cpp", "clang", "1", [
            {"t": "m", "name": "std", "doc": ""},
            {"t": "m", "name": "std::vector", "doc": ""},
            {"t": "s", "owner": "std", "name": "vector", "kind": "type",
             "signature": "vector", "doc": ""},
            {"t": "s", "owner": "std::vector", "name": "push_back", "kind": "function",
             "signature": "push_back(${1:value})", "doc": ""},
            {"t": "s", "owner": "c", "name": "printf", "kind": "function",
             "signature": "printf(${1:fmt})", "doc": ""},
        ])
        cpp = "std::vector<int> items;\n"
        check(core.resolve_owner(db_path, "cpp", cpp, "items") == "std::vector", "cpp variable")
        check(core.resolve_owner(db_path, "cpp", cpp, "std") == "std", "cpp namespace")
        check(core.import_typed("cpp", "#include <vec") == "vec", "cpp include")
        c_bare = [row[0] for row in core.bare_names(db_path, "cpp", cpp, "print")]
        check("printf" in c_bare, "c printf: %s" % c_bare)
    finally:
        shutil.rmtree(directory)


def check_java_scan():
    records = java_src.scan_source(JAVA_FIXTURE)
    modules = [rec["name"] for rec in records if rec["t"] == "m"]
    check("demo.Widget" in modules, "widget module missing: %s" % modules)
    check("demo.Widget.Inner" in modules, "inner module missing: %s" % modules)
    widget = next(rec for rec in records if rec["t"] == "m" and rec["name"] == "demo.Widget")
    check("sample list" in widget["doc"].lower(), "class doc: %s" % widget["doc"])
    add = symbols(records, "add", "demo.Widget")
    check(add, "add method missing")
    check("String name" in add[0]["signature"], "add signature: %s" % add[0]["signature"])
    check("Adds a name" in add[0]["doc"], "add doc: %s" % add[0]["doc"])
    check(symbols(records, "count", "demo.Widget"), "count field missing")
    check(symbols(records, "get", "demo.Widget"), "get method missing")
    check(symbols(records, "ping", "demo.Widget.Inner"), "inner method missing")
    check(not symbols(records, "ping", "demo.Widget"), "inner method leaked onto Widget")

    iface = java_src.scan_source(JAVA_INTERFACE)
    put = symbols(iface, "put", "demo.Bucket")
    check(put, "interface method missing")
    check("Puts an item" in put[0]["doc"], "interface doc: %s" % put[0]["doc"])

    found = core.detect_java()
    if not found:
        print("skip jdk ArrayList (no src.zip)")
        return
    import zipfile
    wanted = None
    with zipfile.ZipFile(found["sources"][0]) as archive:
        for name in archive.namelist():
            if name.endswith("java/util/ArrayList.java"):
                wanted = archive.read(name).decode("utf-8", "replace")
                break
    check(wanted, "ArrayList.java not in src.zip")
    live = java_src.scan_source(wanted)
    adds = symbols(live, "add", "java.util.ArrayList")
    check(adds, "JDK ArrayList.add was not found")
    check(any("Append" in rec["doc"] for rec in adds), "JDK ArrayList.add lost its doc")


def check_python_indexer():
    out_path = os.path.join(tempfile.mkdtemp(prefix="intellekt-py-"), "out.jsonl")
    script = os.path.join(ROOT, "indexers", "python_index.py")
    lib = os.path.join(ROOT, "tests", "fixtures", "pylib")
    proc = subprocess.run(
        [sys.executable, script, "--lib", lib, "--out", out_path],
        capture_output=True, text=True,
    )
    check(proc.returncode == 0, proc.stderr or proc.stdout)
    records = list(core.iter_jsonl(out_path))
    greet = symbols(records, "greet", "demo")
    check(greet, "greet missing: %s" % records)
    check("${1:name}" in greet[0]["signature"], "greet signature: %s" % greet[0]["signature"])
    check("Say hello" in greet[0]["doc"], "greet doc")
    check(symbols(records, "Box", "demo"), "Box class missing")
    put = symbols(records, "put", "demo.Box")
    aliased = symbols(records, "put", "demo")
    check(
        aliased and "value" in aliased[0]["signature"] and "Store it." in aliased[0]["doc"],
        "module alias of a method: %s" % aliased,
    )
    check(put and "self" not in put[0]["signature"], "put signature: %s" % (put and put[0]["signature"]))
    check(any(rec["t"] == "m" and rec["name"] == "demo" and "Demo package" in rec["doc"] for rec in records),
          "demo module doc")
    client = symbols(records, "Client", "extpkg")
    check(client and client[0]["kind"] == "type", "re-exported Client missing: %s" % records)
    ping = symbols(records, "ping", "extpkg")
    check(ping and "host" in ping[0]["signature"], "re-exported ping missing: %s" % ping)
    close = symbols(records, "close", "extpkg.Client")
    check(close and "timeout" in close[0]["signature"] and "self" not in close[0]["signature"],
          "Client.close missing: %s" % close)
    directory = tempfile.mkdtemp(prefix="intellekt-ext-")
    db_path = os.path.join(directory, "index.sqlite")
    try:
        core.write_language(db_path, "python", "fixture", "1", records)
        source = "from extpkg import Client\nimport extpkg\n"
        check(core.resolve_owner(db_path, "python", source, "Client") == "extpkg.Client",
              "from-import of an external class")
        check(core.resolve_owner(db_path, "python", source, "extpkg") == "extpkg",
              "import of an external package")
        names = [row[0] for row in core.query_symbols(db_path, "python", "extpkg.Client", "")]
        check("close" in names, "external class members: %s" % names)
        names = [row[0] for row in core.query_symbols(db_path, "python", "extpkg", "p")]
        check("ping" in names, "external package members: %s" % names)
        label = symbols(records, "label", "demo.Box")
        check(label and label[0]["kind"] == "variable" and label[0]["signature"] == "label",
              "property: %s" % label)
        born = symbols(records, "born", "demo.Box")
        check(born and born[0].get("returns") == "demo.Box", "classmethod return: %s" % born)
        box_src = "from demo import Box\nb = Box.born()\n"
        check(core.resolve_owner(db_path, "python", box_src, "b") == "demo.Box",
              "born() returns Box")
        check_datetime_properties()
    finally:
        shutil.rmtree(directory)


def check_datetime_properties():
    import importlib.util
    path = os.path.join(ROOT, "indexers", "python_index.py")
    spec = importlib.util.spec_from_file_location("python_index_live", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = []
    mod.index_c_module("datetime", out, set(), set())
    year = symbols(out, "year", "datetime.datetime")
    check(year and year[0]["kind"] == "variable", "datetime.year missing: %s" % year)
    now = symbols(out, "now", "datetime.datetime")
    check(now and now[0].get("returns") == "datetime.datetime",
          "datetime.now return: %s" % now)


def check_ruby_indexer():
    ruby = shutil.which("ruby")
    if not ruby:
        print("skip ruby indexer (ruby is not on PATH)")
        return
    out_path = os.path.join(tempfile.mkdtemp(prefix="intellekt-rb-"), "out.jsonl")
    script = os.path.join(ROOT, "indexers", "ruby_index.rb")
    lib = os.path.join(ROOT, "tests", "fixtures", "rblib")
    proc = subprocess.run(
        [ruby, script, "--lib", lib, "--out", out_path],
        capture_output=True, text=True,
    )
    check(proc.returncode == 0, proc.stderr or proc.stdout)
    records = list(core.iter_jsonl(out_path))
    add = symbols(records, "add", "Widget")
    check(add, "ruby add missing: %s" % records)
    check("name" in add[0]["signature"] and "times" in add[0]["signature"],
          "ruby signature: %s" % add[0]["signature"])
    check("Adds a name" in add[0]["doc"], "ruby doc: %s" % add[0]["doc"])
    proc = subprocess.run(
        [ruby, "-e", "load '%s'; puts snippet_from_ri('split', 'split(field_sep = $;, limit = 0) -> array')" % script],
        capture_output=True, text=True,
    )
    check(proc.returncode == 0, proc.stderr)
    check("\\$" in proc.stdout, "ruby snippet should escape $: %s" % proc.stdout.strip())


TS_FIXTURE = """
/** Log sink. */
interface Console {
    /** Writes a line. */
    log(message: string): void;
}
declare var console: Console;
export function greet(name: string): string;
declare module "fs" {
    export class Stats {
        isFile(): boolean;
    }
}
"""

CS_FIXTURE = """
<doc>
  <members>
    <member name="T:System.String">
      <summary>Represents text.</summary>
    </member>
    <member name="M:System.String.Substring(System.Int32)">
      <summary>Retrieves a substring.</summary>
    </member>
    <member name="P:System.String.Length">
      <summary>How many.</summary>
    </member>
  </members>
</doc>
"""

GO_FIXTURE = """
package widget

// Adds a name.
func (w *Widget) Add(name string, times int) {}

// Makes one.
func New() *Widget { return nil }

type Widget struct {}
"""

RUST_FIXTURE = """
/// A sample.
pub struct Widget {
    pub count: i32,
}

impl Widget {
    /// Adds a name.
    pub fn add(&self, name: &str, times: i32) {}
    pub fn new() -> Widget {}
}
"""

CPP_FIXTURE = """
namespace std {
class vector {
public:
    void push_back(int value);
    int size();
};
}

int printf(const char *fmt, ...);
"""

C_FIXTURE = """
int printf(const char *fmt, ...);
"""


def check_ts_scan():
    records = ts_src.scan_source(TS_FIXTURE)
    log = symbols(records, "log", "Console")
    check(log, "console.log missing: %s" % records)
    check("message" in log[0]["signature"], "log signature: %s" % log[0]["signature"])
    check("Writes a line" in log[0]["doc"], "log doc: %s" % log[0]["doc"])
    console = symbols(records, "console", "global")
    check(console and console[0].get("returns") == "Console", "console var: %s" % console)
    greet = symbols(records, "greet", "global")
    check(greet and "name" in greet[0]["signature"], "greet: %s" % greet)
    is_file = symbols(records, "isFile", "fs.Stats")
    check(is_file, "fs.Stats.isFile missing: %s" % records)


def check_cs_scan():
    records = cs_src.scan_xml(CS_FIXTURE)
    modules = [rec["name"] for rec in records if rec["t"] == "m"]
    check("System.String" in modules, "String type missing: %s" % modules)
    text = next(rec for rec in records if rec["t"] == "m" and rec["name"] == "System.String")
    check("Represents text" in text["doc"], "String doc: %s" % text["doc"])
    sub = symbols(records, "Substring", "System.String")
    check(sub and "Int32" in sub[0]["signature"], "Substring: %s" % sub)
    check("substring" in sub[0]["doc"], "Substring doc: %s" % sub[0]["doc"])
    check(symbols(records, "Length", "System.String"), "Length missing")
    check(symbols(records, "String", "System"), "String member of System missing")


def check_go_scan():
    records = go_src.scan_source(GO_FIXTURE, "widget")
    add = symbols(records, "Add", "widget.Widget")
    check(add and "name" in add[0]["signature"], "go Add: %s" % add)
    check("Adds a name" in add[0]["doc"], "go Add doc: %s" % add[0]["doc"])
    new = symbols(records, "New", "widget")
    check(new and new[0].get("returns") == "widget.Widget", "go New return: %s" % new)
    # A return type that continues on the next line is only ``*`` on this line.
    hung = go_src.scan_source("package p\nfunc F() * {\n}\n", "p")
    check(symbols(hung, "F", "p"), "go star return should still record F")
    check(any(rec["t"] == "m" and rec["name"] == "widget.Widget" for rec in records), "go Widget module")


def check_rust_scan():
    records = rust_src.scan_source(RUST_FIXTURE, "demo")
    add = symbols(records, "add", "demo::Widget")
    check(add and "name" in add[0]["signature"] and "self" not in add[0]["signature"],
          "rust add: %s" % add)
    check("Adds a name" in add[0]["doc"], "rust add doc: %s" % add[0]["doc"])
    made = symbols(records, "new", "demo::Widget")
    check(made and made[0].get("returns") == "demo::Widget", "rust new return: %s" % made)
    check(symbols(records, "count", "demo::Widget"), "rust field missing")
    aliased = rust_src.scan_source("pub struct Vec {\n    pub len: usize,\n}\n", "alloc::vec")
    names = [rec["name"] for rec in aliased if rec["t"] == "m"]
    check("alloc::vec::Vec" in names and "std::vec::Vec" in names, "rust std alias: %s" % names)


def check_cpp_scan():
    records = cpp_src.scan_source(CPP_FIXTURE, "vector", False)
    push = symbols(records, "push_back", "std::vector")
    check(push and "value" in push[0]["signature"], "push_back: %s" % push)
    check(symbols(records, "size", "std::vector"), "size missing")
    check(symbols(records, "vector", "std"), "vector type on std missing")
    check(symbols(records, "printf", "c"), "printf in cpp header missing")
    c_records = cpp_src.scan_source(C_FIXTURE, "stdio.h", True)
    printf = symbols(c_records, "printf", "c")
    check(printf and "fmt" in printf[0]["signature"], "c printf: %s" % printf)
    check(any(rec["t"] == "m" and rec["name"] == "stdio.h" for rec in c_records), "stdio module")

    found = core.detect_cpp()
    if not found:
        print("skip live C++ (no compiler)")
        return
    vector = ""
    for root in found["cxx_dirs"]:
        for rel in ("__vector/vector.h", "bits/stl_vector.h"):
            path = os.path.join(root, rel)
            if os.path.isfile(path):
                vector = path
                break
        if vector:
            break
    if not vector:
        print("skip live C++ vector (header not found)")
        return
    text = open(vector, encoding="utf-8", errors="replace").read()
    live = cpp_src.scan_source(text, os.path.basename(vector), False)
    owners = {rec["owner"] for rec in live if rec.get("name") == "push_back"}
    check("std::vector" in owners, "live vector push_back owners: %s" % sorted(owners))


def check_js_indexer():
    node = shutil.which("node")
    if not node:
        print("skip javascript indexer (node is not on PATH)")
        return
    out_path = os.path.join(tempfile.mkdtemp(prefix="intellekt-js-"), "out.jsonl")
    script = os.path.join(ROOT, "indexers", "js_index.js")
    lib = os.path.join(ROOT, "tests", "fixtures", "jslib")
    proc = subprocess.run(
        [node, script, "--lib", lib, "--out", out_path],
        capture_output=True, text=True,
    )
    check(proc.returncode == 0, proc.stderr or proc.stdout)
    records = list(core.iter_jsonl(out_path))
    greet = symbols(records, "greet", "demo")
    check(greet and "name" in greet[0]["signature"], "js greet: %s" % greet)
    add = symbols(records, "add", "demo.Widget")
    check(add and "name" in add[0]["signature"], "js Widget.add: %s" % records)


def check_php_indexer():
    php = shutil.which("php")
    if not php:
        print("skip php indexer (php is not on PATH)")
        return
    out_path = os.path.join(tempfile.mkdtemp(prefix="intellekt-php-"), "out.jsonl")
    script = os.path.join(ROOT, "indexers", "php_index.php")
    lib = os.path.join(ROOT, "tests", "fixtures", "phplib")
    proc = subprocess.run(
        [php, script, "--lib", lib, "--out", out_path],
        capture_output=True, text=True,
    )
    check(proc.returncode == 0, proc.stderr or proc.stdout)
    records = list(core.iter_jsonl(out_path))
    add = symbols(records, "add", "Widget")
    check(add and "name" in add[0]["signature"] and "times" in add[0]["signature"],
          "php add: %s" % add)
    check("Adds a name" in add[0]["doc"], "php add doc: %s" % add[0]["doc"])
    greet = symbols(records, "greet", "php")
    check(greet and "Says hello" in greet[0]["doc"], "php greet: %s" % greet)


def check_markdown():
    plain = core.markdown_html("Say **hello** to `name` and `x` and *world*.")
    check("<b>hello</b>" in plain and ">name</code>" in plain and ">x</code>" in plain, plain)
    check("var(--orangish)" in plain and "<i>world</i>" in plain, plain)
    check(">x</code>" in core.markdown_html("use ``x``"), "double backticks")
    check("<i>" not in core.markdown_html("use some_var_name and *args"), "names stay literal")
    linked = core.markdown_html("See [docs](https://example.com/a).")
    check('href="https://example.com/a"' in linked and "<script>" not in core.markdown_html("<script>"), linked)
    stored = core.clip_doc("## Title\n\n- `item`\n\n```\nx < 1\n```\n")
    check(core.clip_doc("a" * 600) == "a" * 600, "docstrings are not truncated")
    rendered = core.markdown_html(stored)
    check("<b>Title</b>" in rendered and "<li>" in rendered and ">item</code>" in rendered, rendered)
    check("<pre" in rendered and "x &lt; 1" in rendered, rendered)


def check_markup_perl_matlab():
    css = list(markup_src.css_records())
    check(symbols(css, "display", "css"), "css display")
    check(symbols(css, "flex", "display"), "css display value")
    check(core.css_value("    display: fl") == ("display", "fl"), "css value")
    check(core.css_value("color: red; float: ") == ("float", ""), "css value after semicolon")
    check(core.css_value("div span") is None, "css selector is not a value")
    html = list(markup_src.html_records())
    check(symbols(html, "div", "html"), "html div")
    check(symbols(html, "href", "a"), "html href")
    check(symbols(html, "id", "global"), "html global id")
    check(core.html_context("<di") == ("tag", "di"), "html tag: %s" % (core.html_context("<di"),))
    check(core.html_context('<a hr') == ("attr", "a", "hr"), "html attr")
    check(core.html_context('<div class="x" id') == ("attr", "div", "id"), "html later attr")
    perl = list(perl_src.scan_source(open(
        os.path.join(ROOT, "tests", "fixtures", "perllib", "Widget.pm"),
        encoding="utf-8",
    ).read()))
    check(symbols(perl, "add", "Widget"), "perl add")
    check(not symbols(perl, "hidden", "Widget"), "perl pod sub leaked")
    check("name" in symbols(perl, "add", "Widget")[0]["signature"], "perl signature")
    roots = list(perl_src.scan_roots([os.path.join(ROOT, "tests", "fixtures", "perllib")]))
    check(symbols(roots, "print", "perl"), "perl builtin")
    area = list(matlab_src.scan_source(open(
        os.path.join(ROOT, "tests", "fixtures", "matlab", "area.m"),
        encoding="utf-8",
    ).read()))
    check(symbols(area, "area", "matlab"), "matlab area")
    check(not symbols(area, "local", "matlab"), "matlab local function leaked: %s" % area)
    check("width" in symbols(area, "area", "matlab")[0]["signature"], "matlab signature")
    widget = list(matlab_src.scan_source(open(
        os.path.join(ROOT, "tests", "fixtures", "matlab", "Widget.m"),
        encoding="utf-8",
    ).read()))
    check(symbols(widget, "rename", "Widget"), "matlab method")
    check(core.import_typed("perl", "use List::Ut") == "List::Ut", "perl use")
    directory = tempfile.mkdtemp(prefix="intellekt-check-")
    try:
        db_path = os.path.join(directory, "index.sqlite")
        core.write_language(db_path, "perl", "fixture", "1", [
            {"t": "m", "name": "Widget", "doc": ""},
            {"t": "s", "owner": "Widget", "name": "add", "kind": "function",
             "signature": "add()", "doc": ""},
            {"t": "s", "owner": "perl", "name": "print", "kind": "function",
             "signature": "print()", "doc": ""},
        ])
        source = "use List::Util qw(max);\n$box = Widget->new;\n"
        check(
            core.resolve_owner(db_path, "perl", source, "$box") == "Widget",
            "perl method receiver",
        )
        names = [row[0] for row in core.bare_names(db_path, "perl", source, "ma")]
        check(names == ["max"], "perl qw import: %s" % names)
        core.write_language(db_path, "matlab", "fixture", "1", widget)
        box = "box = Widget(name);\n"
        check(
            core.resolve_owner(db_path, "matlab", box, "box") == "Widget",
            "matlab constructor",
        )
        members = [row[0] for row in core.query_symbols(db_path, "matlab", "Widget", "re")]
        check(members == ["rename"], "matlab members: %s" % members)
    finally:
        shutil.rmtree(directory)


def main():
    check_markdown()
    check_cache()
    check_resolve()
    check_java_scan()
    check_python_indexer()
    check_ruby_indexer()
    check_ts_scan()
    check_cs_scan()
    check_go_scan()
    check_rust_scan()
    check_cpp_scan()
    check_js_indexer()
    check_php_indexer()
    check_markup_perl_matlab()
    print("ok")


if __name__ == "__main__":
    main()
