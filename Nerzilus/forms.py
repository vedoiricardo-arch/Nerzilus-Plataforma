from datetime import date

from flask_wtf import FlaskForm
from wtforms import DateField, HiddenField, PasswordField, RadioField, SelectField, StringField, SubmitField, TelField, TimeField
from wtforms.validators import DataRequired, Email, Length, NumberRange, ValidationError
from wtforms.fields.numeric import DecimalField, IntegerField

from Nerzilus import slugify_text
from Nerzilus.models import Tenant, User

AGENDA_SLOT_MINUTES = 45


def normalize_phone(value):
    digits = "".join(char for char in str(value or "") if char.isdigit())
    return digits or None


def normalize_document(value):
    digits = "".join(char for char in str(value or "") if char.isdigit())
    return digits or ""

PAYMENT_CHOICES = [
    ("pix", "Pix pelo app"),
    ("local", "Pagar no local"),
]

STATUS_CHOICES = [
    ("pendente", "Pendente"),
    ("confirmado", "Confirmado"),
    ("cancelado", "Cancelado"),
    ("concluido", "Concluido"),
]

THEME_CHOICES = [
    ("dark", "Dark mode"),
    ("light", "Light mode"),
    ("pink", "Pink mode"),
]

class ClientAccessForm(FlaskForm):
    nome = StringField("Nome", validators=[DataRequired(), Length(min=2, max=120)])
    telefone = TelField("Telefone", validators=[DataRequired(), Length(min=8, max=20)])
    botao_confirmacao = SubmitField("Continuar")

    def validate_telefone(self, telefone):
        telefone.data = telefone.data.strip()


class AdminLoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(min=3, max=80)])
    senha = PasswordField("Senha", validators=[DataRequired(), Length(min=4, max=120)])
    botao_confirmacao = SubmitField("Entrar")

    def validate_username(self, field):
        field.data = field.data.strip().lower()


class PlatformSignupForm(FlaskForm):
    nome_barbearia = StringField("Nome da barbearia", validators=[DataRequired(), Length(min=2, max=120)])
    slug = StringField("Slug da barbearia", validators=[DataRequired(), Length(min=2, max=80)])
    email = StringField("Email admin", validators=[DataRequired(), Email(), Length(max=255)])
    username = StringField("Username admin", validators=[DataRequired(), Length(min=3, max=80)])
    senha = PasswordField("Senha admin", validators=[DataRequired(), Length(min=4, max=120)])
    whatsapp = TelField("WhatsApp", validators=[Length(max=30)])
    cpf_cnpj = StringField("CPF ou CNPJ", validators=[Length(max=20)])
    botao_confirmacao = SubmitField("Criar conta")

    def validate_slug(self, field):
        field.data = slugify_text(field.data)
        if not field.data:
            raise ValidationError("Informe um slug valido.")
        if Tenant.query.filter_by(slug=field.data).first():
            raise ValidationError("Este slug ja esta em uso.")

    def validate_username(self, field):
        field.data = field.data.strip().lower()

    def validate_email(self, field):
        field.data = field.data.strip().lower()
        if User.query.filter(User.email == field.data, User.is_admin.is_(True)).first():
            raise ValidationError("Este email administrativo ja esta em uso.")

    def validate_nome_barbearia(self, field):
        field.data = field.data.strip()

    def validate_whatsapp(self, field):
        field.data = normalize_phone(field.data) or ""

    def validate_cpf_cnpj(self, field):
        field.data = normalize_document(field.data)
        if field.data and len(field.data) not in {11, 14}:
            raise ValidationError("Informe um CPF ou CNPJ valido.")


class BarberForm(FlaskForm):
    nome = StringField("Nome", validators=[DataRequired(), Length(min=2, max=120)])
    especialidade = StringField("Especialidade", validators=[DataRequired(), Length(min=2, max=120)])
    botao_confirmacao = SubmitField("Salvar")


class ServiceForm(FlaskForm):
    nome = StringField("Servico", validators=[DataRequired(), Length(min=2, max=120)])
    valor = DecimalField("Valor", places=2, validators=[DataRequired(), NumberRange(min=0)])
    duracao_minutos = IntegerField("Duracao em minutos", validators=[DataRequired(), NumberRange(min=5, max=480)])
    icone = StringField("Icone", validators=[DataRequired(), Length(min=1, max=4)])
    botao_confirmacao = SubmitField("Salvar")

    def validate_duracao_minutos(self, field):
        if field.data % AGENDA_SLOT_MINUTES != 0:
            raise ValidationError(f"A duracao deve ser em multiplos de {AGENDA_SLOT_MINUTES} minutos.")


class AppointmentForm(FlaskForm):
    barbeiro_id = SelectField("Barbeiro", coerce=int, validators=[DataRequired()])
    servico_id = RadioField("Servico", coerce=int, choices=[], validators=[DataRequired()])
    forma_pagamento = RadioField("Pagamento", choices=PAYMENT_CHOICES, validators=[DataRequired()], default="local")
    data_agendamento = DateField("Data", format="%Y-%m-%d", validators=[DataRequired()])
    hora_agendamento = TimeField("Hora", format="%H:%M", validators=[DataRequired()])
    botao_confirmacao = SubmitField("Agendar")

    def validate_data_agendamento(self, field):
        if field.data < date.today():
            raise ValidationError("Escolha uma data valida.")


class AppointmentStatusForm(FlaskForm):
    status = SelectField("Status", choices=STATUS_CHOICES, validators=[DataRequired()])
    botao_confirmacao = SubmitField("Salvar")


class SlotAvailabilityForm(FlaskForm):
    barbeiro_id = HiddenField(validators=[DataRequired()])
    data_referencia = HiddenField(validators=[DataRequired()])
    hora_referencia = HiddenField(validators=[DataRequired()])
    botao_confirmacao = SubmitField("Atualizar")


class TenantWhatsAppForm(FlaskForm):
    whatsapp = TelField("WhatsApp da barbearia", validators=[Length(max=30)])
    botao_confirmacao = SubmitField("Salvar WhatsApp")

    def validate_whatsapp(self, field):
        field.data = normalize_phone(field.data) or ""


class TenantThemeForm(FlaskForm):
    tema = RadioField("Tema da plataforma", choices=THEME_CHOICES, validators=[DataRequired()])
    botao_confirmacao = SubmitField("Salvar tema")

    def validate_tema(self, field):
        if field.data not in {choice[0] for choice in THEME_CHOICES}:
            raise ValidationError("Tema invalido.")


class BillingCheckoutForm(FlaskForm):
    billing_interval = HiddenField(validators=[DataRequired()])
    billing_method = HiddenField(validators=[DataRequired()])
    botao_confirmacao = SubmitField("Assinar agora")

    def validate_billing_interval(self, field):
        if field.data not in {"monthly", "yearly"}:
            raise ValidationError("Periodo de cobranca invalido.")

    def validate_billing_method(self, field):
        if field.data not in {"PIX", "CREDIT_CARD"}:
            raise ValidationError("Metodo de pagamento invalido.")


class BillingManagementForm(FlaskForm):
    botao_confirmacao = SubmitField("Atualizar assinatura")


class BillingCancelForm(FlaskForm):
    botao_confirmacao = SubmitField("Cancelar no proximo ciclo")


class BillingCustomerForm(FlaskForm):
    cpf_cnpj = StringField("CPF ou CNPJ do responsavel", validators=[DataRequired(), Length(max=20)])
    botao_confirmacao = SubmitField("Salvar dados de cobranca")

    def validate_cpf_cnpj(self, field):
        field.data = normalize_document(field.data)
        if len(field.data) not in {11, 14}:
            raise ValidationError("Informe um CPF ou CNPJ valido.")
