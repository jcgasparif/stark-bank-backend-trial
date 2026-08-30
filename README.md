# Stark Bank Backend Trial

Integração Python com o Stark Bank para emissão de Invoices, recebimento de
webhooks de pagamento e criação de Transfers no ambiente Sandbox.

## Visão geral

O projeto possui dois modos de execução:

- **Local:** uma API Flask recebe webhooks e usa SQLite para auditoria e
  idempotência local.
- **AWS:** duas funções Lambda recebem e processam os webhooks, usando
  DynamoDB, SQS e uma Dead Letter Queue (DLQ).

O ambiente padrão do projeto é `sandbox`. Nenhuma operação de teste deve ser
tratada como movimentação financeira real.

O fluxo de emissão automática usa o GitHub Actions a cada três horas. O
workflow também pode ser iniciado manualmente.

## Arquitetura Python

O código está organizado em `src/starkbank_trial`:

| Módulo | Responsabilidade |
| --- | --- |
| `app.py` | API Flask local, com `/health` e endpoint de webhook. |
| `lambda_handler.py` | Entradas das Lambdas AWS: webhook HTTP e worker SQS. |
| `client.py` | Integração com o SDK Stark Bank para Invoices e Transfers. |
| `service.py` | Validação e processamento dos eventos recebidos. |
| `domain.py` | Regras de negócio, cálculo de `amount - fee` e conta destino. |
| `config.py` | Leitura e validação das configurações e da chave PEM. |
| `store.py` | Persistência local em SQLite. |
| `scheduler.py` | Rotina local de emissão periódica. |

### Fluxo local

```text
Cliente/Stark Bank
        |
        v
  Flask /webhooks/starkbank
        |
        v
  service.process_webhook
        |
        +--> SQLite: eventos e claims
        |
        +--> Stark Bank: pagamento e transferência
```

O banco local fica em `./data/starkbank.sqlite3`, por padrão. Ele não é
sincronizado automaticamente com o DynamoDB da AWS.

## Arquitetura AWS

Os recursos AWS são descritos em [`template.yaml`](template.yaml) e
provisionados pelo AWS SAM/CloudFormation na região `us-east-2`.

```text
Stark Bank
    |
    v
WebhookFunction (Function URL pública)
    |
    +--> valida assinatura do webhook
    +--> grava evento no DynamoDB
    +--> envia mensagem para InvoiceQueue
                              |
                              v
                    WorkerFunction (SQS trigger)
                              |
                              +--> consulta pagamento
                              +--> cria Transfer
                              +--> grava estado no DynamoDB
                              |
                              +--> falha após 5 recebimentos -> InvoiceDlq
```

### Recursos provisionados

- `WebhookFunction` (`stark-bank-webhook`): recebe o webhook via Function URL.
- `WorkerFunction` (`stark-bank-worker`): processa mensagens da SQS.
- `InvoiceQueue`: fila principal, com timeout de visibilidade de 180 segundos.
- `InvoiceDlq`: fila de mensagens que falharam após 5 tentativas.
- `TrialTable`: tabela DynamoDB com chave de partição `pk`.
- IAM policies para acesso das Lambdas ao DynamoDB e à SQS.

As duas Lambdas usam o mesmo pacote em `src`, mas handlers diferentes:

```text
starkbank_trial.lambda_handler.lambda_http_handler
starkbank_trial.lambda_handler.lambda_worker_handler
```

O `WebhookFunction` recebe os webhooks pela Function URL. O `WorkerFunction`
é chamado de duas formas:

- pelo gatilho da `InvoiceQueue`, para processar pagamentos recebidos;
- pelo workflow `issue-invoices.yml`, para emitir um lote de invoices.

### Agendamento da emissão

O arquivo `.github/workflows/issue-invoices.yml` contém:

```yaml
- cron: "0 */3 * * *"
```

Isso executa o job a cada três horas, no minuto zero, usando UTC. O workflow
envia para a Lambda `stark-bank-worker` o payload `{"action":"issue_batch"}`.
Cada execução recebe uma chave idempotente baseada no ID da execução para que
uma repetição não crie invoices duplicadas.

## Resiliência e idempotência

### Criação de Invoice

Cada execução recebe uma chave idempotente. O workflow usa o formato
`github-run-<run_id>`.

Para cada Invoice, o sistema:

1. registra a intenção no DynamoDB;
2. cria uma chave determinística para aquela posição do lote;
3. consulta uma tag determinística no Stark Bank antes de criar novamente;
4. salva o ID retornado e marca a operação como concluída.

Assim, uma repetição da mesma execução consegue recuperar uma Invoice já
criada em vez de gerar outra.

### Transferências

Antes de transferir, o worker cria um registro com estado `processing`,
`lease_until` e `lease_token`.

- O lease dura 120 segundos.
- Uma execução concorrente não assume o mesmo trabalho enquanto o lease está
  válido.
- Após timeout, outra execução pode assumir o processamento.
- A execução antiga não consegue confirmar o resultado porque seu
  `lease_token` deixou de ser válido.
- Em caso de erro, o registro fica `retryable`; ele não é apagado.
- Em caso de sucesso, fica `completed`.
- Se outra entrega da mesma invoice encontrar um lease válido, ela é registrada
  como duplicada em processamento e não entra em retry contínuo até a DLQ.

### Retry do Stark Bank

Falhas temporárias têm até três tentativas com espera exponencial:

```text
0,5 segundo -> 1 segundo -> 2 segundos
```

Erros permanentes, como credenciais inválidas, IP não autorizado e CPF
inválido, não são repetidos desnecessariamente.

## Conta destino das Transfers

Após uma Invoice ser recebida com status `paid` ou `credited`, o worker envia
`amount - fee` para a conta configurada em `domain.py`:

```text
Banco:    20018183
Agência:  0001
Conta:    6341320293482496
Nome:     Stark Bank S.A.
CNPJ:     20.018.183/0001-80
Tipo:     payment
```

No ambiente `sandbox`, essa operação é apenas uma simulação.

## Execução local

O servidor local usa Flask e SQLite. Ele não é necessário para o deploy AWS.
Quando `RUN_SCHEDULER=true`, o processo local inicia também oito lotes com
intervalo de três horas. Para usar somente a API local, defina
`RUN_SCHEDULER=false`.

### Preparar o ambiente

No PowerShell:

```powershell
cd C:\Workspace\Git\StarBank
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Configure as credenciais do Sandbox apenas na sessão atual:

```powershell
$env:STARK_ENVIRONMENT = "sandbox"
$env:STARK_PROJECT_ID = "seu_project_id"
$env:STARK_PRIVATE_KEY_B64 = [Convert]::ToBase64String(
  [Text.Encoding]::UTF8.GetBytes((Get-Content -Raw .\keys\private-key.pem))
)
$env:RUN_SCHEDULER = "false"
```

O valor da chave privada não deve ser impresso, versionado ou colocado em
issues/logs.

### Iniciar a API

```powershell
python -m starkbank_trial
```

Endpoints locais:

```text
GET  http://localhost:8080/health
POST http://localhost:8080/webhooks/starkbank
```

Para receber um webhook real localmente, exponha a porta 8080 com um túnel
seguro, como Cloudflare Tunnel ou ngrok, e cadastre a URL pública no Sandbox.

## Testes e formatação

Executar os testes:

```powershell
python -m pytest -q
```

Verificar a formatação:

```powershell
python -m black --check src tests
```

Verificar a qualidade dos imports e regras estáticas:

```powershell
python -m ruff check src tests
```

Formatar o código:

```powershell
python -m black src tests
```

Os testes atuais cobrem regras de domínio, CPF, configuração da chave,
criação de Invoice/Transfer, idempotência e retry exponencial.

Os comandos acima devem ser executados com o ambiente virtual ativado. As
dependências de desenvolvimento estão no grupo opcional `dev` do
`pyproject.toml`.

## Deploy na AWS

O workflow `.github/workflows/deploy-lambda.yml` é executado em push para a
`master` ou manualmente pelo GitHub Actions.

Ele:

1. instala as dependências;
2. executa os testes;
3. configura credenciais AWS por OIDC;
4. executa `sam build --use-container`;
5. executa `sam deploy` na stack `stark-bank-backend-trial`.

O workflow `.github/workflows/issue-invoices.yml` pode ser executado por
agendamento ou manualmente. Ele invoca a `WorkerFunction` com a ação
`issue_batch` e uma chave idempotente baseada no ID da execução do GitHub.

O deploy usa `sam deploy --resolve-s3`; o SAM utiliza um bucket S3 para os
artefatos de implantação. Esse bucket é diferente da fila SQS e da tabela
DynamoDB usadas pela aplicação.

### Secrets necessários no GitHub

```text
AWS_DEPLOY_ROLE_ARN
STARK_PROJECT_ID
STARK_PRIVATE_KEY
```

`STARK_PRIVATE_KEY` deve conter a chave privada PEM completa. Ela é convertida
para Base64 durante o deploy e chega à Lambda como `STARK_PRIVATE_KEY_B64`.

## Custos e observações

O template atual não cria alarmes CloudWatch e mantém o DynamoDB em capacidade
provisionada `1/1`. Alterar para `PAY_PER_REQUEST`, autoscaling ou adicionar
alarmes muda o comportamento de cobrança e deve ser avaliado separadamente.

Mesmo no Free Tier, consumo de serviços AWS pode gerar cobrança. Consulte o
Billing/Cost Explorer antes de executar testes repetidos na AWS.
