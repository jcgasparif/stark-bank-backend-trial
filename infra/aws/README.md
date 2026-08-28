# AWS serverless deployment

Arquitetura em us-east-2:

Stark Bank -> Function URL -> Webhook Lambda -> SQS -> Worker Lambda -> Transfer
                                             \-> DLQ após 5 tentativas
                                                DynamoDB para idempotência

Recursos criados pelo template SAM:
- Webhook Lambda com Function URL pública;
- Worker Lambda acionado pela SQS;
- SQS principal com VisibilityTimeout de 180 segundos;
- DLQ com retenção de 14 dias e máximo de 5 recebimentos;
- DynamoDB provisionado com 1 RCU e 1 WCU;
- IAM policies gerenciadas pelo SAM.

O workflow deploy-lambda executa testes e deploya a stack. O workflow issue-invoices invoca o Worker Lambda a cada 3 horas; o DynamoDB limita a janela a 8 lotes.

GitHub secrets:
- AWS_DEPLOY_ROLE_ARN
- STARK_PROJECT_ID
- STARK_PRIVATE_KEY

Use o output FunctionUrl como endpoint de webhook no Stark Bank. Monitore a DLQ antes de considerar a execução concluída. A arquitetura busca ficar no free tier, mas isso depende da elegibilidade da conta e dos demais recursos. Apague a stack depois do desafio.
