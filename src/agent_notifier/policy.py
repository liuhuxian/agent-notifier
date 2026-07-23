"""Pure routing policy helpers."""


def should_send_completion_notification(origin: str) -> bool:
    """Return whether a turn completion needs a separate chat notification."""
    return origin not in {"cc_connect", "agent-notifier-bootstrap"}
