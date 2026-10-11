from typing import Callable, Tuple, Dict, Any

# Signature for an action handler:
# def handler(parameters: Dict[str, Any], app_config: Any) -> Tuple[bool, Dict[str, Any]]:
# Returns: (success, diagnostic_info)
ActionHandler = Callable[[Dict[str, Any], Any], Tuple[bool, Dict[str, Any]]]

class IndeterminateStateError(Exception):
    """Raised when an operation fails after a point where partial execution might have occurred, making the outcome uncertain."""
    pass

_REGISTRY: Dict[str, ActionHandler] = {}

def register_handler(action_name: str, handler: ActionHandler) -> None:
    """Registers a handler for a specific tool action."""
    if action_name in _REGISTRY:
        raise ValueError(f"Handler for action '{action_name}' is already registered.")
    _REGISTRY[action_name] = handler

def get_handler(action_name: str) -> ActionHandler | None:
    """Retrieves the handler for a specific tool action, or None if not found."""
    return _REGISTRY.get(action_name)
