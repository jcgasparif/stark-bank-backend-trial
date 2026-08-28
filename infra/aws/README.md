# AWS serverless deployment

O webhook é assíncrono e sempre responde HTTP 200 depois de registrar o recebimento no DynamoDB e publicar a mensagem na SQS. O worker valida a assinatura, identifica invoice paga e cria a Transfer. Erros do worker retornam falha de item para o mapeamento SQS; após 5 tentativas a mensagem vai para a DLQ.

Fluxo: Stark Bank -> Function URL -> DynamoDB + SQS -> Worker Lambda -> Transfer; erro -> retry -> DLQ.

Recursos no template SAM: duas Lambdas, SQS principal, DLQ, DynamoDB e Function URL. A janela do emissor é limitada a 8 lotes.

Atenção: se a própria publicação na SQS falhar, o endpoint ainda responde 200 por exigência do webhook; esse caso deve ser acompanhado por métricas/logs e alarmes. Em operação crítica, recomenda-se um mecanismo de recuperação via consulta periódica aos eventos do Stark Bank.
