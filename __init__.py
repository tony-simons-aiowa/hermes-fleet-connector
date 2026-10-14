"""Hermes plugin entry point; the service is activated separately by the owner."""
def register(ctx):
    from .fleet_connector.bridge import register as register_bridge
    register_bridge(ctx)
