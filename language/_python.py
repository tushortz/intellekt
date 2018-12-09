import inspect
import re
import importlib
from . import helpers

TEMPLATE = "{0} {1}\t{2}"


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