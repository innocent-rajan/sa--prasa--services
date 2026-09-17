"""Notification provider adapter.

The outbox is the queue; providers only know how to deliver one message.
StubProvider logs and succeeds. Real providers (FCM/APNs/WhatsApp) plug in
later without touching the outbox or fan-out logic.
"""
import logging

from app.config import NOTIFICATION_PROVIDER

log = logging.getLogger("prasa.notifications")


class NotificationProvider:
    """Interface: deliver one message on one channel."""

    def send(self, channel: str, recipient: str, title: str, body: str) -> None:
        raise NotImplementedError


class StubProvider(NotificationProvider):
    """Default provider: logs the message and reports success."""

    def send(self, channel: str, recipient: str, title: str, body: str) -> None:
        log.info("STUB SEND channel=%s recipient=%s title=%r", channel, recipient, title)


def get_provider() -> NotificationProvider:
    if NOTIFICATION_PROVIDER == "stub":
        return StubProvider()
    raise ValueError(f"Unknown NOTIFICATION_PROVIDER: {NOTIFICATION_PROVIDER!r}")