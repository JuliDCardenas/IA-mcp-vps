import sys
import pytest

def pytest_configure(config):
    import dari_mcp_vps.tools.action_dispatcher as ad
    ad._REGISTRY.clear()
    original_register = ad.register_handler
    def safe_register(action_name, handler):
        ad._REGISTRY[action_name] = handler
    ad.register_handler = safe_register
