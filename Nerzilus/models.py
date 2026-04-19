from datetime import datetime, timedelta, timezone
from datetime import time as datetime_time

from flask_login import UserMixin

from Nerzilus import database, login_manager


def utcnow():
    return datetime.now(timezone.utc)


@login_manager.user_loader
def load_user(user_id):
    return database.session.get(User, int(user_id))


class Tenant(database.Model):
    __table_args__ = (database.UniqueConstraint("slug", name="uq_tenant_slug"),)

    id = database.Column(database.Integer, primary_key=True)
    nome = database.Column(database.String(120), nullable=False)
    slug = database.Column(database.String(80), nullable=False)
    business_type = database.Column(database.String(20), nullable=False, default="barbershop")
    tema = database.Column(database.String(20), nullable=False, default="dark")
    cor_primaria = database.Column(database.String(20), nullable=False, default="#d4a373")
    hero_image = database.Column(database.String(255), nullable=True)
    hero_image_data = database.Column(database.LargeBinary, nullable=True)
    hero_image_mimetype = database.Column(database.String(120), nullable=True)
    whatsapp = database.Column(database.String(30), nullable=True)
    notificacoes_whatsapp = database.Column(database.Boolean, nullable=False, default=False)
    stripe_customer_id = database.Column(database.String(120), nullable=True, unique=True)
    asaas_customer_id = database.Column(database.String(120), nullable=True, unique=True)
    criado_em = database.Column(database.DateTime, nullable=False, default=utcnow)

    usuarios = database.relationship("User", backref="tenant", lazy=True)
    barbeiros = database.relationship("Barber", backref="tenant", lazy=True)
    servicos = database.relationship("Service", backref="tenant", lazy=True)
    agendamentos = database.relationship("Appointment", backref="tenant", lazy=True)
    faturamentos = database.relationship("RevenueRecord", backref="tenant", lazy=True)
    assinaturas = database.relationship("Subscription", backref="tenant", lazy=True)
    usos = database.relationship("UsageRecord", backref="tenant", lazy=True)
    eventos_pagamento = database.relationship("PaymentEventLog", backref="tenant", lazy=True)


class User(database.Model, UserMixin):
    __table_args__ = (
        database.UniqueConstraint("tenant_id", "telefone", name="uq_user_tenant_phone"),
        database.UniqueConstraint("tenant_id", "username", name="uq_user_tenant_username"),
        database.UniqueConstraint("tenant_id", "email", name="uq_user_tenant_email"),
    )

    id = database.Column(database.Integer, primary_key=True)
    tenant_id = database.Column(database.Integer, database.ForeignKey("tenant.id"), nullable=False, index=True)
    nome = database.Column(database.String(120), nullable=False)
    telefone = database.Column(database.String(20), nullable=False)
    email = database.Column(database.String(255), nullable=True)
    cpf_cnpj = database.Column(database.String(20), nullable=True)
    username = database.Column(database.String(80), nullable=True)
    senha_hash = database.Column(database.String(255), nullable=True)
    is_admin = database.Column(database.Boolean, nullable=False, default=False)
    criado_em = database.Column(database.DateTime, nullable=False, default=utcnow)
    agendamentos = database.relationship("Appointment", backref="cliente", lazy=True)
    faturamentos = database.relationship("RevenueRecord", backref="cliente_rel", lazy=True)
    assinaturas = database.relationship("Subscription", backref="user", lazy=True)
    usos = database.relationship("UsageRecord", backref="user", lazy=True)
    eventos_pagamento = database.relationship("PaymentEventLog", backref="user", lazy=True)


class Barber(database.Model):
    __table_args__ = (database.UniqueConstraint("tenant_id", "nome", name="uq_barber_tenant_name"),)

    id = database.Column(database.Integer, primary_key=True)
    tenant_id = database.Column(database.Integer, database.ForeignKey("tenant.id"), nullable=False, index=True)
    nome = database.Column(database.String(120), nullable=False)
    especialidade = database.Column(database.String(120), nullable=False)
    icone = database.Column(database.String(4), nullable=False, default="BR")
    slot_interval_minutes = database.Column(database.Integer, nullable=False, default=45)
    expediente_inicio = database.Column(database.Time, nullable=False, default=lambda: datetime_time(hour=9, minute=0))
    expediente_fim = database.Column(database.Time, nullable=False, default=lambda: datetime_time(hour=21, minute=0))
    ativo = database.Column(database.Boolean, nullable=False, default=True)
    criado_em = database.Column(database.DateTime, nullable=False, default=utcnow)
    agendamentos = database.relationship("Appointment", backref="barbeiro_rel", lazy=True)
    indisponibilidades = database.relationship("BarberUnavailableSlot", backref="barbeiro_rel", lazy=True)


class Service(database.Model):
    __table_args__ = (
        database.UniqueConstraint("tenant_id", "slug", name="uq_service_tenant_slug"),
        database.UniqueConstraint("tenant_id", "nome", name="uq_service_tenant_name"),
    )

    id = database.Column(database.Integer, primary_key=True)
    tenant_id = database.Column(database.Integer, database.ForeignKey("tenant.id"), nullable=False, index=True)
    nome = database.Column(database.String(120), nullable=False)
    slug = database.Column(database.String(80), nullable=False)
    valor = database.Column(database.Numeric(10, 2), nullable=False)
    duracao_minutos = database.Column(database.Integer, nullable=False)
    icone = database.Column(database.String(4), nullable=False, default="SV")
    ativo = database.Column(database.Boolean, nullable=False, default=True)
    criado_em = database.Column(database.DateTime, nullable=False, default=utcnow)
    agendamentos = database.relationship("Appointment", backref="servico_rel", lazy=True)


class Appointment(database.Model):
    id = database.Column(database.Integer, primary_key=True)
    tenant_id = database.Column(database.Integer, database.ForeignKey("tenant.id"), nullable=False, index=True)
    cliente_id = database.Column(database.Integer, database.ForeignKey("user.id"), nullable=False)
    barbeiro_id = database.Column(database.Integer, database.ForeignKey("barber.id"), nullable=False)
    servico_id = database.Column(database.Integer, database.ForeignKey("service.id"), nullable=False)
    forma_pagamento = database.Column(database.String(30), nullable=False, default="local")
    data_agendamento = database.Column(database.Date, nullable=False)
    hora_agendamento = database.Column(database.Time, nullable=False)
    status = database.Column(database.String(30), nullable=False, default="pendente")
    criado_em = database.Column(database.DateTime, nullable=False, default=utcnow)
    faturamento = database.relationship("RevenueRecord", backref="agendamento_rel", lazy=True, uselist=False)

    @property
    def inicio(self):
        return datetime.combine(self.data_agendamento, self.hora_agendamento)

    @property
    def fim(self):
        return self.inicio + timedelta(minutes=self.servico_rel.duracao_minutos)


class RevenueRecord(database.Model):
    __table_args__ = (
        database.Index("ix_revenue_record_tenant_data", "tenant_id", "data_referencia"),
    )

    id = database.Column(database.Integer, primary_key=True)
    tenant_id = database.Column(database.Integer, database.ForeignKey("tenant.id"), nullable=False, index=True)
    appointment_id = database.Column(database.Integer, database.ForeignKey("appointment.id"), nullable=True, unique=True)
    cliente_id = database.Column(database.Integer, database.ForeignKey("user.id"), nullable=True, index=True)
    cliente_nome = database.Column(database.String(120), nullable=False)
    barbeiro_nome = database.Column(database.String(120), nullable=False)
    servico_nome = database.Column(database.String(120), nullable=False)
    valor = database.Column(database.Numeric(10, 2), nullable=False)
    forma_pagamento = database.Column(database.String(30), nullable=False, default="local")
    data_referencia = database.Column(database.Date, nullable=False, index=True)
    hora_referencia = database.Column(database.Time, nullable=True)
    status = database.Column(database.String(30), nullable=False, default="confirmado")
    origem = database.Column(database.String(30), nullable=False, default="agendamento")
    criado_em = database.Column(database.DateTime, nullable=False, default=utcnow)
    atualizado_em = database.Column(database.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class BarberUnavailableSlot(database.Model):
    __table_args__ = (
        database.UniqueConstraint(
            "tenant_id",
            "barbeiro_id",
            "data_referencia",
            "hora_referencia",
            name="uq_barber_unavailable_slot",
        ),
    )

    id = database.Column(database.Integer, primary_key=True)
    tenant_id = database.Column(database.Integer, database.ForeignKey("tenant.id"), nullable=False, index=True)
    barbeiro_id = database.Column(database.Integer, database.ForeignKey("barber.id"), nullable=False, index=True)
    data_referencia = database.Column(database.Date, nullable=False)
    hora_referencia = database.Column(database.Time, nullable=False)
    criado_em = database.Column(database.DateTime, nullable=False, default=utcnow)


class Subscription(database.Model):
    __table_args__ = (
        database.UniqueConstraint("stripe_subscription_id", name="uq_subscription_stripe_subscription"),
        database.UniqueConstraint("stripe_checkout_session_id", name="uq_subscription_checkout_session"),
        database.UniqueConstraint("asaas_subscription_id", name="uq_subscription_asaas_subscription"),
    )

    id = database.Column(database.Integer, primary_key=True)
    tenant_id = database.Column(database.Integer, database.ForeignKey("tenant.id"), nullable=False, index=True)
    user_id = database.Column(database.Integer, database.ForeignKey("user.id"), nullable=False, index=True)
    stripe_customer_id = database.Column(database.String(120), nullable=True, index=True)
    stripe_subscription_id = database.Column(database.String(120), nullable=True, index=True)
    stripe_checkout_session_id = database.Column(database.String(120), nullable=True, index=True)
    asaas_customer_id = database.Column(database.String(120), nullable=True, index=True)
    asaas_subscription_id = database.Column(database.String(120), nullable=True, index=True)
    billing_method = database.Column(database.String(20), nullable=True, default="PIX")
    next_due_date = database.Column(database.DateTime, nullable=True)
    last_payment_id = database.Column(database.String(120), nullable=True)
    last_invoice_url = database.Column(database.String(255), nullable=True)
    pix_qr_code = database.Column(database.Text, nullable=True)
    pix_copy_paste = database.Column(database.Text, nullable=True)
    plan = database.Column(database.String(80), nullable=False, default="acesso_liberado")
    billing_interval = database.Column(database.String(20), nullable=False, default="monthly")
    status = database.Column(database.String(30), nullable=False, default="trialing")
    cancel_at_period_end = database.Column(database.Boolean, nullable=False, default=False)
    current_period_end = database.Column(database.DateTime, nullable=True)
    trial_end = database.Column(database.DateTime, nullable=True)
    created_at = database.Column(database.DateTime, nullable=False, default=utcnow)
    updated_at = database.Column(database.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class UsageRecord(database.Model):
    __table_args__ = (
        database.UniqueConstraint("tenant_id", "resource_type", name="uq_usage_tenant_resource_type"),
    )

    id = database.Column(database.Integer, primary_key=True)
    tenant_id = database.Column(database.Integer, database.ForeignKey("tenant.id"), nullable=False, index=True)
    user_id = database.Column(database.Integer, database.ForeignKey("user.id"), nullable=False, index=True)
    resource_type = database.Column(database.String(50), nullable=False)
    quantity = database.Column(database.Integer, nullable=False, default=0)
    created_at = database.Column(database.DateTime, nullable=False, default=utcnow)
    updated_at = database.Column(database.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class PaymentEventLog(database.Model):
    __table_args__ = (
        database.UniqueConstraint("stripe_event_id", name="uq_payment_event_log_stripe_event_id"),
        database.UniqueConstraint("external_event_id", name="uq_payment_event_log_external_event_id"),
    )

    id = database.Column(database.Integer, primary_key=True)
    tenant_id = database.Column(database.Integer, database.ForeignKey("tenant.id"), nullable=True, index=True)
    user_id = database.Column(database.Integer, database.ForeignKey("user.id"), nullable=True, index=True)
    stripe_event_id = database.Column(database.String(120), nullable=True, index=True)
    external_event_id = database.Column(database.String(120), nullable=True, index=True)
    event_type = database.Column(database.String(120), nullable=False)
    status = database.Column(database.String(30), nullable=False, default="received")
    payload = database.Column(database.Text, nullable=True)
    created_at = database.Column(database.DateTime, nullable=False, default=utcnow)
    processed_at = database.Column(database.DateTime, nullable=True)
