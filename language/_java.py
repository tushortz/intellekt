import html
import json
import os
import re
from urllib import request

from . import helpers

JAVASE_CONTENTS = helpers.get_content_to_json("javase.json")
JAVAFX_CONTENTS = helpers.get_content_to_json("javafx.json")


def _get_methods(json_data, package_name, class_name):
    methods = []
    for content in json_data:
        p = content.get("p")
        c = content.get("c")
        l = content.get("l")

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


def get_methods(package_name, class_name="*"):
    data = _get_methods(JAVASE_CONTENTS, package_name, class_name)

    if not data:
        data = _get_methods(JAVAFX_CONTENTS, package_name, class_name)

    return data


def get_imports_from_view(view_text):
    imports = sorted(set(re.findall(
        r"((?:java|javax|javafx|com|org)\.[a-z0-9\_\.]+)\.([_A-Z\*][\w_\.]*)", view_text)))

    specials = {}

    for i in imports:
        if i[1] == "*":
            specials[i[0]] = i[1]

    filtered_imports = []
    for i in imports:
        if i[0] not in specials or i[1] == "*":
            filtered_imports.append(i)

    return filtered_imports


def suggest_import(line_text):
    imports = re.search(
        r"import ((java|javax|javafx|com|org)\.[\w\.]+)", line_text, re.I)

    if not imports:
        return

    imports = imports.group(1)

    suggestion = []
    suggestions = []

    for content in JAVASE_CONTENTS + JAVAFX_CONTENTS:
        p = content.get("p")
        c = content.get("c")
        import_text = "{0}.{1}".format(p, c)

        if re.match(imports, import_text):
            suggestions.append(
                "<a href='{0}'>{1}</a>".format(get_url(import_text), import_text))

    suggestions = sorted(set(suggestions))
    suggestions = "<br>".join(suggestions)
    return suggestions


def get_url(text):
    url_prefix = "https://docs.oracle.com/javase/10/docs/api/{0}.html"

    if text.endswith("*") or text.endswith("."):
        url_prefix = "https://docs.oracle.com/javase/10/docs/api/{0}/package-summary.html"
        text = text.rsplit(".", 1)[0]

    url = text.replace(".", "/")
    url = url_prefix.format(url)
    return url


def get_documentation(line_text):
    imports = re.search(
        r"import ((java|javax|javafx|com|org)\.[\w\.]+)", line_text, re.I)

    if not imports:
        return

    imports = imports.group(1)

    url = get_url(imports)
    r = request.urlopen(url).read().decode("utf-8")

    data = re.search(r'<div class="block">(.*?)(?:</div|h2)>', r, re.DOTALL)

    if not data:
        return

    if data:
        data = data.group(1).strip(".")

    data = data.encode('ascii', 'ignore').decode("utf-8")
    data = "<strong>{0}</strong><br><br>{1}".format(imports, data)
    return data
