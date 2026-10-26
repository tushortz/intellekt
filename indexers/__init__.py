"""Indexers.

Java, TypeScript, C#, Go, Rust, C++, Perl, MATLAB, CSS, and HTML run
inside the plugin host. Python, Ruby, JavaScript, and PHP are scripts
executed by the installed interpreter.
"""

import os

# Directories that are tests or generated noise in every toolchain we walk.
SKIP_DIRS = {"test", "tests", "testing", "testdata", "node_modules"}


def walk_files(roots, suffixes, limit, skip_dirs=None, allow_plain=False):
    """Yield source files under ``roots`` until ``limit`` files have been seen.

    NOTE: the walk stops after ``limit`` files, so a large module cache or
    SDK is only partly indexed. Upgrade path: index one dependency at a time.
    """
    skipped = SKIP_DIRS if skip_dirs is None else skip_dirs
    plain_skip = {"LICENSE", "README", "COPYING", "Makefile", "module.modulemap"}
    seen = 0
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                name for name in dirnames
                if name not in skipped and not name.startswith(".")
            ]
            for filename in filenames:
                if filename.endswith(suffixes):
                    wanted = True
                elif allow_plain and "." not in filename and filename not in plain_skip:
                    wanted = True
                else:
                    wanted = False
                if not wanted:
                    continue
                if seen >= limit:
                    return
                seen += 1
                yield os.path.join(dirpath, filename)


def merge_modules(records):
    """Yield symbols immediately. Yield each module once, keeping a real doc.

    A later empty doc must not wipe the summary already seen for that name.
    NOTE: module names are held until ``records`` is exhausted. Upgrade path:
    an insert that refuses to replace a non-empty doc.
    """
    modules = {}
    for rec in records:
        if rec.get("t") != "m":
            yield rec
            continue
        name = rec.get("name") or ""
        if not name:
            continue
        doc = rec.get("doc") or ""
        if name not in modules or (doc and not modules[name]):
            modules[name] = doc
    for name in sorted(modules):
        yield {"t": "m", "name": name, "doc": modules[name]}


def read_text(path, limit=800_000):
    """Return the file as text, or ``""`` if it is missing, huge, or binary."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    if size <= 0 or size > limit:
        return ""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return ""
    if b"\0" in raw[:512]:
        return ""
    return raw.decode("utf-8", "replace")
