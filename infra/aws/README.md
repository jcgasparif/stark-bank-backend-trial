# AWS serverless deployment

O webhook segue o contrato de entrega do Stark Bank:
- HTTP 200 somente após validar o evento, registrar no DynamoDB e publicar na SQS;
- HTTP 400 para assinatura/payload inválido;
- HTTP 500 para falha no DynamoDB ou SQS, permitindo novo envio pelo Stark Bank.

O worker valida novamente o evento, processa a Transfer e informa falhas de item ao SQS. Após 5 tentativas a mensagem vai para a DLQ.

Fluxo: Stark Bank -> Function URL -> DynamoDB + SQS -> Worker Lambda -> Transfer; erro -> retry -> DLQ.
