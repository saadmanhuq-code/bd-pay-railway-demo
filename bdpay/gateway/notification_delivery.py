"""Notification delivery adapters for the assembled gateway app."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from bdpay.connectors.mfs.sms import SmsOtpConnector, make_template
from bdpay.platform.errors import BDPayError
from bdpay.platform.interfaces import NotificationDeliveryResult

__all__ = [
    "NotificationRecipientDirectory",
    "PostgresNotificationRecipientDirectory",
    "SmsConnectorNotificationPort",
    "platform_rendered_sms_templates",
]

ConnectionFactory = Callable[[], Any]

_PLATFORM_RENDERED_TEMPLATE = make_template(
    "platform_rendered",
    body_en="{body}",
    body_bn="{body}",
)


@runtime_checkable
class NotificationRecipientDirectory(Protocol):
    """Resolve opaque notification recipients inside the transport boundary."""

    def sms_recipient(self, *, recipient_type: str, recipient_id: str) -> str | None: ...


class PostgresNotificationRecipientDirectory:
    """Postgres recipient resolver for notification transport adapters.

    The platform queue stores opaque recipient ids only. Today, the assembled
    app can resolve merchant SMS from KYB records. Customer/operator contacts
    and sandbox email stay unresolved until their secret-backed directories
    exist.
    """

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    def sms_recipient(self, *, recipient_type: str, recipient_id: str) -> str | None:
        if recipient_type != "merchant":
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT contact_phone_e164
                FROM kyb_records
                WHERE subject_type = 'MERCHANT'
                  AND subject_id = %s
                  AND contact_phone_e164 IS NOT NULL
                  AND contact_phone_e164 <> ''
                ORDER BY created_at DESC, kyb_record_id DESC
                LIMIT 1
                """,
                (recipient_id,),
            ).fetchone()
        return None if row is None else str(row[0])


def platform_rendered_sms_templates():
    """Templates used by the platform-queue-to-SMS adapter."""
    return {_PLATFORM_RENDERED_TEMPLATE.name: _PLATFORM_RENDERED_TEMPLATE}


class SmsConnectorNotificationPort:
    """NotificationPort adapter backed by the spec/12 SMS connector."""

    def __init__(
        self,
        *,
        directory: NotificationRecipientDirectory,
        connector: SmsOtpConnector,
    ) -> None:
        self._directory = directory
        self._connector = connector

    async def send(
        self,
        *,
        channel: str,
        recipient_ref: str,
        template_id: str,
        body: str,
        connector_ref: str,
    ) -> NotificationDeliveryResult:
        if channel != "sms":
            return NotificationDeliveryResult(
                delivered=False,
                error_code="notification_channel_not_wired",
            )
        recipient = self._directory.sms_recipient(
            recipient_type=_recipient_type_from_ref(recipient_ref),
            recipient_id=recipient_ref,
        )
        if recipient is None:
            return NotificationDeliveryResult(
                delivered=False,
                error_code="notification_recipient_unresolved",
            )
        try:
            row = await self._connector.send_notification(
                event_id=connector_ref,
                template_name=_PLATFORM_RENDERED_TEMPLATE.name,
                locale="en",
                recipient=recipient,
                variables={"body": body},
            )
        except BDPayError as exc:
            return NotificationDeliveryResult(delivered=False, error_code=exc.code)
        except Exception:
            return NotificationDeliveryResult(
                delivered=False,
                error_code="notification_connector_failure",
            )
        delivered = row.state in {"SENT", "DELIVERED", "SUPPRESSED"}
        return NotificationDeliveryResult(
            delivered=delivered,
            provider_message_id=row.provider_message_id,
            error_code=None if delivered else row.failure_code or "notification_failed",
        )


def _recipient_type_from_ref(recipient_ref: str) -> str:
    if recipient_ref.startswith("cust_"):
        return "customer"
    if recipient_ref.startswith("mrch_") or recipient_ref.startswith("sbxs_"):
        return "merchant"
    if recipient_ref.startswith("oper_"):
        return "operator"
    return "unknown"
