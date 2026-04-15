import os
import re
import unicodedata
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, redirect, request, url_for
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from werkzeug.security import generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
DEFAULT_DATABASE_PATH = PROJECT_DIR / "instance" / "comunidade.db"
DEFAULT_UPLOAD_DIR = BASE_DIR / "static" / "fotos_posts"

load_dotenv(PROJECT_DIR / ".env")

database = SQLAlchemy()
login_manager = LoginManager()
DEFAULT_BARBER_MODELS = (
    ("Sergio Lima", "Corte e acabamento", "SL"),
    ("Mateus Lima", "Barba e estilo", "ML"),
    ("Barbeiro Modelo", "Cortes classicos e degrade", "BM"),
)
AGENDA_SLOT_MINUTES = 45
DEFAULT_SERVICE_MODELS = (
    ("Corte", "CT", 35.00, 45),
    ("Barba", "BR", 25.00, 45),
    ("Corte + barba", "CB", 55.00, 90),
    ("Acabamento", "AC", 20.00, 45),
    ("Sobrancelha", "SB", 15.00, 45),
)


def normalize_database_url(database_url):
    if not database_url:
        return f"sqlite:///{DEFAULT_DATABASE_PATH.as_posix()}"
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql://") and "+psycopg" not in database_url:
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


def env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def create_app():
    app = Flask(__name__)

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

    database_url = normalize_database_url(os.getenv("DATABASE_URL"))
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    app.config["UPLOAD_FOLDER"] = os.getenv(
        "UPLOAD_FOLDER",
        str(DEFAULT_UPLOAD_DIR),
    )
    app.config["ASAAS_WEBHOOK_TOKEN"] = os.getenv("ASAAS_WEBHOOK_TOKEN", "")
    app.config["PREFERRED_URL_SCHEME"] = "https" if env_flag("FORCE_HTTPS", default=False) else "http"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
    app.config["SESSION_COOKIE_SECURE"] = env_flag("SESSION_COOKIE_SECURE", default=env_flag("FORCE_HTTPS", False))
    app.config["REMEMBER_COOKIE_SECURE"] = app.config["SESSION_COOKIE_SECURE"]
    app.config["REMEMBER_COOKIE_HTTPONLY"] = True
    app.config["REMEMBER_COOKIE_SAMESITE"] = app.config["SESSION_COOKIE_SAMESITE"]

    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///"):
        DEFAULT_DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    database.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "main.homepage"
    login_manager.login_message = "Faca login para continuar."

    @login_manager.unauthorized_handler
    def unauthorized():
        tenant_slug = request.view_args.get("tenant_slug") if request.view_args else None
        if tenant_slug:
            return redirect(url_for("main.acesso_cliente", tenant_slug=tenant_slug))
        return redirect(url_for("main.homepage"))

    from Nerzilus import models
    from Nerzilus.routes import main_bp

    app.register_blueprint(main_bp)

    with app.app_context():
        try:
            database.create_all()
            ensure_schema_updates()
            seed_initial_data()
        except OperationalError:
            database.drop_all()
            database.create_all()
            ensure_schema_updates()
            seed_initial_data()

    return app


def seed_initial_data():
    from Nerzilus.models import Tenant, User
    from Nerzilus.billing import ensure_trial_subscription

    default_tenant_slug = os.getenv("DEFAULT_TENANT_SLUG", "nerzilus-studio")
    default_tenant_name = os.getenv("DEFAULT_TENANT_NAME", "Sergio Lima Barber e Salao")
    admin_username = os.getenv("ADMIN_USERNAME", "sergioadmin").lower()
    admin_password = os.getenv("ADMIN_PASSWORD", "admin123")
    admin_email = os.getenv("ADMIN_EMAIL", "sergioadmin@nerzilus.local").lower()

    tenant = Tenant.query.filter_by(slug=default_tenant_slug).first()
    if tenant is None:
        tenant = Tenant(
            nome=default_tenant_name,
            slug=default_tenant_slug,
            business_type="barbershop",
            tema="dark",
            cor_primaria="#d4a373",
            whatsapp="5511999999999",
            notificacoes_whatsapp=False,
        )
        database.session.add(tenant)
        database.session.commit()
    elif tenant.nome != default_tenant_name:
        tenant.nome = default_tenant_name
        database.session.commit()

    if not User.query.filter_by(tenant_id=tenant.id, username=admin_username, is_admin=True).first():
        admin = User(
            tenant_id=tenant.id,
            nome="Administrador",
            telefone="0000000000",
            email=admin_email,
            username=admin_username,
            senha_hash=generate_password_hash(admin_password),
            is_admin=True,
        )
        database.session.add(admin)
        try:
            database.session.commit()
        except IntegrityError:
            database.session.rollback()

    admin = User.query.filter_by(tenant_id=tenant.id, username=admin_username, is_admin=True).first()
    if admin and not admin.email:
        admin.email = admin_email
        database.session.commit()

    if admin is not None:
        ensure_trial_subscription(tenant, admin)

    seed_tenant_defaults(tenant.id)
    database.session.commit()


def ensure_schema_updates():
    inspector = inspect(database.engine)
    tenant_columns = {column["name"] for column in inspector.get_columns("tenant")}
    if "hero_image" not in tenant_columns:
        with database.engine.begin() as connection:
            connection.execute(text("ALTER TABLE tenant ADD COLUMN hero_image VARCHAR(255)"))
    if "stripe_customer_id" not in tenant_columns:
        with database.engine.begin() as connection:
            connection.execute(text("ALTER TABLE tenant ADD COLUMN stripe_customer_id VARCHAR(120)"))
    if "asaas_customer_id" not in tenant_columns:
        with database.engine.begin() as connection:
            connection.execute(text("ALTER TABLE tenant ADD COLUMN asaas_customer_id VARCHAR(120)"))

    user_columns = {column["name"] for column in inspector.get_columns("user")}
    if "email" not in user_columns:
        with database.engine.begin() as connection:
            connection.execute(text("ALTER TABLE user ADD COLUMN email VARCHAR(255)"))
    if "cpf_cnpj" not in user_columns:
        with database.engine.begin() as connection:
            connection.execute(text("ALTER TABLE user ADD COLUMN cpf_cnpj VARCHAR(20)"))

    barber_columns = {column["name"] for column in inspector.get_columns("barber")}
    if "slot_interval_minutes" not in barber_columns:
        with database.engine.begin() as connection:
            connection.execute(text("ALTER TABLE barber ADD COLUMN slot_interval_minutes INTEGER DEFAULT 45"))

    subscription_columns = {column["name"] for column in inspector.get_columns("subscription")}
    if "asaas_customer_id" not in subscription_columns:
        with database.engine.begin() as connection:
            connection.execute(text("ALTER TABLE subscription ADD COLUMN asaas_customer_id VARCHAR(120)"))
    if "asaas_subscription_id" not in subscription_columns:
        with database.engine.begin() as connection:
            connection.execute(text("ALTER TABLE subscription ADD COLUMN asaas_subscription_id VARCHAR(120)"))
    if "billing_method" not in subscription_columns:
        with database.engine.begin() as connection:
            connection.execute(text("ALTER TABLE subscription ADD COLUMN billing_method VARCHAR(20)"))
    if "next_due_date" not in subscription_columns:
        with database.engine.begin() as connection:
            connection.execute(text("ALTER TABLE subscription ADD COLUMN next_due_date DATETIME"))
    if "last_payment_id" not in subscription_columns:
        with database.engine.begin() as connection:
            connection.execute(text("ALTER TABLE subscription ADD COLUMN last_payment_id VARCHAR(120)"))
    if "last_invoice_url" not in subscription_columns:
        with database.engine.begin() as connection:
            connection.execute(text("ALTER TABLE subscription ADD COLUMN last_invoice_url VARCHAR(255)"))
    if "pix_qr_code" not in subscription_columns:
        with database.engine.begin() as connection:
            connection.execute(text("ALTER TABLE subscription ADD COLUMN pix_qr_code TEXT"))
    if "pix_copy_paste" not in subscription_columns:
        with database.engine.begin() as connection:
            connection.execute(text("ALTER TABLE subscription ADD COLUMN pix_copy_paste TEXT"))

    payment_event_columns = {column["name"] for column in inspector.get_columns("payment_event_log")}
    if "external_event_id" not in payment_event_columns:
        with database.engine.begin() as connection:
            connection.execute(text("ALTER TABLE payment_event_log ADD COLUMN external_event_id VARCHAR(120)"))


def seed_tenant_defaults(tenant_id):
    from Nerzilus.models import Barber, BarberWorkingSlot, Service

    existing_barbers = Barber.query.filter_by(tenant_id=tenant_id).order_by(Barber.nome.asc()).all()
    existing_names = [barber.nome for barber in existing_barbers]

    if existing_names == ["Caio Mendes", "Enzo Alves", "Rafael Costa"]:
        for barber in existing_barbers:
            database.session.delete(barber)
        database.session.commit()
        existing_barbers = []

    existing_barbers_by_name = {barber.nome: barber for barber in existing_barbers}
    for nome, especialidade, icone in DEFAULT_BARBER_MODELS:
        barber = existing_barbers_by_name.get(nome)
        if barber is None:
            barber = Barber(tenant_id=tenant_id, nome=nome, especialidade=especialidade, icone=icone, slot_interval_minutes=45)
            database.session.add(barber)
            continue
        if not barber.especialidade:
            barber.especialidade = especialidade
        if not barber.icone:
            barber.icone = icone
        if not barber.slot_interval_minutes:
            barber.slot_interval_minutes = 45

    existing_services = Service.query.filter_by(tenant_id=tenant_id).all()
    existing_services_by_name = {service.nome: service for service in existing_services}
    for nome, icone, valor, duracao in DEFAULT_SERVICE_MODELS:
        service = existing_services_by_name.get(nome)
        if service is None:
            database.session.add(
                Service(
                    tenant_id=tenant_id,
                    nome=nome,
                    slug=slugify_text(nome),
                    valor=valor,
                    duracao_minutos=duracao,
                    icone=icone,
                )
            )
            continue
        if not service.slug:
            service.slug = slugify_text(nome)
        if not service.icone:
            service.icone = icone
        if not service.valor:
            service.valor = valor
        if not service.duracao_minutos:
            service.duracao_minutos = duracao
        elif service.nome in {
            "Corte",
            "Barba",
            "Corte + barba",
            "Acabamento",
            "Sobrancelha",
        }:
            service.duracao_minutos = duracao

    database.session.flush()
    barbers = Barber.query.filter_by(tenant_id=tenant_id).all()
    for barber in barbers:
        has_working_slots = BarberWorkingSlot.query.filter_by(tenant_id=tenant_id, barbeiro_id=barber.id).first()
        if has_working_slots:
            continue
        base_slots = []
        for _, _, start_time, end_time in (
            ("manha", "Manha", datetime_time(hour=9, minute=0), datetime_time(hour=12, minute=0)),
            ("tarde", "Tarde", datetime_time(hour=14, minute=0), datetime_time(hour=21, minute=0)),
        ):
            current_slot = datetime.combine(date.today(), start_time)
            final_slot = datetime.combine(date.today(), end_time)
            while current_slot <= final_slot:
                base_slots.append(current_slot.time())
                current_slot += timedelta(minutes=barber.slot_interval_minutes or AGENDA_SLOT_MINUTES)
        for weekday in range(6):
            for slot_time in base_slots:
                database.session.add(
                    BarberWorkingSlot(
                        tenant_id=tenant_id,
                        barbeiro_id=barber.id,
                        weekday=weekday,
                        hora_referencia=slot_time,
                    )
                )


def slugify_text(value):
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")


app = create_app()
