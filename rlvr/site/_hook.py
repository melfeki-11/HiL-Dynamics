"""after_import(module_name, fn): run fn(module) right after `module_name` is first imported (or now, if it is)."""
from __future__ import annotations

import importlib.abc
import importlib.util
import sys


def after_import(target: str, fn) -> None:
    if target in sys.modules:
        fn(sys.modules[target])
        return

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target_=None):
            if name != target:
                return None
            sys.meta_path.remove(self)
            try:
                spec = importlib.util.find_spec(name)
            finally:
                sys.meta_path.insert(0, self)
            if spec is None or spec.loader is None:
                return spec
            loader = spec.loader

            class _Loader(importlib.abc.Loader):
                def create_module(self, s):
                    return loader.create_module(s)

                def exec_module(self, module):
                    loader.exec_module(module)
                    try:
                        fn(module)
                    except Exception as exc:  # never break imports
                        print(f"[rlvr] patch of {target} failed: {exc}", flush=True)

            spec.loader = _Loader()
            return spec

    sys.meta_path.insert(0, _Finder())
