import json
import os
from dataclasses import dataclass
from urllib import error, request
from urllib.parse import quote

from Nerzilus.models import User


@dataclass
class WhatsAppNotificationResult:
    attempted: bool
    delivered: bool
    target_number: str | None = None
    error_message: str | None = None
    direct_link: str | None = None
    message: str | None = None


def normalize_phone(value):
    if not value:
        return None
    digits = "".join(char for char in str(value) if char.isdigit())
    return digits or None


def format_phone_display(value):
    normalized = normalize_phone(value)
    if not normalized:
        return ""

    if normalized.startswith("55") and len(normalized) >= 12:
        country = normalized[:2]
        area = normalized[2:4]
        number = normalized[4:]
        if len(number) == 9:
            return f"+{country} ({area}) {number[:5]}-{number[5:]}"
        if len(number) == 8:
            return f"+{country} ({area}) {number[:4]}-{number[4:]}"

    if len(normalized) == 11:
        return f"({normalized[:2]}) {normalized[2:7]}-{normalized[7:]}"
    if len(normalized) == 10:
        return f"({normalized[:2]}) {normalized[2:6]}-{normalized[6:]}"
    return normalized


def build_whatsapp_link(value):
    normalized = normalize_phone(value)
    if not normalized:
        return None
    return f"https://wa.me/{normalized}"


def build_whatsapp_message_link(value, message):
    normalized = normalize_phone(value)
    if not normalized or not message:
        return None
    return f"https://wa.me/{normalized}?text={quote(message)}"


def resolve_admin_whatsapp(tenant):
    if tenant.whatsapp:
        return normalize_phone(tenant.whatsapp)

    admin = User.query.filter_by(tenant_id=tenant.id, is_admin=True).order_by(User.id.asc()).first()
    if admin is None:
        return None
    return normalize_phone(admin.telefone)


def build_booking_message(appointment):
    tenant = appointment.tenant
    client = appointment.cliente
    barber = appointment.barbeiro_rel
    service = appointment.servico_rel
    payment_label = "Pix pelo app" if appointment.forma_pagamento == "pix" else "Pagar no local"
    client_phone = format_phone_display(client.telefone) or client.telefone

    return (
        "\u2705 *Novo agendamento confirmado*\n\n"
        "------------------------------\n"
        f"\U0001F3E2 *Barbearia:* {tenant.nome}\n"
        f"\U0001F464 *Cliente:* {client.nome}\n"
        f"\U0001F4F1 *WhatsApp:* {client_phone}\n\n"
        f"\U0001F488 *Barbeiro:* {barber.nome}\n"
        f"\u2702\uFE0F *Servico:* {service.nome}\n"
        f"\U0001F4B3 *Pagamento:* {payment_label}\n\n"
        f"\U0001F4C5 *Data:* {appointment.data_agendamento.strftime('%d/%m/%Y')}\n"
        f"\u23F0 *Hora:* {appointment.hora_agendamento.strftime('%H:%M')}\n"
        "------------------------------\n"
        "\U0001F4CC Confira os detalhes e prepare o atendimento."
    )


def send_booking_whatsapp_notification(appointment):
    target_number = resolve_admin_whatsapp(appointment.tenant)
    message = build_booking_message(appointment)
    direct_link = build_whatsapp_message_link(target_number, message)
    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    api_version = os.getenv("WHATSAPP_API_VERSION", "v21.0")

    if not target_number or not access_token or not phone_number_id:
        return WhatsAppNotificationResult(
            attempted=False,
            delivered=False,
            target_number=target_number,
            error_message="WhatsApp nao configurado.",
            direct_link=direct_link,
            message=message,
        )

    payload = {
        "messaging_product": "whatsapp",
        "to": target_number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": message,
        },
    }
    endpoint = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    http_request = request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with request.urlopen(http_request, timeout=10) as response:
            response.read()
        return WhatsAppNotificationResult(
            attempted=True,
            delivered=True,
            target_number=target_number,
            direct_link=direct_link,
            message=message,
        )
    except (error.HTTPError, error.URLError, TimeoutError, ValueError) as exc:
        return WhatsAppNotificationResult(
            attempted=True,
            delivered=False,
            target_number=target_number,
            error_message=str(exc),
            direct_link=direct_link,
            message=message,
        )
