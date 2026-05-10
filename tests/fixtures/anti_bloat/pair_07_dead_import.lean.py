"""Lean: only what's used."""
import json


def load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)
