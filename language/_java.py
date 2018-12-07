import os
from . import helpers
import json
import re
CONTENTS = helpers.get_content_to_json("java.json")


def get_methods(package_name, class_name="*"):
    methods = []
    for content in CONTENTS:
        p = content.get("p")
        c = content.get("c")
        l = content.get("l")
        print(p)
        if l.isupper():
            symbol = "▢"
        elif l[0].isupper() and l[1].islower():
            symbol = "◼"
        else:
            symbol = "➛"

        if p == package_name and c == class_name:
            left_display = "{0} {1}\t{2}".format(symbol, l, c)
            methods.append([left_display, helpers.placeholder_format(l)])

        elif p == package_name and class_name == "*":
            left_display = "{0} {1}\t{2}".format(symbol, l, c)
            methods.append([left_display, helpers.placeholder_format(l)])

    return methods


def get_imports_from_view(view_text):
    imports = sorted(set(re.findall(r"import ((?:java|javax|javafx|com|org)\.[a-z0-9\_\.]+)\.([_A-Z\*][\w_\.]*);", view_text)))
    specials = {}

    for i in imports:
        if i[1] == "*":
            specials[i[0]] = i[1]

    filtered_imports = []
    for i in imports:
        if i[0] not in specials or i[1] == "*":
            filtered_imports.append(i)

    return filtered_imports