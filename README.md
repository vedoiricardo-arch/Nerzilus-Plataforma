# Nerzilus

Plataforma SaaS multi-tenant para barbearias e saloes, baseada em Flask, PostgreSQL e Asaas.

## O que esta pronto

- Multi-tenant por `tenant_id`
- Cadastro da empresa com admin proprio
- Trial de 7 dias
- Assinatura SaaS com Asaas
- Pix recorrente via gateway
- Webhook Asaas para ativacao e resiliencia de billing
- Painel interno de assinatura
- Painel admin, agenda, clientes, profissionais e servicos
- Controle de acesso por status da assinatura
- Healthcheck para deploy

## Ambiente local

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python criar_banco.py
python main.py
```

App local: `http://127.0.0.1:5000`

Tenant demo:

- tenant: `nerzilus-studio`
- admin: `sergioadmin`
- senha: `admin123`

## Variaveis de ambiente

```env
SECRET_KEY=troque-esta-chave-em-producao
DATABASE_URL=postgresql+psycopg://usuario:senha@localhost:5432/nerzilus
APP_BASE_URL=http://127.0.0.1:5000
FORCE_HTTPS=false
SESSION_COOKIE_SECURE=false
SESSION_COOKIE_SAMESITE=Lax
UPLOAD_FOLDER=Nerzilus/static/fotos_posts
ADMIN_EMAIL=sergioadmin@nerzilus.local

ASAAS_API_KEY=
ASAAS_ENVIRONMENT=sandbox
ASAAS_WEBHOOK_TOKEN=
ASAAS_PIX_KEY=51993338005
PLAN_ACCESS_LIBERADO_MONTHLY_AMOUNT=99.00
PLAN_ACCESS_LIBERADO_YEARLY_AMOUNT=990.00

WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_API_VERSION=v21.0
```

## Billing Asaas

Fluxo implementado:

- criacao do customer Asaas por tenant
- criacao de assinatura recorrente
- trial de 7 dias antes da primeira cobranca
- pagina interna de billing com link da cobranca
- exibicao do Pix copia e cola quando a cobranca atual for Pix
- cancelamento da assinatura no proximo ciclo
- webhook como fonte de verdade para status

Endpoint de webhook:

```text
POST /webhook/asaas
```

Se `ASAAS_WEBHOOK_TOKEN` estiver configurado, o endpoint exige o header `asaas-access-token` com o mesmo valor.

## PostgreSQL

Em producao, use PostgreSQL persistente. O app aceita:

- `postgres://...`
- `postgresql://...`
- `postgresql+psycopg://...`

O bootstrap normaliza automaticamente para `postgresql+psycopg://...`.

## Deploy

Arquivos preparados:

- `Procfile`
- `wsgi.py`
- `runtime.txt`
- `render.yaml`
- `requirements.txt`

Comando web:

```bash
gunicorn wsgi:application --workers 2 --threads 4 --timeout 120
```

Comando release:

```bash
python criar_banco.py
```

### Render

1. Crie o PostgreSQL.
2. Conecte o repositório.
3. Use o `render.yaml` ou configure manualmente.
4. Preencha `ASAAS_API_KEY`, `ASAAS_ENVIRONMENT`, `ASAAS_WEBHOOK_TOKEN`, `APP_BASE_URL` e `DATABASE_URL`.
5. Cadastre no Asaas o webhook `https://seu-dominio.com/webhook/asaas`.

## Observacoes

- O app usa `ProxyFix` para funcionar atras de proxy reverso.
- Imagens de cabecalho passam a ser persistidas no banco; o diretório local segue apenas para compatibilidade e migracao.
- A ativacao da assinatura depende do webhook, nao do redirect visual.
- A chave Pix `51993338005` precisa estar configurada e validada na conta do gateway para o repasse oficial.

## Testes

```bash
python -m unittest tests.test_app
```