import pytest

# We must clear the registry before collection because `register_handler` is called at import-time.
def pytest_configure(config):
    import dari_mcp_vps.tools.action_dispatcher as ad
    ad._REGISTRY.clear()
    original_register = ad.register_handler
    def safe_register(action_name, handler):
        ad._REGISTRY[action_name] = handler
    ad.register_handler = safe_register

@pytest.fixture(autouse=True, scope="function")
def isolated_action_dispatcher():
    import dari_mcp_vps.tools.action_dispatcher as ad
    original_registry = ad._REGISTRY.copy()
    yield
    ad._REGISTRY.clear()
    ad._REGISTRY.update(original_registry)
