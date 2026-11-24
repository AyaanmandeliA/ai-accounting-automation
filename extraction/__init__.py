"""Extraction package entry points."""

from __future__ import annotations

import importlib
import sys
import types


def _install_anthropic_stub() -> None:
    if "anthropic" in sys.modules:
        return
    stub = types.ModuleType("anthropic")

    class Anthropic:
        def __init__(self, *args, **kwargs):
            self.messages = None

    stub.Anthropic = Anthropic
    sys.modules["anthropic"] = stub


def _load_ai_extractor():
    try:
        return importlib.import_module(".ai_extractor", __name__)
    except ModuleNotFoundError as exc:
        if exc.name != "anthropic":
            raise
        _install_anthropic_stub()
        return importlib.import_module(".ai_extractor", __name__)


def __getattr__(name: str):
    if name == "ai_extractor":
        module = _load_ai_extractor()
        globals()[name] = module
        return module
    raise AttributeError(name)
