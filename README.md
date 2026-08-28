# Stark Bank Backend Trial

Projeto independente para o desafio Stark Bank.

Emite 8 a 12 invoices a cada 3 horas durante 24 horas, recebe webhook de invoice paga e transfere amount menos fee ao destino solicitado. Usa SQLite para auditoria e idempotência.

## Rodar

    py -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -e ".[dev]"
    Copy-Item .env.example .env
    python -m starkbank_trial

Webhook: POST /webhooks/starkbank. Health: GET /health.

    python -m starkbank_trial.setup_webhook --url https://SEU-ENDPOINT/webhooks/starkbank
    pytest
