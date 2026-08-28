# AWS deployment

The workflow deploys to ECS Fargate in us-east-2.

Required resources: ECR repository, ECS Fargate cluster/service, CloudWatch log group /ecs/stark-bank-backend-trial, two Secrets Manager secrets, and an ALB exposing port 8080. Register the Stark webhook at the public HTTPS ALB URL plus /webhooks/starkbank.

Required GitHub Actions secrets: AWS_DEPLOY_ROLE_ARN, ECS_EXECUTION_ROLE_ARN, ECS_TASK_ROLE_ARN, STARK_PROJECT_ID_SECRET_ARN and STARK_PRIVATE_KEY_SECRET_ARN.

The deploy role must use GitHub OIDC and allow ECR push plus ECS task-definition registration and service update. Keep one ECS task during the 24-hour trial. SQLite is ephemeral in this first deployment; use RDS or DynamoDB for durable production idempotency.
