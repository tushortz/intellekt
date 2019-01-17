import importlib
import inspect
import json
import os
import re
import site
import sys

from . import helpers

TEMPLATE = "{0} {1}\t{2} "


def get_module_members(module, package=''):
    all_members = []

    try:
        imported = importlib.import_module(module)
        module = str(module)

        if package and package != "*":
            imported = getattr(imported, package)
            module = module + "." + package

        all_members = []
        members = inspect.getmembers(imported)

        if members:
            for member in members:
                function = member[0]

                if not function.startswith("__"):
                    try:
                        args = str(inspect.signature(member[1]))
                        args = re.sub(
                            r'(?:, /|[\/])|(?:self[ ,]+)', "", args)
                    except Exception:
                        args = ""

                    if function.isupper():
                        symbol = "▢"
                    elif function[0].isupper() and function[1].islower():
                        symbol = "◼"
                    else:
                        symbol = "➛"

                    completions = TEMPLATE.format(symbol, function, module)

                    all_members.append([
                        completions,
                        helpers.placeholder_format(function + args)
                    ])
    except Exception as e:
        print(e)

    return all_members


def get_imports_from_view(view_text):
    imports = sorted(set(re.findall(
        r"from ([\w\.]+) import ([\w \,\*]+)|import ([\w\. \,]+)", view_text)))

    filtered_imports = []

    for i in imports:
        if i[0] and i[1] and (not i[2]):
            j = re.sub(r'as \w+', "", i[1])
            j = re.findall(r'[\w\.]+', j)

            for k in j:
                if not [i[0], k] in filtered_imports:
                    filtered_imports.append([i[0], k])

        if (not i[0]) and (not i[1]) and i[2]:
            j = re.sub(r' as \w+', "", i[2])
            j = re.findall(r'[\w\.]+', j)

            for k in j:
                if not [i[0], k] in filtered_imports:
                    filtered_imports.append([k, i[0]])

        if i[0] and i[1] == "*":
            filtered_imports.append([i[0], ""])

    return filtered_imports


def load_sublime_events(sublime):
    all_views = sublime.active_window().views()

    if len(all_views) > 0:
        intellekt_settings = all_views[0].settings().get("intellekt")
        if intellekt_settings:
            try:
                if type(intellekt_settings) == dict:
                    python_path = intellekt_settings.get("python_path")

                    if not os.path.exists(python_path):
                        sublime.status_message(
                            "Intellekt Error: '" + python_path + "' is an invalid python path")

                    if python_path:
                        site_packages = site.getsitepackages([python_path])

                        for package in site_packages:
                            if not package in sys.path:
                                sys.path.append(package)
                else:
                    sublime.status_message(
                        "Intellekt Error: Please use a dictionary as your 'intellekt' settings value")
            except Exception as err:
                sublime.status_message("Intellekt Error: " + str(err))


def get_documentation(module, package=""):
    imported = importlib.import_module(module)

    if package and package != "*":
        try:
            imported = getattr(imported, package)
        except:
            pass
        module = module + "." + package

    doc = imported.__doc__ or "no doc found!"

    doc = doc.encode('ascii', 'ignore').decode("utf-8")
    data = r"<strong>{0}</strong><br><br>{1}".format(
        module, doc.replace("\n", "<br>"))
    return data


import pkgutil


def get_modules(path=None):
    imports = []

    for importer, name, ispkg in pkgutil.walk_packages(path, onerror=lambda x: None):
        imports.append(name)

    return imports


def suggest_import():
    imports = ""
    paths = []
    for x in sys.path:
        if x.lower().strip("\\").endswith("lib") or x.lower().strip("\\").endswith("site-packages"):
            paths.append(x)

    try:
        suggestions = get_modules(paths[:1])
    except:
        suggestions = []

    return suggestions
