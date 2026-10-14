"""Independently imported by the enabled user-plugin Dashboard loader."""
import importlib.util
from pathlib import Path
import sys

if 'ghost_hermes_pm' not in sys.modules:
    package = Path(__file__).resolve().parent.parent / 'ghost_hermes_pm'
    spec = importlib.util.spec_from_file_location('ghost_hermes_pm', package / '__init__.py',
                                                 submodule_search_locations=[str(package)])
    module = importlib.util.module_from_spec(spec)
    sys.modules['ghost_hermes_pm'] = module
    spec.loader.exec_module(module)

from ghost_hermes_pm.dashboard import create_router
from ghost_hermes_pm.native import NativeDashboardEntry

router = create_router(NativeDashboardEntry())
