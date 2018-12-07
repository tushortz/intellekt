import os
from . import helpers
import json

CONTENTS = helpers.get_content_to_json("java.json")


def get_methods(package_name, class_name="*"):
    methods = []
    for content in CONTENTS:
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

