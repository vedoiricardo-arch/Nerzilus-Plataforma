from datetime import date, time, timedelta
import os
import unittest
from unittest.mock import patch


class AppRoutesTestCase(unittest.TestCase):
    def setUp(self):
        from Nerzilus import app, database

        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        app.config["ASAAS_WEBHOOK_TOKEN"] = "asaas_test_token"

        self.app = app
        self.client = app.test_client()

        with app.app_context():
            database.drop_all()
            database.create_all()

            from Nerzilus import seed_initial_data
            from Nerzilus.models import Service, Tenant, User

            seed_initial_data()
            admin = User.query.filter_by(username="sergioadmin", is_admin=True).first()
            admin.cpf_cnpj = "12345678901"
            database.session.commit()

            outro_tenant = Tenant(nome="Salao Rosa", slug="salao-rosa", business_type="salon")
            database.session.add(outro_tenant)
            database.session.commit()
            database.session.add(
                User(
                    tenant_id=outro_tenant.id,
                    nome="Cliente Rosa",
                    telefone="11888888888",
                    is_admin=False,
                )
            )
            database.session.add(
                Service(
                    tenant_id=outro_tenant.id,
                    nome="Escova",
                    slug="escova",
                    valor=70,
                    duracao_minutos=30,
                    icone="ES",
                )
            )
            database.session.commit()

    def login_admin(self):
        return self.client.post(
            "/",
            data={
                "login-username": "sergioadmin",
                "login-senha": "admin123",
                "login-botao_confirmacao": True,
            },
            follow_redirects=True,
        )

    def test_homepage_loads(self):
        resposta = self.client.get("/")

        self.assertEqual(resposta.status_code, 200)
        self.assertIn(b"Nerzilus", resposta.data)
        self.assertIn("ACESSO AO MEU NEGÓCIO".encode("utf-8"), resposta.data)
        self.assertIn(b"Criar conta", resposta.data)
        self.assertNotIn(b"Nome do seu negocio", resposta.data)

    def test_homepage_creates_new_barbershop_account_with_trial_subscription(self):
        resposta = self.client.post(
            "/",
            data={
                "signup-nome_barbearia": "Barbearia Fenix",
                "signup-slug": "barbearia-fenix",
                "signup-email": "fenix@barbearia.com",
                "signup-username": "fenixadmin",
                "signup-senha": "123456",
                "signup-whatsapp": "5551997777777",
                "signup-cpf_cnpj": "12345678901",
                "signup-botao_confirmacao": True,
            },
            follow_redirects=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertIn(b"Conta criada na NerzilusBee", resposta.data)
        self.assertIn("Painel de assinatura".encode("utf-8"), resposta.data)

        with self.app.app_context():
            from Nerzilus.models import Barber, Service, Subscription, Tenant, User

            tenant = Tenant.query.filter_by(slug="barbearia-fenix").first()
            admin = User.query.filter_by(tenant_id=tenant.id, username="fenixadmin", is_admin=True).first()
            subscription = Subscription.query.filter_by(tenant_id=tenant.id).first()

            self.assertIsNotNone(tenant)
            self.assertIsNotNone(admin)
            self.assertIsNotNone(subscription)
            self.assertEqual(admin.email, "fenix@barbearia.com")
            self.assertEqual(subscription.status, "trialing")
            self.assertEqual(Barber.query.filter_by(tenant_id=tenant.id).count(), 3)
            self.assertEqual(Service.query.filter_by(tenant_id=tenant.id).count(), 5)

    def test_homepage_reveals_signup_form_when_requested(self):
        resposta = self.client.get("/?criar_conta=1")

        self.assertEqual(resposta.status_code, 200)
        self.assertIn(b"Nome do seu negocio", resposta.data)
        self.assertIn("Email do admin".encode("utf-8"), resposta.data)

    def test_homepage_logs_admin_by_username_and_password(self):
        resposta = self.login_admin()
        conteudo = resposta.get_data(as_text=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("SERGIO LIMA BARBER E SALAO", conteudo)
        self.assertIn("/t/nerzilus-studio/cliente", conteudo)
        self.assertIn("Assinatura SaaS", conteudo)

    def test_billing_page_loads_for_admin(self):
        self.login_admin()

        resposta = self.client.get("/billing", follow_redirects=True)

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Painel de assinatura".encode("utf-8"), resposta.data)
        self.assertIn("Dados do pagamento".encode("utf-8"), resposta.data)

    @patch("Nerzilus.routes.create_checkout_session")
    def test_create_checkout_session_route_redirects_to_asaas_url(self, mocked_checkout):
        class SessionStub:
            url = "https://sandbox.asaas.com/i/pay_123"

        mocked_checkout.return_value = SessionStub()
        self.login_admin()

        resposta = self.client.post(
            "/create-checkout-session",
            data={"billing_interval": "monthly", "billing_method": "PIX", "botao_confirmacao": True},
            follow_redirects=False,
        )

        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(resposta.headers["Location"], "https://sandbox.asaas.com/i/pay_123")
        mocked_checkout.assert_called_once()

    @patch("Nerzilus.routes.cancel_subscription_at_period_end")
    def test_billing_cancel_route_requests_subscription_cancellation(self, mocked_cancel):
        self.login_admin()

        resposta = self.client.post(
            "/billing/cancel",
            data={"botao_confirmacao": True},
            follow_redirects=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Cancelamento solicitado no Asaas".encode("utf-8"), resposta.data)
        mocked_cancel.assert_called_once()

    @patch("Nerzilus.billing.get_subscription_from_asaas")
    def test_webhook_payment_confirmed_updates_subscription(self, mocked_subscription_fetch):
        with self.app.app_context():
            from Nerzilus.models import Subscription, Tenant, User

            tenant = Tenant.query.filter_by(slug="nerzilus-studio").first()
            admin = User.query.filter_by(tenant_id=tenant.id, username="sergioadmin").first()
            subscription = Subscription.query.filter_by(tenant_id=tenant.id).first()
            self.assertIsNotNone(subscription)

            mocked_subscription_fetch.return_value = {
                "id": "sub_asaas_123",
                "customer": "cus_asaas_123",
                "status": "ACTIVE",
                "billingType": "PIX",
                "cycle": "MONTHLY",
                "nextDueDate": "2026-05-22",
            }

        resposta = self.client.post(
            "/webhook/asaas",
            json={
                "id": "evt_asaas_123",
                "event": "PAYMENT_CONFIRMED",
                "payment": {
                    "id": "pay_123",
                    "customer": "cus_asaas_123",
                    "subscription": "sub_asaas_123",
                    "status": "RECEIVED",
                    "billingType": "PIX",
                    "invoiceUrl": "https://sandbox.asaas.com/i/pay_123",
                    "pixTransaction": "0002012636pix-copia-cola",
                    "externalReference": f"tenant:{tenant.id}:user:{admin.id}",
                },
            },
            headers={"asaas-access-token": "asaas_test_token"},
        )

        self.assertEqual(resposta.status_code, 200)

        with self.app.app_context():
            from Nerzilus.models import Subscription, Tenant

            tenant = Tenant.query.filter_by(slug="nerzilus-studio").first()
            subscription = Subscription.query.filter_by(tenant_id=tenant.id).first()
            self.assertEqual(subscription.status, "active")
            self.assertEqual(subscription.asaas_customer_id, "cus_asaas_123")
            self.assertEqual(subscription.asaas_subscription_id, "sub_asaas_123")
            self.assertEqual(subscription.pix_copy_paste, "0002012636pix-copia-cola")

    def test_webhook_pix_payment_received_activates_access_without_recurring_subscription(self):
        with self.app.app_context():
            from Nerzilus import database
            from Nerzilus.models import Subscription, Tenant, User

            tenant = Tenant.query.filter_by(slug="nerzilus-studio").first()
            admin = User.query.filter_by(tenant_id=tenant.id, username="sergioadmin").first()
            subscription = Subscription.query.filter_by(tenant_id=tenant.id).first()
            subscription.status = "pending"
            subscription.billing_interval = "monthly"
            subscription.billing_method = "PIX"
            database.session.commit()
            tenant_id = tenant.id
            admin_id = admin.id

        resposta = self.client.post(
            "/webhook/asaas",
            json={
                "id": "evt_asaas_pix_123",
                "event": "PAYMENT_RECEIVED",
                "payment": {
                    "id": "pay_pix_123",
                    "customer": "cus_pix_123",
                    "status": "RECEIVED",
                    "billingType": "PIX",
                    "invoiceUrl": "https://sandbox.asaas.com/i/pay_pix_123",
                    "dueDate": "2026-04-16",
                    "externalReference": f"tenant:{tenant_id}:user:{admin_id}",
                    "payload": "000201pix-copia-cola",
                    "encodedImage": "ZmFrZS1xci1jb2Rl",
                },
            },
            headers={"asaas-access-token": "asaas_test_token"},
        )

        self.assertEqual(resposta.status_code, 200)

        with self.app.app_context():
            from Nerzilus.models import Subscription, Tenant

            tenant = Tenant.query.filter_by(slug="nerzilus-studio").first()
            subscription = Subscription.query.filter_by(tenant_id=tenant.id).first()
            self.assertEqual(subscription.status, "active")
            self.assertEqual(subscription.billing_method, "PIX")
            self.assertEqual(subscription.last_payment_id, "pay_pix_123")
            self.assertEqual(subscription.pix_copy_paste, "000201pix-copia-cola")
            self.assertEqual(subscription.pix_qr_code, "ZmFrZS1xci1jb2Rl")
            self.assertIsNotNone(subscription.current_period_end)

    def test_client_quick_access_and_booking(self):
        booking_day = date.today() + timedelta(days=1)

        with self.app.app_context():
            from Nerzilus.models import Appointment, Barber, Service, Tenant, User

            tenant = Tenant.query.filter_by(slug="nerzilus-studio").first()
            barbeiros = Barber.query.filter_by(tenant_id=tenant.id).order_by(Barber.nome.asc()).all()
            servico = Service.query.filter_by(tenant_id=tenant.id).order_by(Service.valor.asc()).first()
            barbeiro = barbeiros[0]
            self.assertEqual([barber.nome for barber in barbeiros], ["Barbeiro Modelo", "Mateus Lima", "Sergio Lima"])

        acesso = self.client.post(
            "/t/nerzilus-studio/cliente",
            data={"nome": "Cliente Teste", "telefone": "11999999999"},
            follow_redirects=True,
        )
        conteudo = acesso.get_data(as_text=True)

        self.assertEqual(acesso.status_code, 200)
        self.assertIn("Ola, Cliente Teste", conteudo)
        self.assertIn("Sergio Lima Barber e Salao", conteudo)
        self.assertIn("Agenda da manha", conteudo)
        self.assertIn("Agenda da tarde", conteudo)
        self.assertIn("09:00", conteudo)
        self.assertIn("14:00", conteudo)

        with patch("Nerzilus.routes.send_booking_whatsapp_notification") as mocked_notification:
            agendamento = self.client.post(
                "/t/nerzilus-studio/cliente/dashboard",
                data={
                    "barbeiro_id": barbeiro.id,
                    "servico_id": servico.id,
                    "data_agendamento": booking_day.isoformat(),
                    "hora_agendamento": "14:45",
                },
                follow_redirects=True,
            )

        self.assertEqual(agendamento.status_code, 200)
        self.assertIn(b"Agendamento criado", agendamento.data)
        mocked_notification.assert_called_once()

        with self.app.app_context():
            appointment = Appointment.query.first()
            cliente = User.query.filter_by(tenant_id=appointment.tenant_id, telefone="11999999999", is_admin=False).first()

            self.assertEqual(Appointment.query.count(), 1)
            self.assertEqual(appointment.status, "confirmado")
            self.assertEqual(appointment.forma_pagamento, "local")
            self.assertEqual(appointment.servico_rel.nome, servico.nome)
            self.assertIsNotNone(cliente)
            self.assertEqual(cliente.nome, "Cliente Teste")

    def test_client_contact_is_reused_and_kept_in_database(self):
        self.client.post(
            "/t/nerzilus-studio/cliente",
            data={"nome": "Cliente Original", "telefone": "11999999999"},
            follow_redirects=True,
        )
        self.client.get("/logout", follow_redirects=True)
        self.client.post(
            "/t/nerzilus-studio/cliente",
            data={"nome": "Cliente Atualizado", "telefone": "11999999999"},
            follow_redirects=True,
        )

        with self.app.app_context():
            from Nerzilus.models import Tenant, User

            tenant = Tenant.query.filter_by(slug="nerzilus-studio").first()
            clientes = User.query.filter_by(tenant_id=tenant.id, telefone="11999999999", is_admin=False).all()
            self.assertEqual(len(clientes), 1)
            self.assertEqual(clientes[0].nome, "Cliente Atualizado")

    def test_booking_whatsapp_message_has_readable_spacing_and_emojis(self):
        with self.app.app_context():
            from Nerzilus import database
            from Nerzilus.models import Appointment, Barber, Service, Tenant, User
            from Nerzilus.notifications import build_booking_message

            tenant = Tenant.query.filter_by(slug="nerzilus-studio").first()
            client = User(tenant_id=tenant.id, nome="Cliente Emoji", telefone="11999999999", is_admin=False)
            barber = Barber.query.filter_by(tenant_id=tenant.id).order_by(Barber.nome.asc()).first()
            service = Service.query.filter_by(tenant_id=tenant.id).order_by(Service.valor.asc()).first()

            database.session.add(client)
            database.session.commit()

            appointment = Appointment(
                tenant_id=tenant.id,
                cliente_id=client.id,
                barbeiro_id=barber.id,
                servico_id=service.id,
                forma_pagamento="local",
                data_agendamento=date(2026, 4, 15),
                hora_agendamento=time(14, 30),
                status="confirmado",
            )
            database.session.add(appointment)
            database.session.commit()

            message = build_booking_message(appointment)

            self.assertIn("\u2705 *Novo agendamento confirmado*", message)
            self.assertIn("\U0001F4F1 *WhatsApp:* (11) 99999-9999", message)
            self.assertIn("\U0001F488 *Barbeiro:*", message)
            self.assertIn("\u2702\uFE0F *Servico:*", message)
            self.assertIn("\u23F0 *Hora:* 14:30", message)
            self.assertIn("------------------------------", message)

    def test_admin_can_block_and_release_slot_for_client(self):
        self.client.post(
            "/t/nerzilus-studio/admin/login",
            data={"username": "sergioadmin", "senha": "admin123"},
            follow_redirects=True,
        )

        with self.app.app_context():
            from Nerzilus.models import Barber, Service

            barber = Barber.query.filter_by(nome="Barbeiro Modelo").first()
            service = Service.query.filter_by(nome="Acabamento").first()

        resposta = self.client.post(
            "/t/nerzilus-studio/admin/agenda/slot",
            data={
                "barbeiro_id": barber.id,
                "data_referencia": "2026-04-16",
                "hora_referencia": "14:00",
                "botao_confirmacao": True,
            },
            follow_redirects=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Slot bloqueado para o cliente.".encode("utf-8"), resposta.data)
        self.assertIn("Liberar slot".encode("utf-8"), resposta.data)

        self.client.post(
            "/t/nerzilus-studio/cliente",
            data={"nome": "Cliente Slot", "telefone": "11911111111"},
            follow_redirects=True,
        )
        visualizacao = self.client.get(
            f"/t/nerzilus-studio/cliente/dashboard?barbeiro_id={barber.id}&data_agendamento=2026-04-16",
            follow_redirects=True,
        )
        self.assertIn("Horario bloqueado pela barbearia".encode("utf-8"), visualizacao.data)

        tentativa = self.client.post(
            "/t/nerzilus-studio/cliente/dashboard",
            data={
                "barbeiro_id": barber.id,
                "servico_id": service.id,
                "data_agendamento": "2026-04-16",
                "hora_agendamento": "18:30",
            },
            follow_redirects=True,
        )
        self.assertIn(b"bloqueado pela barbearia", tentativa.data.lower())

    def test_admin_can_edit_barber_workday(self):
        self.client.post(
            "/t/nerzilus-studio/admin/login",
            data={"username": "sergioadmin", "senha": "admin123"},
            follow_redirects=True,
        )

        with self.app.app_context():
            from Nerzilus.models import Barber, Service

            barber = Barber.query.filter_by(nome="Barbeiro Modelo").first()
            service = Service.query.filter_by(nome="Acabamento").first()

        resposta = self.client.post(
            f"/t/nerzilus-studio/admin/barbeiros/{barber.id}/editar",
            data={
                "nome": barber.nome,
                "especialidade": barber.especialidade,
                "slot_interval_minutes": barber.slot_interval_minutes,
                "expediente_inicio": "10:00",
                "expediente_fim": "18:00",
                "botao_confirmacao": True,
            },
            follow_redirects=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Barbeiro atualizado.".encode("utf-8"), resposta.data)

        self.client.get("/logout", follow_redirects=True)
        self.client.post(
            "/t/nerzilus-studio/cliente",
            data={"nome": "Cliente Horario", "telefone": "11911112222"},
            follow_redirects=True,
        )
        visualizacao = self.client.get(
            f"/t/nerzilus-studio/cliente/dashboard?barbeiro_id={barber.id}&data_agendamento=2026-04-16",
            follow_redirects=True,
        )
        self.assertIn("Fora do horario de atendimento".encode("utf-8"), visualizacao.data)
        self.assertIn(b"10:30", visualizacao.data)

        tentativa = self.client.post(
            "/t/nerzilus-studio/cliente/dashboard",
            data={
                "barbeiro_id": barber.id,
                "servico_id": service.id,
                "data_agendamento": "2026-04-16",
                "hora_agendamento": "18:30",
            },
            follow_redirects=True,
        )
        self.assertIn(b"fora do atendimento configurado pelo admin", tentativa.data.lower())

    def test_admin_can_change_barber_slot_interval(self):
        self.client.post(
            "/t/nerzilus-studio/admin/login",
            data={"username": "sergioadmin", "senha": "admin123"},
            follow_redirects=True,
        )

        with self.app.app_context():
            from Nerzilus.models import Barber

            barber = Barber.query.filter_by(nome="Barbeiro Modelo").first()

        resposta = self.client.post(
            f"/t/nerzilus-studio/admin/barbeiros/{barber.id}/editar",
            data={
                "nome": barber.nome,
                "especialidade": barber.especialidade,
                "slot_interval_minutes": 30,
                "expediente_inicio": "09:00",
                "expediente_fim": "21:00",
                "botao_confirmacao": True,
            },
            follow_redirects=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Barbeiro atualizado.".encode("utf-8"), resposta.data)

        self.client.get("/logout", follow_redirects=True)
        self.client.post(
            "/t/nerzilus-studio/cliente",
            data={"nome": "Cliente Intervalo", "telefone": "11922223333"},
            follow_redirects=True,
        )
        visualizacao = self.client.get(
            f"/t/nerzilus-studio/cliente/dashboard?barbeiro_id={barber.id}&data_agendamento=2026-04-16",
            follow_redirects=True,
        )
        self.assertIn(b"14:30", visualizacao.data)
        self.assertNotIn(b"14:45", visualizacao.data)

        self.client.get("/logout", follow_redirects=True)
        self.client.post(
            "/t/nerzilus-studio/admin/login",
            data={"username": "sergioadmin", "senha": "admin123"},
            follow_redirects=True,
        )
        agenda_admin = self.client.get(
            f"/t/nerzilus-studio/admin?barbeiro_id={barber.id}&day=2026-04-16",
            follow_redirects=True,
        )
        self.assertIn(b"14:30", agenda_admin.data)
        self.assertNotIn(b"14:45", agenda_admin.data)

    def test_admin_can_update_whatsapp_and_client_sees_link(self):
        self.client.post(
            "/t/nerzilus-studio/admin/login",
            data={"username": "sergioadmin", "senha": "admin123"},
            follow_redirects=True,
        )

        resposta = self.client.post(
            "/t/nerzilus-studio/admin/whatsapp",
            data={"whatsapp": "11955554444", "botao_confirmacao": True},
            follow_redirects=True,
        )
        self.assertEqual(resposta.status_code, 200)
        self.assertIn(b"WhatsApp da barbearia atualizado.", resposta.data)
        self.assertIn(b'value="(11) 95555-4444"', resposta.data)

        acesso = self.client.post(
            "/t/nerzilus-studio/cliente",
            data={"nome": "Cliente Whats", "telefone": "11900001111"},
            follow_redirects=True,
        )
        self.assertEqual(acesso.status_code, 200)
        self.assertIn(b"https://wa.me/11955554444", acesso.data)

    def test_admin_can_update_platform_theme(self):
        self.client.post(
            "/t/nerzilus-studio/admin/login",
            data={"username": "sergioadmin", "senha": "admin123"},
            follow_redirects=True,
        )

        resposta = self.client.post(
            "/t/nerzilus-studio/admin/tema",
            data={"tema": "pink", "botao_confirmacao": True},
            follow_redirects=True,
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertIn(b"Tema da plataforma atualizado.", resposta.data)
        self.assertIn(b'data-theme="pink"', resposta.data)

    def test_tenant_isolation_blocks_cross_access(self):
        self.client.post(
            "/t/nerzilus-studio/cliente",
            data={"nome": "Cliente Teste", "telefone": "11999999999"},
            follow_redirects=True,
        )

        resposta = self.client.get("/t/salao-rosa/cliente/dashboard")

        self.assertEqual(resposta.status_code, 403)

    def test_unauthenticated_dashboard_redirects_to_tenant_access(self):
        resposta = self.client.get("/t/nerzilus-studio/cliente/dashboard", follow_redirects=False)

        self.assertEqual(resposta.status_code, 302)
        self.assertIn("/t/nerzilus-studio/cliente", resposta.headers["Location"])

    def test_deployed_environment_requires_database_url(self):
        from Nerzilus import normalize_database_url

        with patch.dict(os.environ, {"REQUIRE_DATABASE_URL": "true"}, clear=False):
            with self.assertRaises(RuntimeError):
                normalize_database_url("")


if __name__ == "__main__":
    unittest.main()
