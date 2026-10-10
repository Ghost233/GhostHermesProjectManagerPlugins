"""Scope artificial external APIs to the actual SDK-loaded plugin package."""
import importlib.abc
import importlib.machinery
from pathlib import Path
import sys


def install(plugin_root, callbacks, *, synthetic_readiness=True):
    callbacks = dict(callbacks)
    if synthetic_readiness:
        from readiness_support import FixtureReadinessHost
        callbacks.setdefault('readiness', lambda module: setattr(module, 'configured_readiness_host', lambda config, intake, resolver: FixtureReadinessHost()))
    root = Path(plugin_root).resolve()
    class Loader(importlib.machinery.SourceFileLoader):
        def exec_module(self, module):
            super().exec_module(module)
            callbacks[module.__name__.rsplit('.', 1)[-1]](module)
    class Boundary(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if '.ghost_hermes_pm.' not in fullname or fullname.rsplit('.', 1)[-1] not in callbacks:
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and spec.origin and Path(spec.origin).resolve().is_relative_to(root):
                spec.loader = Loader(fullname, spec.origin)
                return spec
            return None
    finder = Boundary()
    sys.meta_path.insert(0, finder)
    return finder
