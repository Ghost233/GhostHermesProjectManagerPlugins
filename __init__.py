"""Hermes native directory-plugin entry."""


def register(ctx):
    # Keep implementation modules in the SDK-owned package so public force reload
    # evicts this plugin's code without touching other Profile/module namespaces.
    from .ghost_hermes_pm.native import register_native
    register_native(ctx)
