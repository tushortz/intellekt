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
from indexers import java_src

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
        ])
        names = [row[0] for row in core.query_symbols(db_path, "python", "json", "du")]
        check(names == ["dump", "dump", "dumps"], "prefix query got %s" % names)
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
        check(core.import_typed("python", "import os.pa") == "os.pa", "python import path")
        check(core.import_typed("python", "from json import JSO") == "json.JSO", "from import path")
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
    check(put and "self" not in put[0]["signature"], "put signature: %s" % (put and put[0]["signature"]))
    check(any(rec["t"] == "m" and rec["name"] == "demo" and "Demo package" in rec["doc"] for rec in records),
          "demo module doc")


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


def main():
    check_cache()
    check_resolve()
    check_java_scan()
    check_python_indexer()
    check_ruby_indexer()
    print("ok")


if __name__ == "__main__":
    main()
