# AWS serverless deployment

Arquitetura: uma Lambda, uma Function URL pública, uma tabela DynamoDB provisionada com 1 leitura/escrita e IAM OIDC para GitHub Actions, na região us-east-2.

Secrets do GitHub: AWS_DEPLOY_ROLE_ARN, STARK_PROJECT_ID e STARK_PRIVATE_KEY.

O deploy usa SAM. A Function URL gerada no output FunctionUrl deve ser registrada no Stark Bank com subscription invoice. O workflow issue-invoices invoca a Lambda a cada 3 horas; o DynamoDB limita a oito lotes por janela.

A arquitetura busca permanecer no free tier, mas a AWS pode cobrar conforme elegibilidade da conta e outros recursos. Remova a stack após o desafio.
