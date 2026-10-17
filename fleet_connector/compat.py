"""Capability checks for the Hermes APIs used by the connector."""
from __future__ import annotations

from typing import Any

REQUIRED_COMMAND_METHODS = (
    "register_command",
    "register_cli_command",
)
REQUIRED_NATIVE_METHODS = (
    "register_platform_handler",
    "register_dashboard_auth_provider",
)


def require_reporting_apis(server: Any, registry: Any) -> None:
    """Reporting is optional and must prove its live registry even when idle."""
    if (not isinstance(getattr(server, "_sessions", None), dict)
            or not hasattr(getattr(server, "_sessions_lock", None), "__enter__")
            or not callable(getattr(server, "_session_live_item", None))
            or not callable(getattr(registry, "list_active_subagents", None))):
        raise AttributeError("Live reporting registry capability unavailable")


def require_hermes_apis(ctx: Any | None = None, *, native: bool = True) -> None:
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

    methods = REQUIRED_COMMAND_METHODS + (REQUIRED_NATIVE_METHODS if native else ())
    missing = [name for name in methods
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
