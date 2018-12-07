import re
import os

def get_data_path(filename):
    PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_PATH = os.path.join(PROJECT_DIR, "data", filename)
    return DATA_PATH

