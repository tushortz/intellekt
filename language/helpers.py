import json
import os
import re


def get_data_path(filename):
    PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_PATH = os.path.join(PROJECT_DIR, "data", filename)
    return DATA_PATH


def get_content_to_json(filename):
    file_path = get_data_path(filename)

    data = []
    with open(file_path) as f:
        data = json.load(f)

    return data


def placeholder_format(string):
    if "(" in string:
        signature, args = string.split("(", 1)
        args = args.rsplit(")", 1)[0]
        args = args.split(",")

        for index, arg in enumerate(args, 1):
            args[index-1] = "${%s:%s}" % (index, arg)

        formatted = "{0}({1})".format(signature, ",".join(args))
        return formatted

    return string
