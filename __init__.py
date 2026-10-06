"""Hermes native directory-plugin entry."""
import importlib.util
from pathlib import Path
import sys


def register(ctx):
    # Agent and Dashboard have different host module names; both load the bundled package.
    if 'ghost_hermes_pm' not in sys.modules:
        package = Path(__file__).parent / 'ghost_hermes_pm'
        spec = importlib.util.spec_from_file_location('ghost_hermes_pm', package / '__init__.py',
                                                     submodule_search_locations=[str(package)])
        module = importlib.util.module_from_spec(spec)
        sys.modules['ghost_hermes_pm'] = module
        spec.loader.exec_module(module)
    from ghost_hermes_pm.native import register_native
    register_native(ctx)
