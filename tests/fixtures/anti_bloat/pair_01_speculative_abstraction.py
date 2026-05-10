"""Verbose: speculative abstraction smell.

A BaseClass + AbstractFactory + SingletonRegistry for what is functionally
a single function that doubles a number. Mirrors the kind of over-engineering
LLMs emit when asked to "design extensibly".
"""
from abc import ABC, abstractmethod


class BaseDoubler(ABC):
    @abstractmethod
    def double(self, x: int) -> int:
        ...


class IntegerDoubler(BaseDoubler):
    def double(self, x: int) -> int:
        return x * 2


class DoublerFactory:
    @staticmethod
    def create(kind: str = "integer") -> BaseDoubler:
        if kind == "integer":
            return IntegerDoubler()
        raise ValueError(f"unknown kind: {kind}")


class DoublerRegistry:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._doubler = DoublerFactory.create("integer")
        return cls._instance

    def double(self, x: int) -> int:
        return self._doubler.double(x)


def double(x: int) -> int:
    return DoublerRegistry().double(x)
