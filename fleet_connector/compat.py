"""Capability checks for the Hermes APIs used by the connector."""
from __future__ import annotations

from typing import Any

REQUIRED_CONTEXT_METHODS = (
    "register_platform_handler",
    "register_dashboard_auth_provider",
)


def require_hermes_apis(ctx: Any | None = None) -> None:
    """Fail closed unless the host exposes the plugin APIs this connector uses.

    When called by the plugin loader, inspect the supplied public context instance.
    The setup/service preflight has no context instance, so it checks the public
    ``PluginContext`` class before starting or configuring anything.
    """
    if ctx is None:
        try:
            from hermes_cli.plugins import PluginContext as context_type
        except (ImportError, AttributeError) as exc:
            raise RuntimeError("Hermes PluginContext is unavailable") from exc
        context = context_type
    else:
        context = ctx

    missing = [name for name in REQUIRED_CONTEXT_METHODS
               if not callable(getattr(context, name, None))]
    if missing:
        names = ", ".join(f"PluginContext.{name}" for name in missing)
        raise RuntimeError(f"Hermes is missing required plugin capabilities: {names}")

    try:
        from hermes_constants import get_hermes_home
        from hermes_cli.plugin_isolation import isolation_mode
    except (ImportError, AttributeError) as exc:
        raise RuntimeError("Hermes home or plugin isolation API is unavailable") from exc
    if not callable(get_hermes_home) or not callable(isolation_mode):
        raise RuntimeError("Hermes home or plugin isolation API is unavailable")
