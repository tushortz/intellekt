#!/usr/bin/env node
"use strict";

// Index a Node install.
//
//   node js_index.js --out symbols.jsonl
//   node js_index.js --lib ./some/dir --out symbols.jsonl
//
// Built-in modules are required and their own properties are recorded.
// --lib only requires the .js files in that directory (the check script).
//
// NOTE: requiring a package runs it. Side effects stay in this process.
// Each package is capped, and only the first PACKAGE_CAP global packages
// are loaded. A project's node_modules is not indexed. Upgrade path: index
// the folder of the active view.

const fs = require("fs");
const path = require("path");
const childProcess = require("child_process");

const PACKAGE_CAP = 40;
const SKIP = new Set([
  "prototype", "constructor", "length", "name", "caller", "arguments",
  "callee", "__proto__", "toJSON",
]);

function parseArgs(argv) {
  const out = { out: "", lib: "" };
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === "--out") out.out = argv[++i] || "";
    else if (argv[i] === "--lib") out.lib = argv[++i] || "";
  }
  return out;
}

function snippet(name, fn) {
  let source = "";
  try {
    source = Function.prototype.toString.call(fn);
  } catch (err) {
    source = "";
  }
  const match = source.match(/^[^(]*\(([^)]*)\)/);
  if (!match || !match[1].trim() || source.indexOf("[native code]") !== -1) {
    return name + "()";
  }
  const labels = match[1].split(",").map((part) => part.trim()).filter(Boolean);
  if (!labels.length) return name + "()";
  const bits = labels.map((label, index) => "${" + (index + 1) + ":" + label.replace(/[${}]/g, "") + "}");
  return name + "(" + bits.join(", ") + ")";
}

function emit(stream, rec) {
  stream.write(JSON.stringify(rec) + "\n");
}

function walk(stream, value, owner, depth) {
  if (value == null || (typeof value !== "object" && typeof value !== "function")) return;
  emit(stream, { t: "m", name: owner, doc: "" });
  const lists = [value];
  if (typeof value === "function" && value.prototype) lists.push(value.prototype);
  lists.forEach((obj) => {
    let names = [];
    try {
      names = Object.getOwnPropertyNames(obj);
    } catch (err) {
      return;
    }
    names.forEach((name) => {
      if (SKIP.has(name) || !/^[A-Za-z_$]/.test(name)) return;
      // Read the descriptor. Calling a getter on a prototype (for example
      // ReadableStreamDefaultReader.closed) returns a rejected promise and
      // Node then exits on the unhandled rejection.
      let desc;
      try {
        desc = Object.getOwnPropertyDescriptor(obj, name);
      } catch (err) {
        return;
      }
      if (!desc || desc.get || desc.set) {
        if (desc) {
          emit(stream, {
            t: "s", owner: owner, name: name, kind: "variable",
            signature: name, doc: "", returns: "",
          });
        }
        return;
      }
      const prop = desc.value;
      const isFn = typeof prop === "function";
      const isType = isFn && /^[A-Z]/.test(name);
      emit(stream, {
        t: "s",
        owner: owner,
        name: name,
        kind: isType ? "type" : (isFn ? "function" : (prop && typeof prop === "object" ? "namespace" : "variable")),
        signature: isFn ? snippet(name, prop) : name,
        doc: "",
        returns: "",
      });
      if (depth < 1 && isType) {
        try {
          walk(stream, prop, owner + "." + name, depth + 1);
        } catch (err) {
          // One bad constructor should not drop the rest of the module.
        }
      }
    });
  });
}

function scanLib(stream, dir) {
  fs.readdirSync(dir).forEach((file) => {
    if (!file.endsWith(".js")) return;
    const owner = file.replace(/\.js$/, "");
    const loaded = load(path.resolve(dir, file));
    walk(stream, loaded, owner, 0);
  });
}

function globalPackages() {
  let root = "";
  try {
    root = childProcess.execFileSync("npm", ["root", "-g"], {
      encoding: "utf8",
      timeout: 10000,
    }).trim();
  } catch (err) {
    return [];
  }
  if (!root || !fs.existsSync(root)) return [];
  const found = [];
  fs.readdirSync(root).forEach((name) => {
    if (name.startsWith(".") || name === "@types") return;
    const full = path.join(root, name);
    if (name.startsWith("@")) {
      let nested = [];
      try {
        nested = fs.readdirSync(full);
      } catch (err) {
        return;
      }
      nested.forEach((child) => {
        if (!child.startsWith(".")) found.push(path.join(full, child));
      });
      return;
    }
    found.push(full);
  });
  return found.slice(0, PACKAGE_CAP);
}

function scanInstall(stream) {
  ["console", "Math", "Buffer", "process", "URL", "JSON", "TextEncoder", "TextDecoder"].forEach((name) => {
    if (global[name] == null) return;
    try {
      walk(stream, global[name], name, 0);
    } catch (err) {
      // One global that cannot be walked is skipped.
    }
  });
  const mods = (require("module").builtinModules || []);
  mods.forEach((name) => {
    if (name.startsWith("_") || name.startsWith("node:")) return;
    try {
      walk(stream, load(name), name, 0);
    } catch (err) {
      // A builtin that refuses to load is skipped.
    }
  });
  globalPackages().forEach((dir) => {
    const name = dir.split(path.sep).slice(-2).join("/");
    const owner = name.startsWith("@") ? name : path.basename(dir);
    try {
      walk(stream, load(dir), owner, 0);
    } catch (err) {
      // A package that throws on require is skipped.
    }
  });
}

function load(request) {
  try {
    return require(request);
  } catch (err) {
    return undefined;
  }
}

function main() {
  const args = parseArgs(process.argv);
  if (!args.out) {
    process.stderr.write("missing --out\n");
    process.exit(1);
  }
  // Required modules parse process.argv. Hide our flags before any require
  // that runs at scan time. Builtins are required below, not here.
  process.argv = process.argv.slice(0, 2);
  // A required package may call process.exit when it does not see its own
  // CLI, or reject from a getter. This process only writes the index.
  const realExit = process.exit;
  process.exit = function blocked() {};
  process.on("unhandledRejection", function () {});
  const stream = fs.createWriteStream(args.out);
  if (args.lib) scanLib(stream, args.lib);
  else scanInstall(stream);
  stream.end(function () {
    realExit(0);
  });
}

main();
