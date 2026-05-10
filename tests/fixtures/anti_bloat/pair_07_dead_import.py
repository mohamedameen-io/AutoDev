"""Verbose: dead imports + commented-out alternatives."""
import os
import sys
import json
import re
from pathlib import Path
from collections import OrderedDict, defaultdict
from typing import Any, Dict, List, Optional, Tuple
# import yaml   # alternative serializer, kept for reference
# import toml   # in case we switch formats later
# from datetime import datetime  # may need for timestamps


def load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)
