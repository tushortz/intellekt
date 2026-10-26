# Intellekt

Offline completions for Python, Java, and Ruby in Sublime Text 4.

The plugin reads the language already installed on your machine and builds a local index. Completions and hover text come from that index. It does not use the network, and it does not install Python packages into Sublime.

## Install

Copy this folder into Sublime's Packages directory, or install it from Package Control. Restart Sublime, or run **Tools → Developer → Reload Plugins** if the folder is already in place.

The first time a language is found, the status bar says `Intellekt: indexing …`. That pass runs in the background. After it finishes, completions are available in Python, Java, and Ruby files. You do not need to edit Preferences.

If a language is not installed, that language stays quiet. **Intellekt: Index Status** (Command Palette) says which interpreters were found.

## What gets indexed

* **Python.** The standard library and installed packages, read from source. Extension modules in site-packages are not imported. A short list of standard-library modules written in C (`math`, `sys`, `builtins`, and a few others) is imported so types such as `str` and `list` still complete.
* **Ruby.** The standard library and installed gems, read with `Ripper` so the gems are not loaded. Core classes such as `String` and `Array` come from the local RI documentation, when Ruby was installed with docs.
* **Java.** `lib/src.zip` from a JDK. A JRE has no sources, so it is not enough. Extra `.zip` source archives (JavaFX, for example) can be listed in the settings below.

The index is stored under Sublime's cache folder and rebuilt when the interpreter version changes. **Intellekt: Rebuild Index** rebuilds it on demand, for example after you install a package or a gem.

## Settings

Defaults live in `Intellekt.sublime-settings`. Leave them empty unless detection picks the wrong install. Sublime lists the file under Preferences → Package Settings → Intellekt.

```json
{
    "python_executable": "",
    "ruby_executable": "",
    "java_home": "",
    "java_sources": []
}
```

## How completion chooses a type

After a dot, the name to the left is resolved in this order: an import in the file, a simple assignment whose right-hand side is a type or module (`data = JSONDecoder()`, `ArrayList items`, `items = Widget.new`), then a builtin type (`str`, `String`, `Array`).

`items = ArrayList(); items.` completes only when that right-hand side is a type name written in the file. There is no type checker. Import lines complete package and module names as you type.

Hovering a known name shows the cached description.
