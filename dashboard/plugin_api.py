"""Load only the observational component; never run connector registration."""
import importlib.util
import importlib
from pathlib import Path
import sys

_root = Path(__file__).resolve().parents[1] / "fleet_connector"
_package = "hermes_fleet_observation_component"
if _package not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _package, _root / "__init__.py", submodule_search_locations=[str(_root)])
    _namespace = importlib.util.module_from_spec(_spec)
    sys.modules[_package] = _namespace
    _spec.loader.exec_module(_namespace)
_module = importlib.import_module(f"{_package}.reporting")
router = _module.router
