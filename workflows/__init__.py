"""Local workflow helpers for accounting automation."""

from __future__ import annotations

import importlib


def __getattr__(name: str):
    if name == "single_document":
        module = importlib.import_module(".single_document", __name__)
        globals()[name] = module
        return module
    raise AttributeError(name)
