"""Notification error types."""

from __future__ import annotations

__all__ = [
    "NotificationConfigurationError",
    "NotificationError",
    "NotificationSecurityError",
    "NotificationStoreError",
    "NotificationTransportError",
]


class NotificationError(RuntimeError):
    pass


class NotificationStoreError(NotificationError):
    pass


class NotificationConfigurationError(NotificationError):
    pass


class NotificationSecurityError(NotificationError):
    pass


class NotificationTransportError(NotificationError):
    pass
