from __future__ import annotations

import importlib


def __getattr__(name: str):
    if name in {"qbo_poster", "dry_run_builders"}:
        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(name)
