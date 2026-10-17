"""Keep reporting contract tests independent of Hermes entry-point provisioning."""
import sys
import types

import pytest


@pytest.fixture(autouse=True)
def reporting_library_imports(request, monkeypatch):
    if request.module.__name__.startswith("test_reporting"):
        # tui_gateway.slash_worker imports the executable launcher solely for
        # path hardening. The source root is already explicit on PYTHONPATH here.
        # Do not let importing a read-only registry test provision/swap a Python
        # environment or continue an updater. This is not a launcher acceptance test.
        bootstrap = types.ModuleType("hermes_bootstrap")
        bootstrap.harden_import_path = lambda *args, **kwargs: None
        def no_provider_connection(*args, **kwargs):
            pytest.fail("Reporting must not open provider connections")
        bootstrap._happy_eyeballs_create_connection = no_provider_connection
        monkeypatch.setitem(sys.modules, "hermes_bootstrap", bootstrap)
