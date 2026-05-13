import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from urllib import error, parse, request

from flask import current_app

from Nerzilus import database
from Nerzilus.models import PaymentEventLog, Subscription, Tenant, UsageRecord, User


PLAN_KEY = "acesso_liberado"
TRIAL_DAYS = 7


class BillingConfigurationError(RuntimeError):
    pass


class BillingProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlanOption:
    key: str
    name: str
    description: str
    amount_brl: Decimal
    billing_interval: str


def utcnow():
    return datetime.now(timezone.utc)


def ensure_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_asaas_datetime(value):
    if not value:
        return None
    if len(value) == 10:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_asaas_api_key():
    return os.getenv("ASAAS_API_KEY", "").strip()


def get_asaas_base_url():
    environment = os.getenv("ASAAS_ENVIRONMENT", "sandbox").strip().lower()
    if environment == "production":
        return "https://api.asaas.com/v3"
    return "https://api-sandbox.asaas.com/v3"


def asaas_is_configured():
    return bool(get_asaas_api_key())


def get_plan_catalog():
    monthly_amount = Decimal(os.getenv("PLAN_ACCESS_LIBERADO_ACTIVE_MONTHLY_AMOUNT", "49.90"))
    yearly_amount = Decimal(os.getenv("PLAN_ACCESS_LIBERADO_ACTIVE_YEARLY_AMOUNT", "499.90"))
    return {
        "monthly": PlanOption(
            key=PLAN_KEY,
            name="Acesso Liberado",
            description="Plano unico com acesso completo ao ambiente administrativo.",
            amount_brl=monthly_amount,
            billing_interval="monthly",
        ),
        "yearly": PlanOption(
            key=PLAN_KEY,
            name="Acesso Liberado",
            description="Plano anual com renovacao automatica e economia no ciclo.",
            amount_brl=yearly_amount,
            billing_interval="yearly",
        ),
    }


def get_launch_plan_amount(option):
    return option.amount_brl


def get_primary_subscription(tenant_id):
    return (
        Subscription.query.filter_by(tenant_id=tenant_id)
        .order_by(Subscription.updated_at.desc(), Subscription.created_at.desc())
        .first()
    )


def subscription_allows_access(subscription):
    if subscription is None:
        return False
    if subscription.status not in {"active", "trialing"}:
        return False
    expires_at = ensure_utc(subscription.current_period_end or subscription.trial_end)
    return expires_at is None or expires_at >= utcnow()


def tenant_has_active_access(tenant):
    if tenant_has_permanent_test_access(tenant):
        return True
    return subscription_allows_access(get_primary_subscription(tenant.id))


def tenant_has_permanent_test_access(tenant):
    if tenant is None:
        return False
    default_tenant_slug = os.getenv("DEFAULT_TENANT_SLUG", "nerzilus-studio")
    return tenant.slug == default_tenant_slug


def user_has_permanent_admin_access(user):
    if user is None or not getattr(user, "is_admin", False):
        return False
    admin_username = os.getenv("ADMIN_USERNAME", "sergioadmin").lower()
    admin_email = os.getenv("ADMIN_EMAIL", "sergioadmin@nerzilus.local").lower()
    username = (getattr(user, "username", "") or "").lower()
    email = (getattr(user, "email", "") or "").lower()
    return username == admin_username or email == admin_email


def get_owner_user_for_tenant(tenant_id):
    if isinstance(tenant_id, Tenant):
        tenant_id = tenant_id.id
    return User.query.filter_by(tenant_id=tenant_id, is_admin=True).order_by(User.id.asc()).first()


def can_create_client(user):
    if user and tenant_has_permanent_test_access(user.tenant):
        return True
    subscription = get_primary_subscription(user.tenant_id)
    return subscription_allows_access(subscription)


def can_access_feature(user, feature):
    del feature
    if user and tenant_has_permanent_test_access(user.tenant):
        return True
    subscription = get_primary_subscription(user.tenant_id)
    return subscription_allows_access(subscription)


def record_usage(user_or_tenant_id, user_id_or_resource_type, resource_type=None, quantity=1):
    if resource_type is None:
        user = user_or_tenant_id
        tenant_id = user.tenant_id
        user_id = user.id
        resource_type = user_id_or_resource_type
    else:
        tenant_id = user_or_tenant_id
        user_id = user_id_or_resource_type

    usage = UsageRecord.query.filter_by(tenant_id=tenant_id, resource_type=resource_type).first()
    if usage is None:
        usage = UsageRecord(
            tenant_id=tenant_id,
            user_id=user_id,
            resource_type=resource_type,
            quantity=0,
        )
        database.session.add(usage)
    usage.user_id = user_id
    usage.quantity += quantity
    database.session.commit()
    return usage


def log_payment_event(event_type, *, tenant_id=None, user_id=None, stripe_event_id=None, external_event_id=None, payload=None, status="received"):
    if stripe_event_id:
        existing = PaymentEventLog.query.filter_by(stripe_event_id=stripe_event_id).first()
        if existing is not None:
            existing.status = status
            existing.payload = json.dumps(payload, ensure_ascii=True) if payload is not None else existing.payload
            if status == "processed":
                existing.processed_at = utcnow()
            database.session.commit()
            return existing
    if external_event_id:
        existing = PaymentEventLog.query.filter_by(external_event_id=external_event_id).first()
        if existing is not None:
            existing.status = status
            existing.payload = json.dumps(payload, ensure_ascii=True) if payload is not None else existing.payload
            if status == "processed":
                existing.processed_at = utcnow()
            database.session.commit()
            return existing

    event_log = PaymentEventLog(
        tenant_id=tenant_id,
        user_id=user_id,
        stripe_event_id=stripe_event_id,
        external_event_id=external_event_id,
        event_type=event_type,
        payload=json.dumps(payload, ensure_ascii=True) if payload is not None else None,
        status=status,
        processed_at=utcnow() if status == "processed" else None,
    )
    database.session.add(event_log)
    database.session.commit()
    return event_log


def ensure_trial_subscription(tenant, user):
    subscription = get_primary_subscription(tenant.id)
    if subscription is not None:
        return subscription

    trial_end = utcnow() + timedelta(days=TRIAL_DAYS)
    subscription = Subscription(
        tenant_id=tenant.id,
        user_id=user.id,
        stripe_customer_id=tenant.stripe_customer_id,
        asaas_customer_id=tenant.asaas_customer_id,
        plan=PLAN_KEY,
        billing_interval="monthly",
        billing_method="PIX",
        status="trialing",
        current_period_end=trial_end,
        trial_end=trial_end,
        next_due_date=trial_end,
    )
    database.session.add(subscription)
    database.session.commit()
    log_payment_event(
        "trial.subscription.created",
        tenant_id=tenant.id,
        user_id=user.id,
        payload={"trial_end": trial_end.isoformat()},
        status="processed",
    )
    return subscription


def get_app_base_url():
    configured = os.getenv("APP_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    if current_app:
        return current_app.config.get("SERVER_NAME", "")
    raise BillingConfigurationError("APP_BASE_URL nao configurada.")


def asaas_request(method, path, *, payload=None, query=None):
    api_key = get_asaas_api_key()
    if not api_key:
        raise BillingConfigurationError("ASAAS_API_KEY nao configurada.")

    url = f"{get_asaas_base_url()}{path}"
    if query:
        url = f"{url}?{parse.urlencode(query)}"

    body = None
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "access_token": api_key,
        "User-Agent": "NerzilusBilling/1.0",
    }
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")

    http_request = request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with request.urlopen(http_request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"errors": [{"description": raw or f"HTTP {exc.code}"}]}
        errors = payload.get("errors") or []
        message = ", ".join(item.get("description", "Erro no Asaas.") for item in errors) or f"HTTP {exc.code}"
        raise BillingProviderError(message) from exc
    except error.URLError as exc:
        raise BillingProviderError(f"Falha de conexao com o Asaas: {exc.reason}") from exc


def ensure_asaas_customer(user, tenant):
    if tenant.asaas_customer_id:
        try:
            customer = asaas_request("GET", f"/customers/{tenant.asaas_customer_id}")
            if customer.get("id"):
                return tenant.asaas_customer_id
        except BillingProviderError:
            tenant.asaas_customer_id = None
            database.session.commit()

    customer_payload = {
        "name": tenant.nome,
        "email": user.email or f"{tenant.slug}@nerzilus.local",
        "mobilePhone": user.telefone,
        "cpfCnpj": user.cpf_cnpj,
        "externalReference": f"tenant:{tenant.id}",
        "notificationDisabled": False,
    }
    customer = asaas_request("POST", "/customers", payload=customer_payload)
    tenant.asaas_customer_id = customer["id"]
    database.session.commit()
    log_payment_event(
        "asaas.customer.created",
        tenant_id=tenant.id,
        user_id=user.id,
        payload={"asaas_customer_id": customer["id"]},
        status="processed",
    )
    return customer["id"]


def interval_to_cycle(billing_interval):
    if billing_interval == "monthly":
        return "MONTHLY"
    if billing_interval == "yearly":
        return "YEARLY"
    raise BillingConfigurationError("Periodo de cobranca invalido.")


def add_interval(value, billing_interval):
    if billing_interval == "yearly":
        return value + timedelta(days=365)
    return value + timedelta(days=30)


def current_access_end(subscription):
    if subscription is None:
        return None
    return ensure_utc(subscription.current_period_end or subscription.trial_end)


def update_subscription_from_asaas_data(subscription, tenant, user, subscription_payload=None, payment_payload=None, status_override=None):
    if subscription is None:
        subscription = get_primary_subscription(tenant.id)
    if subscription is None:
        subscription = Subscription(tenant_id=tenant.id, user_id=user.id, plan=PLAN_KEY)
        database.session.add(subscription)

    subscription.tenant_id = tenant.id
    subscription.user_id = user.id
    subscription.asaas_customer_id = tenant.asaas_customer_id or subscription.asaas_customer_id
    had_active_access = subscription_allows_access(subscription)

    if subscription_payload:
        subscription.asaas_subscription_id = subscription_payload.get("id", subscription.asaas_subscription_id)
        subscription.asaas_customer_id = subscription_payload.get("customer", subscription.asaas_customer_id)
        subscription.plan = PLAN_KEY
        subscription.billing_interval = "yearly" if subscription_payload.get("cycle") == "YEARLY" else "monthly"
        subscription.billing_method = subscription_payload.get("billingType", subscription.billing_method)
        subscription.status = status_override or normalize_subscription_status(subscription_payload.get("status"), payment_payload)
        subscription.next_due_date = parse_asaas_datetime(subscription_payload.get("nextDueDate"))
        subscription.current_period_end = subscription.next_due_date
        if subscription_payload.get("deleted"):
            subscription.cancel_at_period_end = True

    if payment_payload:
        subscription.asaas_customer_id = payment_payload.get("customer", subscription.asaas_customer_id)
        subscription.last_payment_id = payment_payload.get("id", subscription.last_payment_id)
        subscription.last_invoice_url = payment_payload.get("invoiceUrl", subscription.last_invoice_url)
        subscription.pix_copy_paste = (
            payment_payload.get("pixTransaction")
            or payment_payload.get("payload")
            or payment_payload.get("qrCode", {}).get("payload")
            or subscription.pix_copy_paste
        )
        subscription.pix_qr_code = (
            payment_payload.get("encodedImage")
            or payment_payload.get("qrCode", {}).get("encodedImage")
            or subscription.pix_qr_code
        )
        payment_status = normalize_payment_status(payment_payload.get("status"))
        if payment_status:
            if not (payment_status == "pending" and had_active_access):
                subscription.status = status_override or payment_status
        due_date = parse_asaas_datetime(payment_payload.get("dueDate"))
        if due_date:
            subscription.next_due_date = due_date
            if payment_status == "active":
                base_end = current_access_end(subscription)
                payment_reference = max(utcnow(), due_date)
                period_start = max(base_end, payment_reference) if base_end and base_end > utcnow() else payment_reference
                period_end = add_interval(period_start, subscription.billing_interval or "monthly")
                subscription.current_period_end = period_end
                subscription.next_due_date = period_end
            elif not had_active_access:
                subscription.current_period_end = due_date

    if tenant.asaas_customer_id != subscription.asaas_customer_id:
        tenant.asaas_customer_id = subscription.asaas_customer_id

    database.session.commit()
    return subscription


def create_pix_payment(user, tenant, billing_interval):
    option = get_plan_catalog().get(billing_interval)
    if option is None:
        raise BillingConfigurationError("Plano selecionado nao existe.")

    customer_id = ensure_asaas_customer(user, tenant)
    charge_amount = option.amount_brl
    payment_payload = {
        "customer": customer_id,
        "billingType": "PIX",
        "value": float(charge_amount),
        "dueDate": date.today().isoformat(),
        "description": f"{option.name} - {tenant.nome}",
        "externalReference": f"tenant:{tenant.id}:user:{user.id}",
    }
    payment = asaas_request("POST", "/payments", payload=payment_payload)
    qr_code = asaas_request("GET", f"/payments/{payment['id']}/pixQrCode")
    payment.update(qr_code)

    subscription = get_primary_subscription(tenant.id)
    if subscription is None:
        subscription = Subscription(tenant_id=tenant.id, user_id=user.id, plan=PLAN_KEY)
        database.session.add(subscription)

    access_active = subscription_allows_access(subscription)
    subscription.plan = PLAN_KEY
    subscription.user_id = user.id
    subscription.tenant_id = tenant.id
    subscription.asaas_customer_id = customer_id
    subscription.asaas_subscription_id = None
    subscription.billing_interval = billing_interval
    subscription.billing_method = "PIX"
    subscription.last_payment_id = payment.get("id")
    subscription.last_invoice_url = payment.get("invoiceUrl")
    subscription.pix_copy_paste = qr_code.get("payload")
    subscription.pix_qr_code = qr_code.get("encodedImage")
    subscription.next_due_date = parse_asaas_datetime(payment.get("dueDate"))
    if not access_active:
        subscription.status = "pending"
        subscription.current_period_end = subscription.next_due_date
    database.session.commit()

    log_payment_event(
        "asaas.pix.payment.created",
        tenant_id=tenant.id,
        user_id=user.id,
        external_event_id=payment.get("id"),
        payload={
            "payment_id": payment.get("id"),
            "billing_interval": billing_interval,
            "billing_method": "PIX",
            "amount_brl": str(charge_amount),
        },
        status="processed",
    )
    return SimpleNamespace(
        url=payment.get("invoiceUrl") or f"{get_app_base_url()}/billing",
        subscription=subscription,
        payment=payment,
    )


def create_checkout_session(user, tenant, billing_interval, billing_method):
    option = get_plan_catalog().get(billing_interval)
    if option is None:
        raise BillingConfigurationError("Plano selecionado nao existe.")
    if billing_method not in {"PIX", "CREDIT_CARD"}:
        raise BillingConfigurationError("Metodo de pagamento invalido.")
    if billing_method == "PIX":
        return create_pix_payment(user, tenant, billing_interval)

    customer_id = ensure_asaas_customer(user, tenant)
    next_due_date = (utcnow() + timedelta(days=TRIAL_DAYS)).date().isoformat()
    subscription_payload = {
        "customer": customer_id,
        "billingType": billing_method,
        "value": float(option.amount_brl),
        "nextDueDate": next_due_date,
        "cycle": interval_to_cycle(option.billing_interval),
        "description": f"{option.name} - {tenant.nome}",
        "externalReference": f"tenant:{tenant.id}:user:{user.id}",
        "endDate": None,
    }
    asaas_subscription = asaas_request("POST", "/subscriptions", payload=subscription_payload)

    payment_payload = get_latest_payment_for_subscription(asaas_subscription.get("id"))
    subscription = get_primary_subscription(tenant.id)
    subscription = update_subscription_from_asaas_data(
        subscription,
        tenant,
        user,
        subscription_payload=asaas_subscription,
        payment_payload=payment_payload,
    )

    log_payment_event(
        "asaas.subscription.created",
        tenant_id=tenant.id,
        user_id=user.id,
        payload={
            "asaas_subscription_id": asaas_subscription.get("id"),
            "billing_interval": billing_interval,
            "billing_method": billing_method,
        },
        status="processed",
    )
    return SimpleNamespace(
        url=(payment_payload or {}).get("invoiceUrl") or f"{get_app_base_url()}/billing?success=true",
        subscription=subscription,
        payment=payment_payload,
    )


def get_subscription_from_asaas(asaas_subscription_id):
    return asaas_request("GET", f"/subscriptions/{asaas_subscription_id}")


def get_latest_payment_for_subscription(asaas_subscription_id):
    if not asaas_subscription_id:
        return None
    payments = asaas_request("GET", "/payments", query={"subscription": asaas_subscription_id, "limit": 1})
    data = payments.get("data") or []
    payment = data[0] if data else None
    if payment and payment.get("billingType") == "PIX":
        try:
            qr_code = asaas_request("GET", f"/payments/{payment['id']}/pixQrCode")
        except BillingProviderError:
            qr_code = {}
        payment.update(qr_code)
    return payment


def cancel_subscription_at_period_end(subscription):
    if subscription is None or not subscription.asaas_subscription_id:
        raise BillingConfigurationError("Nenhuma assinatura Asaas encontrada para cancelar.")
    updated = asaas_request(
        "POST",
        f"/subscriptions/{subscription.asaas_subscription_id}/cancel",
        payload={},
    )
    subscription.cancel_at_period_end = True
    subscription.status = "canceled"
    subscription.current_period_end = parse_asaas_datetime(updated.get("nextDueDate")) or subscription.current_period_end
    database.session.commit()
    log_payment_event(
        "asaas.subscription.cancelled",
        tenant_id=subscription.tenant_id,
        user_id=subscription.user_id,
        payload={"asaas_subscription_id": subscription.asaas_subscription_id},
        status="processed",
    )
    return subscription


def normalize_payment_status(status):
    mapping = {
        "RECEIVED": "active",
        "CONFIRMED": "active",
        "RECEIVED_IN_CASH": "active",
        "PENDING": "pending",
        "OVERDUE": "past_due",
        "REFUNDED": "canceled",
        "REFUND_REQUESTED": "past_due",
        "CHARGEBACK_REQUESTED": "past_due",
        "CHARGEBACK_DISPUTE": "past_due",
        "AWAITING_RISK_ANALYSIS": "pending",
    }
    return mapping.get((status or "").upper())


def normalize_subscription_status(status, payment_payload=None):
    payment_status = normalize_payment_status((payment_payload or {}).get("status"))
    if payment_status:
        return payment_status
    mapping = {
        "ACTIVE": "active",
        "EXPIRED": "canceled",
        "INACTIVE": "canceled",
    }
    return mapping.get((status or "").upper(), "pending")


def find_subscription_by_reference(reference, asaas_subscription_id=None, asaas_customer_id=None):
    tenant = None
    user = None
    if reference and reference.startswith("tenant:"):
        parts = reference.split(":")
        if len(parts) >= 4:
            tenant = database.session.get(Tenant, int(parts[1]))
            user = database.session.get(User, int(parts[3]))
    if tenant is None and asaas_subscription_id:
        subscription = Subscription.query.filter_by(asaas_subscription_id=asaas_subscription_id).first()
        if subscription is not None:
            tenant = subscription.tenant
            user = subscription.user
    if tenant is None and asaas_customer_id:
        tenant = Tenant.query.filter_by(asaas_customer_id=asaas_customer_id).first()
        if tenant is not None:
            user = get_owner_user_for_tenant(tenant.id)
    if tenant is None or user is None:
        raise BillingConfigurationError("Tenant ou usuario nao encontrado para o evento do Asaas.")
    return tenant, user


def sync_subscription_from_asaas_event(event_payload):
    payment = event_payload.get("payment") or {}
    asaas_subscription_id = payment.get("subscription")
    asaas_customer_id = payment.get("customer")
    reference = payment.get("externalReference") or event_payload.get("externalReference")
    tenant, user = find_subscription_by_reference(reference, asaas_subscription_id, asaas_customer_id)

    subscription_payload = None
    if asaas_subscription_id:
        try:
            subscription_payload = get_subscription_from_asaas(asaas_subscription_id)
        except BillingProviderError:
            subscription_payload = {"id": asaas_subscription_id, "billingType": payment.get("billingType")}

    return update_subscription_from_asaas_data(
        get_primary_subscription(tenant.id),
        tenant,
        user,
        subscription_payload=subscription_payload,
        payment_payload=payment or None,
    )
