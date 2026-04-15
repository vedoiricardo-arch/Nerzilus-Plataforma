from datetime import date, datetime, time, timedelta
from functools import wraps
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask import session
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from Nerzilus import database, seed_tenant_defaults, slugify_text
from Nerzilus.billing import (
    BillingConfigurationError,
    BillingProviderError,
    asaas_is_configured,
    can_access_feature,
    can_create_client,
    cancel_subscription_at_period_end,
    create_checkout_session,
    ensure_asaas_customer,
    ensure_trial_subscription,
    get_owner_user_for_tenant,
    get_plan_catalog,
    get_primary_subscription,
    log_payment_event,
    record_usage,
    subscription_allows_access,
    sync_subscription_from_asaas_event,
    tenant_has_active_access,
)
from Nerzilus.forms import (
    AdminLoginForm,
    AppointmentForm,
    AppointmentStatusForm,
    BarberForm,
    BarberWorkingSlotForm,
    BillingCancelForm,
    BillingCheckoutForm,
    BillingCustomerForm,
    BillingManagementForm,
    ClientAccessForm,
    PlatformSignupForm,
    ServiceForm,
    SlotAvailabilityForm,
    TenantThemeForm,
    TenantWhatsAppForm,
)
from Nerzilus.models import Appointment, Barber, BarberUnavailableSlot, BarberWorkingSlot, PaymentEventLog, Service, Subscription, Tenant, User
from Nerzilus.notifications import (
    build_whatsapp_link,
    format_phone_display,
    resolve_admin_whatsapp,
    send_booking_whatsapp_notification,
)


main_bp = Blueprint("main", __name__)
AGENDA_SLOT_MINUTES = 45
DEFAULT_HERO_IMAGE_URL = "https://images.unsplash.com/photo-1621605815971-fbc98d665033?auto=format&fit=crop&w=1400&q=80"
AGENDA_PERIODS = (
    ("manha", "Manha", time(hour=9, minute=0), time(hour=12, minute=0)),
    ("tarde", "Tarde", time(hour=14, minute=0), time(hour=21, minute=0)),
)


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped_view


def tenant_member_required(view):
    @wraps(view)
    def wrapped_view(tenant_slug, *args, **kwargs):
        tenant = get_tenant_or_404(tenant_slug)
        if not current_user.is_authenticated:
            return redirect(url_for("main.acesso_cliente", tenant_slug=tenant.slug))
        if current_user.tenant_id != tenant.id:
            abort(403)
        return view(tenant, *args, **kwargs)

    return wrapped_view


def subscription_required(view):
    @wraps(view)
    def wrapped_view(tenant, *args, **kwargs):
        if tenant_has_active_access(tenant):
            return view(tenant, *args, **kwargs)
        if current_user.is_authenticated and current_user.is_admin and current_user.tenant_id == tenant.id:
            flash("Sua assinatura esta inativa. Regularize o plano para liberar o ambiente.", "error")
            return redirect(url_for("main.billing_dashboard"))
        abort(403)

    return wrapped_view


def get_tenant_or_404(tenant_slug):
    tenant = Tenant.query.filter_by(slug=slugify_text(tenant_slug)).first()
    if tenant is None:
        abort(404)
    return tenant


def tenant_hero_image_url(tenant):
    if tenant.hero_image:
        return url_for("static", filename=f"fotos_posts/{tenant.hero_image}")
    return DEFAULT_HERO_IMAGE_URL


def build_appointment_form(tenant_id):
    form = AppointmentForm()
    barbeiros = Barber.query.filter_by(tenant_id=tenant_id, ativo=True).order_by(Barber.nome.asc()).all()
    servicos = Service.query.filter_by(tenant_id=tenant_id, ativo=True).order_by(Service.valor.asc()).all()
    form.barbeiro_id.choices = [(barbeiro.id, barbeiro.nome) for barbeiro in barbeiros]
    form.servico_id.choices = [(servico.id, servico.nome) for servico in servicos]
    return form


def get_barber_slot_interval(tenant_id, barber_id):
    if not barber_id:
        return AGENDA_SLOT_MINUTES
    barber = Barber.query.filter_by(id=barber_id, tenant_id=tenant_id).first()
    if barber is None or not barber.slot_interval_minutes:
        return AGENDA_SLOT_MINUTES
    return barber.slot_interval_minutes


def has_overlap(tenant_id, barber_id, service, selected_date, selected_time, ignore_appointment_id=None):
    agendamentos = (
        Appointment.query.filter_by(tenant_id=tenant_id, barbeiro_id=barber_id)
        .order_by(Appointment.data_agendamento.asc(), Appointment.hora_agendamento.asc())
        .all()
    )
    proposed_start = datetime.combine(selected_date, selected_time)
    proposed_end = proposed_start + timedelta(minutes=service.duracao_minutos)
    for item in agendamentos:
        if ignore_appointment_id is not None and item.id == ignore_appointment_id:
            continue
        current_start = datetime.combine(item.data_agendamento, item.hora_agendamento)
        current_end = current_start + timedelta(minutes=item.servico_rel.duracao_minutos)
        if current_start < proposed_end and current_end > proposed_start and item.status != "cancelado":
            return True
    return False


def get_blocked_slot_labels(tenant_id, barber_id, selected_day):
    if not barber_id or not selected_day:
        return set()
    slots = (
        BarberUnavailableSlot.query.filter_by(
            tenant_id=tenant_id,
            barbeiro_id=barber_id,
            data_referencia=selected_day,
        )
        .order_by(BarberUnavailableSlot.hora_referencia.asc())
        .all()
    )
    return {slot.hora_referencia.strftime("%H:%M") for slot in slots}


def get_standard_slot_labels(slot_interval_minutes=AGENDA_SLOT_MINUTES):
    labels = []
    for _, _, start_time, end_time in AGENDA_PERIODS:
        for slot in build_time_slots(start_time, end_time, slot_interval_minutes):
            labels.append(slot.strftime("%H:%M"))
    return labels


def get_working_slot_labels(tenant_id, barber_id, selected_day):
    slot_interval_minutes = get_barber_slot_interval(tenant_id, barber_id)
    if not barber_id or not selected_day:
        return set(get_standard_slot_labels(slot_interval_minutes))
    slots = (
        BarberWorkingSlot.query.filter_by(
            tenant_id=tenant_id,
            barbeiro_id=barber_id,
            weekday=selected_day.weekday(),
        )
        .order_by(BarberWorkingSlot.hora_referencia.asc())
        .all()
    )
    if not slots:
        return set(get_standard_slot_labels(slot_interval_minutes))
    return {slot.hora_referencia.strftime("%H:%M") for slot in slots}


def build_platform_signup_form():
    return PlatformSignupForm(prefix="signup")


def create_platform_account(form):
    tenant = Tenant(
        nome=form.nome_barbearia.data,
        slug=form.slug.data,
        business_type="barbershop",
        tema="dark",
        cor_primaria="#d4a373",
        whatsapp=form.whatsapp.data.strip() if form.whatsapp.data else None,
        notificacoes_whatsapp=bool(form.whatsapp.data and form.whatsapp.data.strip()),
    )
    database.session.add(tenant)
    database.session.flush()

    admin = User(
        tenant_id=tenant.id,
        nome=form.nome_barbearia.data,
        telefone=form.whatsapp.data.strip() if form.whatsapp.data else f"admin-{tenant.slug}",
        email=form.email.data,
        cpf_cnpj=form.cpf_cnpj.data,
        username=form.username.data,
        senha_hash=generate_password_hash(form.senha.data),
        is_admin=True,
    )
    database.session.add(admin)
    seed_tenant_defaults(tenant.id)
    database.session.commit()
    ensure_trial_subscription(tenant, admin)
    if asaas_is_configured():
        try:
            ensure_asaas_customer(admin, tenant)
        except (BillingConfigurationError, BillingProviderError):
            pass
    return tenant, admin


def build_platform_login_form():
    return AdminLoginForm(prefix="login")


def build_time_slots(start_time, end_time, slot_interval_minutes=AGENDA_SLOT_MINUTES):
    slots = []
    current = datetime.combine(date.today(), start_time)
    end = datetime.combine(date.today(), end_time)
    while current <= end:
        slots.append(current.time())
        current += timedelta(minutes=slot_interval_minutes)
    return slots


def appointment_slot_span(appointment, slot_interval_minutes=AGENDA_SLOT_MINUTES):
    duration = appointment.servico_rel.duracao_minutos or slot_interval_minutes
    return max(1, (duration + slot_interval_minutes - 1) // slot_interval_minutes)


def build_day_schedule(appointments, selected_day, blocked_slot_labels=None, working_slot_labels=None, slot_interval_minutes=AGENDA_SLOT_MINUTES):
    blocked_slot_labels = blocked_slot_labels or set()
    working_slot_labels = working_slot_labels or set(get_standard_slot_labels(slot_interval_minutes))
    appointments_by_time = {appointment.hora_agendamento.strftime("%H:%M"): appointment for appointment in appointments}
    occupied_slots = set()
    for appointment in appointments:
        span = appointment_slot_span(appointment, slot_interval_minutes)
        for index in range(1, span):
            occupied_slots.add(
                (
                    datetime.combine(selected_day, appointment.hora_agendamento) + timedelta(minutes=slot_interval_minutes * index)
                ).time().strftime("%H:%M")
            )

    sections = []
    for section_key, section_label, start_time, end_time in AGENDA_PERIODS:
        rows = []
        for slot in build_time_slots(start_time, end_time, slot_interval_minutes):
            slot_key = slot.strftime("%H:%M")
            appointment = appointments_by_time.get(slot_key)
            rows.append(
                {
                    "label": slot_key,
                    "appointment": appointment,
                    "is_working": slot_key in working_slot_labels,
                    "is_blocked": slot_key in blocked_slot_labels,
                    "is_continuation": slot_key in occupied_slots,
                    "span": appointment_slot_span(appointment, slot_interval_minutes) if appointment else 1,
                }
            )
        sections.append({"key": section_key, "label": section_label, "rows": rows})
    return sections


def build_booking_time_sections(appointments, selected_day, selected_time_value=None, slot_interval_minutes=AGENDA_SLOT_MINUTES):
    sections = build_day_schedule(appointments, selected_day, slot_interval_minutes=slot_interval_minutes)
    booking_sections = []
    for section in sections:
        rows = []
        for row in section["rows"]:
            rows.append(
                {
                    **row,
                    "available": row["is_working"] and not row["appointment"] and not row["is_continuation"] and not row["is_blocked"],
                    "selected": row["label"] == selected_time_value,
                }
            )
        booking_sections.append({**section, "rows": rows})
    return booking_sections


def build_booking_time_sections_for_barber(tenant_id, barber_id, appointments, selected_day, selected_time_value=None):
    slot_interval_minutes = get_barber_slot_interval(tenant_id, barber_id)
    blocked_slot_labels = get_blocked_slot_labels(tenant_id, barber_id, selected_day)
    working_slot_labels = get_working_slot_labels(tenant_id, barber_id, selected_day)
    sections = build_day_schedule(appointments, selected_day, blocked_slot_labels, working_slot_labels, slot_interval_minutes)
    booking_sections = []
    for section in sections:
        rows = []
        for row in section["rows"]:
            rows.append(
                {
                    **row,
                    "available": row["is_working"] and not row["appointment"] and not row["is_continuation"] and not row["is_blocked"],
                    "selected": row["label"] == selected_time_value,
                }
            )
        booking_sections.append({**section, "rows": rows})
    return booking_sections


def build_working_hours_editor(tenant_id, barber_id):
    slot_interval_minutes = get_barber_slot_interval(tenant_id, barber_id)
    weekday_labels = [(0, "Seg"), (1, "Ter"), (2, "Qua"), (3, "Qui"), (4, "Sex"), (5, "Sab"), (6, "Dom")]
    all_slots = []
    if barber_id:
        all_slots = BarberWorkingSlot.query.filter_by(tenant_id=tenant_id, barbeiro_id=barber_id).all()
    active_keys = {(slot.weekday, slot.hora_referencia.strftime("%H:%M")) for slot in all_slots}
    if not all_slots and barber_id:
        active_keys = {(weekday, label) for weekday, _ in weekday_labels[:6] for label in get_standard_slot_labels(slot_interval_minutes)}

    sections = []
    for section_key, section_label, start_time, end_time in AGENDA_PERIODS:
        rows = []
        for slot in build_time_slots(start_time, end_time, slot_interval_minutes):
            label = slot.strftime("%H:%M")
            weekdays = []
            for weekday, weekday_label in weekday_labels:
                weekdays.append(
                    {
                        "weekday": weekday,
                        "weekday_label": weekday_label,
                        "label": label,
                        "enabled": (weekday, label) in active_keys,
                    }
                )
            rows.append({"label": label, "weekdays": weekdays})
        sections.append({"key": section_key, "label": section_label, "rows": rows})
    return sections


def build_week_schedule(appointments, week_start):
    days = [week_start + timedelta(days=index) for index in range(7)]
    columns = []
    for day_item in days:
        day_appointments = [item for item in appointments if item.data_agendamento == day_item]
        day_appointments.sort(key=lambda item: item.hora_agendamento)
        columns.append(
            {
                "day": day_item,
                "appointments": day_appointments,
            }
        )
    return days, columns


@main_bp.route("/", methods=["GET", "POST"])
def homepage():
    login_form = build_platform_login_form()
    signup_form = build_platform_signup_form()
    show_signup = request.args.get("criar_conta") == "1"

    if "login-botao_confirmacao" in request.form and login_form.validate_on_submit():
        admins = User.query.filter_by(username=login_form.username.data, is_admin=True).all()
        for admin in admins:
            if admin.senha_hash and check_password_hash(admin.senha_hash, login_form.senha.data):
                login_user(admin)
                flash("Login realizado.", "success")
                return redirect(url_for("main.dashboard_redirect"))
        flash("Login invalido.", "error")

    if "signup-botao_confirmacao" in request.form:
        show_signup = True
        if signup_form.validate_on_submit():
            tenant, admin = create_platform_account(signup_form)
            login_user(admin)
            flash("Conta criada na NerzilusBee.", "success")
            return redirect(url_for("main.billing_dashboard"))

    return render_template(
        "homepage.html",
        login_form=login_form,
        signup_form=signup_form,
        show_signup=show_signup,
    )


@main_bp.route("/t/<tenant_slug>/cliente", methods=["GET", "POST"])
def acesso_cliente(tenant_slug):
    tenant = get_tenant_or_404(tenant_slug)
    if not tenant_has_active_access(tenant):
        abort(403)
    if current_user.is_authenticated and not current_user.is_admin and current_user.tenant_id == tenant.id:
        return redirect(url_for("main.dashboard_cliente", tenant_slug=tenant.slug))

    form = ClientAccessForm()
    if form.validate_on_submit():
        telefone = form.telefone.data.strip()
        usuario = User.query.filter_by(tenant_id=tenant.id, telefone=telefone, is_admin=False).first()
        if usuario is None:
            if not can_create_client(get_owner_user_for_tenant(tenant)):
                flash("O plano atual nao permite novos clientes.", "error")
                return redirect(url_for("main.acesso_cliente", tenant_slug=tenant.slug))
            usuario = User(
                tenant_id=tenant.id,
                nome=form.nome.data.strip(),
                telefone=telefone,
                is_admin=False,
            )
            database.session.add(usuario)
            database.session.commit()
            record_usage(tenant.id, get_owner_user_for_tenant(tenant).id, "clients")
        else:
            usuario.nome = form.nome.data.strip()
            database.session.commit()

        login_user(usuario)
        return redirect(url_for("main.dashboard_cliente", tenant_slug=tenant.slug))

    return render_template("client_access.html", form=form, tenant=tenant)


@main_bp.route("/t/<tenant_slug>/admin/login", methods=["GET", "POST"])
def login_admin(tenant_slug):
    tenant = get_tenant_or_404(tenant_slug)
    if current_user.is_authenticated and current_user.is_admin and current_user.tenant_id == tenant.id:
        return redirect(url_for("main.dashboard_redirect"))

    form = AdminLoginForm()
    if form.validate_on_submit():
        usuario = User.query.filter_by(
            tenant_id=tenant.id,
            username=form.username.data.lower(),
            is_admin=True,
        ).first()
        if usuario and usuario.senha_hash and check_password_hash(usuario.senha_hash, form.senha.data):
            login_user(usuario)
            return redirect(url_for("main.dashboard_redirect"))
        flash("Login de administrador invalido.", "error")

    return render_template("admin_login.html", form=form, tenant=tenant)


@main_bp.route("/dashboard")
@login_required
def dashboard_redirect():
    if current_user.is_admin:
        tenant = current_user.tenant
        if tenant_has_active_access(tenant):
            return redirect(url_for("main.admin_dashboard", tenant_slug=tenant.slug))
        return redirect(url_for("main.billing_dashboard"))

    return redirect(url_for("main.dashboard_cliente", tenant_slug=current_user.tenant.slug))


@main_bp.route("/healthz")
def healthcheck():
    try:
        database.session.execute(text("SELECT 1"))
    except Exception:
        return {"status": "error"}, 503
    return {"status": "ok"}, 200


@main_bp.route("/plans")
@login_required
def plans():
    if not current_user.is_admin:
        abort(403)

    tenant = current_user.tenant
    subscription = get_primary_subscription(tenant.id)
    checkout_forms = {}
    for interval in get_plan_catalog():
        for method in ("PIX", "CREDIT_CARD"):
            checkout_forms[(interval, method)] = BillingCheckoutForm(
                billing_interval=interval,
                billing_method=method,
            )
    management_form = BillingManagementForm()
    cancel_form = BillingCancelForm()
    customer_form = BillingCustomerForm()
    customer_form.cpf_cnpj.data = current_user.cpf_cnpj or ""
    return render_template(
        "plans.html",
        tenant=tenant,
        subscription=subscription,
        plan_catalog=get_plan_catalog(),
        checkout_forms=checkout_forms,
        management_form=management_form,
        cancel_form=cancel_form,
        customer_form=customer_form,
        asaas_is_configured=asaas_is_configured(),
    )


@main_bp.route("/billing")
@login_required
def billing_dashboard():
    if not current_user.is_admin:
        abort(403)

    tenant = current_user.tenant
    subscription = get_primary_subscription(tenant.id)
    checkout_forms = {}
    for interval in get_plan_catalog():
        for method in ("PIX", "CREDIT_CARD"):
            checkout_forms[(interval, method)] = BillingCheckoutForm(
                billing_interval=interval,
                billing_method=method,
            )
    management_form = BillingManagementForm()
    cancel_form = BillingCancelForm()
    customer_form = BillingCustomerForm()
    customer_form.cpf_cnpj.data = current_user.cpf_cnpj or ""
    return render_template(
        "billing_dashboard.html",
        tenant=tenant,
        subscription=subscription,
        plan_catalog=get_plan_catalog(),
        checkout_forms=checkout_forms,
        management_form=management_form,
        cancel_form=cancel_form,
        customer_form=customer_form,
        asaas_is_configured=asaas_is_configured(),
    )


@main_bp.route("/create-checkout-session", methods=["POST"])
@login_required
def create_checkout_session_route():
    if not current_user.is_admin:
        abort(403)

    form = BillingCheckoutForm()
    if not form.validate_on_submit():
        flash("Selecione um plano valido para continuar.", "error")
        return redirect(url_for("main.plans"))
    if not current_user.cpf_cnpj:
        flash("Preencha o CPF ou CNPJ do responsavel antes de iniciar a cobranca no Asaas.", "error")
        return redirect(url_for("main.billing_dashboard"))

    try:
        checkout_session = create_checkout_session(
            current_user,
            current_user.tenant,
            form.billing_interval.data,
            form.billing_method.data,
        )
    except BillingConfigurationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.plans"))
    except BillingProviderError as exc:
        flash(f"Nao foi possivel iniciar a assinatura no Asaas: {exc}", "error")
        return redirect(url_for("main.plans"))

    return redirect(checkout_session.url)


@main_bp.route("/billing/manage", methods=["POST"])
@login_required
def billing_manage():
    if not current_user.is_admin:
        abort(403)

    form = BillingManagementForm()
    if not form.validate_on_submit():
        flash("Nao foi possivel atualizar a assinatura.", "error")
        return redirect(url_for("main.billing_dashboard"))
    return redirect(url_for("main.plans"))


@main_bp.route("/billing/customer", methods=["POST"])
@login_required
def billing_customer():
    if not current_user.is_admin:
        abort(403)

    form = BillingCustomerForm()
    if not form.validate_on_submit():
        flash("Informe um CPF ou CNPJ valido para continuar com a cobranca.", "error")
        return redirect(url_for("main.billing_dashboard"))

    current_user.cpf_cnpj = form.cpf_cnpj.data
    database.session.commit()
    flash("CPF/CNPJ salvo para o billing no Asaas.", "success")
    return redirect(url_for("main.billing_dashboard"))


@main_bp.route("/billing/cancel", methods=["POST"])
@login_required
def billing_cancel():
    if not current_user.is_admin:
        abort(403)

    form = BillingCancelForm()
    if not form.validate_on_submit():
        flash("Nao foi possivel cancelar a assinatura.", "error")
        return redirect(url_for("main.billing_dashboard"))

    try:
        cancel_subscription_at_period_end(get_primary_subscription(current_user.tenant_id))
    except (BillingConfigurationError, BillingProviderError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.billing_dashboard"))

    flash("Cancelamento solicitado no Asaas para o proximo ciclo.", "success")
    return redirect(url_for("main.billing_dashboard"))


@main_bp.route("/webhook/asaas", methods=["POST"])
def asaas_webhook():
    payload = request.get_json(silent=True) or {}
    configured_token = current_app.config.get("ASAAS_WEBHOOK_TOKEN") or ""
    received_token = request.headers.get("asaas-access-token", "")

    if configured_token and configured_token != received_token:
        return ("Token do webhook invalido.", 401)

    event_id = payload.get("id") or payload.get("eventId") or payload.get("payment", {}).get("id")
    existing_log = PaymentEventLog.query.filter_by(external_event_id=event_id).first()
    if existing_log is not None and existing_log.processed_at is not None and existing_log.status == "processed":
        return ("ok", 200)
    if existing_log is None:
        log_payment_event(
            payload.get("event", "asaas.unknown"),
            external_event_id=event_id,
            payload=payload,
            status="received",
        )

    try:
        subscription = sync_subscription_from_asaas_event(payload)
        event_type = payload.get("event", "asaas.unknown")
        if subscription is not None:
            log_payment_event(
                event_type,
                tenant_id=subscription.tenant_id,
                user_id=subscription.user_id,
                external_event_id=event_id,
                payload=payload,
                status="processed",
            )
        else:
            log_payment_event(
                event_type,
                external_event_id=event_id,
                payload=payload,
                status="ignored",
            )
    except Exception as exc:
        log_payment_event(
            payload.get("event", "asaas.unknown"),
            external_event_id=event_id,
            payload={"error": str(exc)},
            status="error",
        )
        return ("erro ao processar webhook", 400)

    return ("ok", 200)


@main_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("main.homepage"))


@main_bp.route("/t/<tenant_slug>/cliente/dashboard", methods=["GET", "POST"])
@login_required
@tenant_member_required
def dashboard_cliente(tenant):
    if current_user.is_admin:
        return redirect(url_for("main.admin_dashboard", tenant_slug=tenant.slug))

    form = build_appointment_form(tenant.id)
    service_ids = {service.id for service in Service.query.filter_by(tenant_id=tenant.id, ativo=True).all()}
    barber_choice_ids = {choice[0] for choice in form.barbeiro_id.choices}

    if request.method == "GET":
        requested_barber_id = request.args.get("barbeiro_id", type=int)
        if requested_barber_id in barber_choice_ids:
            form.barbeiro_id.data = requested_barber_id
        elif form.barbeiro_id.choices:
            form.barbeiro_id.data = form.barbeiro_id.choices[0][0]

        requested_service_id = request.args.get("servico_id", type=int)
        if requested_service_id in service_ids:
            form.servico_id.data = requested_service_id

        requested_payment = request.args.get("forma_pagamento")
        if requested_payment in dict(form.forma_pagamento.choices):
            form.forma_pagamento.data = requested_payment

        requested_day = request.args.get("data_agendamento")
        try:
            form.data_agendamento.data = (
                datetime.strptime(requested_day, "%Y-%m-%d").date() if requested_day else date.today()
            )
        except ValueError:
            form.data_agendamento.data = date.today()

        requested_time = request.args.get("hora_agendamento")
        if requested_time:
            try:
                form.hora_agendamento.data = datetime.strptime(requested_time, "%H:%M").time()
            except ValueError:
                form.hora_agendamento.data = None

    if form.barbeiro_id.data is None and form.barbeiro_id.choices:
        form.barbeiro_id.data = form.barbeiro_id.choices[0][0]
    if form.data_agendamento.data is None:
        form.data_agendamento.data = date.today()

    recent_admin_whatsapp_link = session.pop("recent_admin_whatsapp_link", None)
    recent_admin_whatsapp_message = session.pop("recent_admin_whatsapp_message", None)

    selected_time_value = request.form.get(form.hora_agendamento.name)
    if not selected_time_value and form.hora_agendamento.data is not None:
        selected_time_value = form.hora_agendamento.data.strftime("%H:%M")

    booking_appointments = []
    blocked_slot_labels = set()
    working_slot_labels = set()
    if form.barbeiro_id.data and form.data_agendamento.data:
        booking_appointments = (
            Appointment.query.filter(
                Appointment.tenant_id == tenant.id,
                Appointment.barbeiro_id == form.barbeiro_id.data,
                Appointment.data_agendamento == form.data_agendamento.data,
                Appointment.status != "cancelado",
            )
            .order_by(Appointment.hora_agendamento.asc())
            .all()
        )
        blocked_slot_labels = get_blocked_slot_labels(tenant.id, form.barbeiro_id.data, form.data_agendamento.data)
        working_slot_labels = get_working_slot_labels(tenant.id, form.barbeiro_id.data, form.data_agendamento.data)

    if not form.barbeiro_id.choices:
        flash("Nenhum barbeiro disponivel.", "error")
    elif form.validate_on_submit():
        servico = Service.query.filter_by(id=form.servico_id.data, tenant_id=tenant.id, ativo=True).first()
        if servico is None:
            flash("Servico invalido.", "error")
            return redirect(url_for("main.dashboard_cliente", tenant_slug=tenant.slug))
        if form.hora_agendamento.data.strftime("%H:%M") in blocked_slot_labels:
            flash("Este horario foi bloqueado pela barbearia.", "error")
            return redirect(url_for("main.dashboard_cliente", tenant_slug=tenant.slug))
        if form.hora_agendamento.data.strftime("%H:%M") not in working_slot_labels:
            flash("Este horario esta fora do atendimento configurado pelo admin.", "error")
            return redirect(url_for("main.dashboard_cliente", tenant_slug=tenant.slug))
        if has_overlap(
            tenant.id,
            form.barbeiro_id.data,
            servico,
            form.data_agendamento.data,
            form.hora_agendamento.data,
        ):
            flash("Este horario acabou de ficar indisponivel.", "error")
            return redirect(url_for("main.dashboard_cliente", tenant_slug=tenant.slug))

        agendamento = Appointment(
            tenant_id=tenant.id,
            cliente_id=current_user.id,
            barbeiro_id=form.barbeiro_id.data,
            servico_id=form.servico_id.data,
            forma_pagamento=form.forma_pagamento.data,
            data_agendamento=form.data_agendamento.data,
            hora_agendamento=form.hora_agendamento.data,
            status="confirmado",
        )
        database.session.add(agendamento)
        database.session.commit()
        notification = send_booking_whatsapp_notification(agendamento)
        direct_link = getattr(notification, "direct_link", None)
        direct_message = getattr(notification, "message", None)
        if isinstance(direct_link, str) and direct_link:
            session["recent_admin_whatsapp_link"] = direct_link
        if isinstance(direct_message, str) and direct_message:
            session["recent_admin_whatsapp_message"] = direct_message
        flash("Agendamento criado.", "success")
        return redirect(url_for("main.dashboard_cliente", tenant_slug=tenant.slug))

    servicos = Service.query.filter_by(tenant_id=tenant.id, ativo=True).order_by(Service.valor.asc()).all()
    return render_template(
        "client_dashboard.html",
        form=form,
        services=servicos,
        booking_time_sections=build_booking_time_sections_for_barber(
            tenant.id,
            form.barbeiro_id.data,
            booking_appointments,
            form.data_agendamento.data,
            selected_time_value,
        ),
        tenant=tenant,
        tenant_whatsapp_link=build_whatsapp_link(tenant.whatsapp),
        recent_admin_whatsapp_link=recent_admin_whatsapp_link,
        recent_admin_whatsapp_message=recent_admin_whatsapp_message,
    )


@main_bp.route("/t/<tenant_slug>/cliente/agendamento/<int:appointment_id>/cancelar", methods=["POST"])
@login_required
@tenant_member_required
def cancelar_agendamento_cliente(tenant, appointment_id):
    agendamento = Appointment.query.filter_by(id=appointment_id, tenant_id=tenant.id).first_or_404()
    if current_user.is_admin or agendamento.cliente_id != current_user.id:
        abort(403)

    agendamento.status = "cancelado"
    database.session.commit()
    flash("Agendamento cancelado.", "success")
    return redirect(url_for("main.dashboard_cliente", tenant_slug=tenant.slug))


@main_bp.route("/t/<tenant_slug>/admin")
@login_required
@tenant_member_required
@admin_required
@subscription_required
def admin_dashboard(tenant):
    barbeiro_form = BarberForm()
    service_form = ServiceForm()
    slot_form = SlotAvailabilityForm()
    working_slot_form = BarberWorkingSlotForm()
    tenant_whatsapp_form = TenantWhatsAppForm()
    tenant_theme_form = TenantThemeForm()
    tenant_whatsapp_form.whatsapp.data = format_phone_display(resolve_admin_whatsapp(tenant))
    tenant_theme_form.tema.data = tenant.tema if tenant.tema in {"dark", "light", "pink"} else "dark"
    usuarios = User.query.filter_by(tenant_id=tenant.id).order_by(User.is_admin.desc(), User.nome.asc()).all()
    barbeiros = Barber.query.filter_by(tenant_id=tenant.id).order_by(Barber.nome.asc()).all()
    servicos = Service.query.filter_by(tenant_id=tenant.id).order_by(Service.valor.asc()).all()
    agendamentos = (
        Appointment.query.filter_by(tenant_id=tenant.id)
        .order_by(Appointment.data_agendamento.asc(), Appointment.hora_agendamento.asc())
        .all()
    )
    selected_day_raw = request.args.get("day")
    selected_barber_id = request.args.get("barbeiro_id", type=int)
    try:
        selected_day = datetime.strptime(selected_day_raw, "%Y-%m-%d").date() if selected_day_raw else date.today()
    except ValueError:
        selected_day = date.today()
    active_barber = None
    if barbeiros:
        active_barber = next((barber for barber in barbeiros if barber.id == selected_barber_id), barbeiros[0])
        selected_barber_id = active_barber.id
        barbeiro_form.slot_interval_minutes.data = active_barber.slot_interval_minutes or AGENDA_SLOT_MINUTES
    active_slot_interval_minutes = get_barber_slot_interval(tenant.id, selected_barber_id)
    week_start = selected_day - timedelta(days=selected_day.weekday())
    day_appointments = [
        item
        for item in agendamentos
        if item.data_agendamento == selected_day and (selected_barber_id is None or item.barbeiro_id == selected_barber_id)
    ]
    week_appointments = [
        item
        for item in agendamentos
        if week_start <= item.data_agendamento <= week_start + timedelta(days=6)
        and (selected_barber_id is None or item.barbeiro_id == selected_barber_id)
    ]
    blocked_slot_labels = get_blocked_slot_labels(tenant.id, selected_barber_id, selected_day)
    working_slot_labels = get_working_slot_labels(tenant.id, selected_barber_id, selected_day)
    week_days, week_schedule = build_week_schedule(week_appointments, week_start)
    status_forms = {agendamento.id: AppointmentStatusForm(status=agendamento.status) for agendamento in agendamentos}
    return render_template(
        "admin_dashboard.html",
        barbeiro_form=barbeiro_form,
        service_form=service_form,
        slot_form=slot_form,
        tenant_theme_form=tenant_theme_form,
        tenant_whatsapp_form=tenant_whatsapp_form,
        usuarios=usuarios,
        barbeiros=barbeiros,
        active_barber=active_barber,
        servicos=servicos,
        agendamentos=agendamentos,
        selected_day=selected_day,
        selected_barber_id=selected_barber_id,
        day_schedule=build_day_schedule(
            day_appointments,
            selected_day,
            blocked_slot_labels,
            working_slot_labels,
            active_slot_interval_minutes,
        ),
        working_hours_editor=build_working_hours_editor(tenant.id, selected_barber_id),
        active_slot_interval_minutes=active_slot_interval_minutes,
        week_days=week_days,
        week_schedule=week_schedule,
        status_forms=status_forms,
        working_slot_form=working_slot_form,
        subscription=get_primary_subscription(tenant.id),
        tenant=tenant,
        hero_image_url=tenant_hero_image_url(tenant),
        tenant_whatsapp_link=build_whatsapp_link(tenant.whatsapp),
        client_booking_url=url_for("main.acesso_cliente", tenant_slug=tenant.slug, _external=True),
    )


@main_bp.route("/t/<tenant_slug>/admin/whatsapp", methods=["POST"])
@login_required
@tenant_member_required
@admin_required
@subscription_required
def atualizar_whatsapp_tenant(tenant):
    form = TenantWhatsAppForm()
    if form.validate_on_submit():
        tenant.whatsapp = form.whatsapp.data or None
        tenant.notificacoes_whatsapp = bool(tenant.whatsapp)
        admin = (
            User.query.filter_by(tenant_id=tenant.id, is_admin=True)
            .order_by(User.id.asc())
            .first()
        )
        if admin is not None and tenant.whatsapp:
            admin.telefone = tenant.whatsapp
        database.session.commit()
        flash("WhatsApp da barbearia atualizado.", "success")
    else:
        flash("Nao foi possivel atualizar o WhatsApp.", "error")
    return redirect(url_for("main.admin_dashboard", tenant_slug=tenant.slug))


@main_bp.route("/t/<tenant_slug>/admin/tema", methods=["POST"])
@login_required
@tenant_member_required
@admin_required
@subscription_required
def atualizar_tema_tenant(tenant):
    form = TenantThemeForm()
    if form.validate_on_submit():
        tenant.tema = form.tema.data
        database.session.commit()
        flash("Tema da plataforma atualizado.", "success")
    else:
        flash("Nao foi possivel atualizar o tema.", "error")
    return redirect(url_for("main.admin_dashboard", tenant_slug=tenant.slug))


@main_bp.route("/t/<tenant_slug>/admin/cabecalho-imagem", methods=["POST"])
@login_required
@tenant_member_required
@admin_required
@subscription_required
def atualizar_imagem_cabecalho(tenant):
    image_file = request.files.get("hero_image")
    if image_file is None or not image_file.filename:
        flash("Selecione uma imagem para o cabecalho.", "error")
        return redirect(url_for("main.admin_dashboard", tenant_slug=tenant.slug))

    filename = secure_filename(image_file.filename)
    extension = Path(filename).suffix.lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
        flash("Formato invalido. Use JPG, PNG ou WEBP.", "error")
        return redirect(url_for("main.admin_dashboard", tenant_slug=tenant.slug))

    upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
    upload_dir.mkdir(parents=True, exist_ok=True)
    final_name = f"tenant-{tenant.id}-hero{extension}"

    if tenant.hero_image and tenant.hero_image != final_name:
        previous_file = upload_dir / tenant.hero_image
        if previous_file.exists():
            previous_file.unlink()

    image_file.save(upload_dir / final_name)
    tenant.hero_image = final_name
    database.session.commit()
    flash("Imagem do cabecalho atualizada.", "success")
    return redirect(url_for("main.admin_dashboard", tenant_slug=tenant.slug))


@main_bp.route("/t/<tenant_slug>/admin/barbeiros/novo", methods=["POST"])
@login_required
@tenant_member_required
@admin_required
@subscription_required
def criar_barbeiro(tenant):
    form = BarberForm()
    if form.validate_on_submit():
        barber = Barber(
            tenant_id=tenant.id,
            nome=form.nome.data,
            especialidade=form.especialidade.data,
            icone=slugify_text(form.nome.data)[:2].upper() or "BR",
            slot_interval_minutes=form.slot_interval_minutes.data,
            ativo=True,
        )
        database.session.add(barber)
        database.session.flush()
        for weekday in range(6):
            for slot_label in get_standard_slot_labels(barber.slot_interval_minutes):
                database.session.add(
                    BarberWorkingSlot(
                        tenant_id=tenant.id,
                        barbeiro_id=barber.id,
                        weekday=weekday,
                        hora_referencia=datetime.strptime(slot_label, "%H:%M").time(),
                    )
                )
        database.session.commit()
        flash("Barbeiro criado.", "success")
    else:
        flash("Nao foi possivel criar o barbeiro.", "error")
    return redirect(url_for("main.admin_dashboard", tenant_slug=tenant.slug))


@main_bp.route("/t/<tenant_slug>/admin/servicos/novo", methods=["POST"])
@login_required
@tenant_member_required
@admin_required
@subscription_required
def criar_servico(tenant):
    form = ServiceForm()
    if form.validate_on_submit():
        slug = slugify_text(form.nome.data)
        existing = Service.query.filter_by(tenant_id=tenant.id, slug=slug).first()
        servico = Service(
            tenant_id=tenant.id,
            nome=form.nome.data,
            slug=f"{slug}-{Service.query.filter_by(tenant_id=tenant.id).count() + 1}" if existing else slug,
            valor=form.valor.data,
            duracao_minutos=form.duracao_minutos.data,
            icone=form.icone.data.upper(),
        )
        database.session.add(servico)
        database.session.commit()
        flash("Servico criado.", "success")
    else:
        flash("Nao foi possivel criar o servico.", "error")
    return redirect(url_for("main.admin_dashboard", tenant_slug=tenant.slug))


@main_bp.route("/t/<tenant_slug>/admin/servicos/<int:service_id>/editar", methods=["POST"])
@login_required
@tenant_member_required
@admin_required
@subscription_required
def editar_servico(tenant, service_id):
    servico = Service.query.filter_by(id=service_id, tenant_id=tenant.id).first_or_404()
    form = ServiceForm()
    if form.validate_on_submit():
        new_slug = slugify_text(form.nome.data)
        duplicate = Service.query.filter(
            Service.id != servico.id,
            Service.tenant_id == tenant.id,
            Service.slug == new_slug,
        ).first()
        servico.nome = form.nome.data
        servico.slug = new_slug if not duplicate or new_slug == servico.slug else f"{new_slug}-{servico.id}"
        servico.valor = form.valor.data
        servico.duracao_minutos = form.duracao_minutos.data
        servico.icone = form.icone.data.upper()
        database.session.commit()
        flash("Servico atualizado.", "success")
    else:
        flash("Nao foi possivel atualizar o servico.", "error")
    return redirect(url_for("main.admin_dashboard", tenant_slug=tenant.slug))


@main_bp.route("/t/<tenant_slug>/admin/servicos/<int:service_id>/excluir", methods=["POST"])
@login_required
@tenant_member_required
@admin_required
@subscription_required
def excluir_servico(tenant, service_id):
    servico = Service.query.filter_by(id=service_id, tenant_id=tenant.id).first_or_404()
    if servico.agendamentos:
        servico.ativo = False
        flash("Servico desativado porque ja possui agendamentos.", "success")
    else:
        database.session.delete(servico)
        flash("Servico excluido.", "success")
    database.session.commit()
    return redirect(url_for("main.admin_dashboard", tenant_slug=tenant.slug))


@main_bp.route("/t/<tenant_slug>/admin/barbeiros/<int:barber_id>/editar", methods=["POST"])
@login_required
@tenant_member_required
@admin_required
@subscription_required
def editar_barbeiro(tenant, barber_id):
    barbeiro = Barber.query.filter_by(id=barber_id, tenant_id=tenant.id).first_or_404()
    form = BarberForm()
    if form.validate_on_submit():
        interval_changed = barbeiro.slot_interval_minutes != form.slot_interval_minutes.data
        barbeiro.nome = form.nome.data
        barbeiro.especialidade = form.especialidade.data
        barbeiro.slot_interval_minutes = form.slot_interval_minutes.data
        if interval_changed:
            BarberWorkingSlot.query.filter_by(tenant_id=tenant.id, barbeiro_id=barbeiro.id).delete()
            for weekday in range(6):
                for slot_label in get_standard_slot_labels(barbeiro.slot_interval_minutes):
                    database.session.add(
                        BarberWorkingSlot(
                            tenant_id=tenant.id,
                            barbeiro_id=barbeiro.id,
                            weekday=weekday,
                            hora_referencia=datetime.strptime(slot_label, "%H:%M").time(),
                        )
                    )
        database.session.commit()
        if interval_changed:
            flash("Barbeiro atualizado com nova grade de horarios.", "success")
        else:
            flash("Barbeiro atualizado.", "success")
    else:
        flash("Nao foi possivel atualizar.", "error")
    return redirect(url_for("main.admin_dashboard", tenant_slug=tenant.slug))


@main_bp.route("/t/<tenant_slug>/admin/barbeiros/<int:barber_id>/excluir", methods=["POST"])
@login_required
@tenant_member_required
@admin_required
@subscription_required
def excluir_barbeiro(tenant, barber_id):
    barbeiro = Barber.query.filter_by(id=barber_id, tenant_id=tenant.id).first_or_404()
    if barbeiro.agendamentos:
        barbeiro.ativo = False
    else:
        BarberWorkingSlot.query.filter_by(tenant_id=tenant.id, barbeiro_id=barbeiro.id).delete()
        BarberUnavailableSlot.query.filter_by(tenant_id=tenant.id, barbeiro_id=barbeiro.id).delete()
        database.session.delete(barbeiro)
    database.session.commit()
    flash("Barbeiro removido da agenda.", "success")
    return redirect(url_for("main.admin_dashboard", tenant_slug=tenant.slug))


@main_bp.route("/t/<tenant_slug>/admin/agendamentos/<int:appointment_id>/status", methods=["POST"])
@login_required
@tenant_member_required
@admin_required
@subscription_required
def atualizar_status_agendamento(tenant, appointment_id):
    agendamento = Appointment.query.filter_by(id=appointment_id, tenant_id=tenant.id).first_or_404()
    form = AppointmentStatusForm()
    if form.validate_on_submit():
        agendamento.status = form.status.data
        database.session.commit()
        flash("Status atualizado.", "success")
    else:
        flash("Nao foi possivel atualizar o status.", "error")
    return redirect(url_for("main.admin_dashboard", tenant_slug=tenant.slug))


@main_bp.route("/t/<tenant_slug>/admin/agendamentos/<int:appointment_id>/excluir", methods=["POST"])
@login_required
@tenant_member_required
@admin_required
@subscription_required
def excluir_agendamento(tenant, appointment_id):
    agendamento = Appointment.query.filter_by(id=appointment_id, tenant_id=tenant.id).first_or_404()
    database.session.delete(agendamento)
    database.session.commit()
    flash("Agendamento excluido.", "success")
    return redirect(url_for("main.admin_dashboard", tenant_slug=tenant.slug))


@main_bp.app_errorhandler(403)
def forbidden(_error):
    return render_template("403.html"), 403
@main_bp.route("/t/<tenant_slug>/admin/agenda/slot", methods=["POST"])
@login_required
@tenant_member_required
@admin_required
@subscription_required
def toggle_slot_disponibilidade(tenant):
    form = SlotAvailabilityForm()
    if not form.validate_on_submit():
        flash("Nao foi possivel atualizar o slot.", "error")
        return redirect(url_for("main.admin_dashboard", tenant_slug=tenant.slug))

    barber_id = int(form.barbeiro_id.data)
    selected_day = datetime.strptime(form.data_referencia.data, "%Y-%m-%d").date()
    selected_time = datetime.strptime(form.hora_referencia.data, "%H:%M").time()
    barber = Barber.query.filter_by(id=barber_id, tenant_id=tenant.id, ativo=True).first_or_404()
    slot_interval_minutes = get_barber_slot_interval(tenant.id, barber.id)
    existing_slot = BarberUnavailableSlot.query.filter_by(
        tenant_id=tenant.id,
        barbeiro_id=barber.id,
        data_referencia=selected_day,
        hora_referencia=selected_time,
    ).first()

    if existing_slot is None:
        selected_slot_label = form.hora_referencia.data
        day_appointments = Appointment.query.filter_by(
            tenant_id=tenant.id,
            barbeiro_id=barber.id,
            data_agendamento=selected_day,
        ).all()
        busy_labels = set()
        for appointment in day_appointments:
            start_at = datetime.combine(selected_day, appointment.hora_agendamento)
            busy_labels.add(appointment.hora_agendamento.strftime("%H:%M"))
            for index in range(1, appointment_slot_span(appointment, slot_interval_minutes)):
                busy_labels.add((start_at + timedelta(minutes=slot_interval_minutes * index)).time().strftime("%H:%M"))
        overlap_exists = selected_slot_label in busy_labels
        if overlap_exists:
            flash("Este slot ja possui agendamento e nao pode ser bloqueado.", "error")
        else:
            database.session.add(
                BarberUnavailableSlot(
                    tenant_id=tenant.id,
                    barbeiro_id=barber.id,
                    data_referencia=selected_day,
                    hora_referencia=selected_time,
                )
            )
            database.session.commit()
            flash("Slot bloqueado para o cliente.", "success")
    else:
        database.session.delete(existing_slot)
        database.session.commit()
        flash("Slot liberado para o cliente.", "success")

    return redirect(
        url_for(
            "main.admin_dashboard",
            tenant_slug=tenant.slug,
            day=selected_day.isoformat(),
            barbeiro_id=barber.id,
        )
    )


@main_bp.route("/t/<tenant_slug>/admin/agenda/horarios", methods=["POST"])
@login_required
@tenant_member_required
@admin_required
@subscription_required
def toggle_horario_atendimento(tenant):
    form = BarberWorkingSlotForm()
    if not form.validate_on_submit():
        flash("Nao foi possivel atualizar o horario de atendimento.", "error")
        return redirect(url_for("main.admin_dashboard", tenant_slug=tenant.slug))

    barber_id = int(form.barbeiro_id.data)
    weekday = int(form.weekday.data)
    selected_time = datetime.strptime(form.hora_referencia.data, "%H:%M").time()
    barber = Barber.query.filter_by(id=barber_id, tenant_id=tenant.id, ativo=True).first_or_404()

    existing_slot = BarberWorkingSlot.query.filter_by(
        tenant_id=tenant.id,
        barbeiro_id=barber.id,
        weekday=weekday,
        hora_referencia=selected_time,
    ).first()

    if existing_slot is None:
        database.session.add(
            BarberWorkingSlot(
                tenant_id=tenant.id,
                barbeiro_id=barber.id,
                weekday=weekday,
                hora_referencia=selected_time,
            )
        )
        database.session.commit()
        flash("Horario de atendimento liberado para este slot.", "success")
    else:
        database.session.delete(existing_slot)
        database.session.commit()
        flash("Horario de atendimento removido deste slot.", "success")

    return redirect(
        url_for(
            "main.admin_dashboard",
            tenant_slug=tenant.slug,
            barbeiro_id=barber.id,
        )
    )
