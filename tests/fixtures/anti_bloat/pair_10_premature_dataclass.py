"""Verbose: dataclass with one field, used in exactly one place."""
from dataclasses import dataclass


@dataclass
class TimeoutConfig:
    seconds: int


def make_timeout() -> TimeoutConfig:
    return TimeoutConfig(seconds=30)


def call_api() -> str:
    config = make_timeout()
    return f"calling api with timeout={config.seconds}s"
