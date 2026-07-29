"""Resolve notification targets from persisted agent-session bindings."""

from __future__ import annotations

from .config import NotifierConfig, NotificationRoute
from .registry import SessionMapping, SessionRegistry


def agent_route_name(
    config: NotifierConfig, agent: str, explicit: str | None = None
) -> str:
    """Return an explicit route or the configured route for an agent kind."""
    if explicit:
        return explicit
    return config.agent_routes.get(agent.lower(), "default")


def _mapping_matches_route(mapping: SessionMapping, route: NotificationRoute) -> bool:
    external_key = mapping.external_key
    if route.session_key and (
        external_key == route.session_key
        or external_key.startswith(route.session_key.rstrip(":") + ":")
    ):
        return True
    # Terminal bindings use: feishu:<receive_id>:terminal:<thread_id>.
    parts = external_key.split(":", 3)
    return len(parts) >= 2 and parts[0] == "feishu" and parts[1] == route.receive_id


def resolve_thread_route_name(
    config: NotifierConfig,
    registry: SessionRegistry | None,
    thread_id: str | None,
    mapping: SessionMapping | None = None,
    *,
    fallback: str | None = None,
) -> str | None:
    """Resolve a thread route, preferring persisted binding over inference."""
    if registry is not None and thread_id:
        persisted = registry.get_thread_route(thread_id)
        if persisted and persisted.route_name in config.notification_routes:
            return persisted.route_name
    if mapping is not None:
        for name, route in config.notification_routes.items():
            if _mapping_matches_route(mapping, route):
                return name
    return fallback
