import base64, json, os, time, uuid
from datetime import datetime, timezone
import boto3
import starkbank
from .client import StarkClient


class DynamoStore:
    LEASE_SECONDS = 120

    def __init__(self, name):
        self.table = boto3.resource(
            "dynamodb", region_name=os.getenv("AWS_REGION", "us-east-2")
        ).Table(name)

    def save_invoice(self, invoice_id, created_at):
        self.table.put_item(
            Item={
                "pk": f"INVOICE#{invoice_id}",
                "type": "invoice",
                "created_at": created_at,
            }
        )

    def save_event(self, event_id, invoice_id, received_at):
        self.table.put_item(
            Item={
                "pk": f"EVENT#{event_id}",
                "type": "event",
                "invoice_id": invoice_id,
                "received_at": received_at,
            }
        )

    def claim(self, invoice_id, created_at):
        now = int(time.time())
        lease_token = str(uuid.uuid4())
        try:
            self.table.put_item(
                Item={
                    "pk": f"TRANSFER#{invoice_id}",
                    "status": "processing",
                    "created_at": created_at,
                    "lease_until": now + self.LEASE_SECONDS,
                    "lease_token": lease_token,
                },
                ConditionExpression="attribute_not_exists(pk) OR (#s <> :completed AND (attribute_not_exists(lease_until) OR lease_until <= :now))",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":completed": "completed", ":now": now},
            )
            return {"claimed": True, "status": "processing", "lease_token": lease_token}
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            item = self.table.get_item(
                Key={"pk": f"TRANSFER#{invoice_id}"}, ConsistentRead=True
            ).get("Item", {})
            return {"claimed": False, "status": item.get("status", "processing")}

    def mark_retryable(self, invoice_id, lease_token):
        self.table.update_item(
            Key={"pk": f"TRANSFER#{invoice_id}"},
            UpdateExpression="SET #s=:s, lease_until=:lease",
            ConditionExpression="lease_token=:token",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "retryable",
                ":lease": int(time.time()) + self.LEASE_SECONDS,
                ":token": lease_token,
            },
        )

    def complete(self, invoice_id, amount, stark_id, lease_token):
        self.table.update_item(
            Key={"pk": f"TRANSFER#{invoice_id}"},
            UpdateExpression="SET #s=:s, amount=:a, stark_id=:i, lease_until=:lease",
            ConditionExpression="lease_token=:token",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "completed",
                ":a": amount,
                ":i": stark_id,
                ":lease": 0,
                ":token": lease_token,
            },
        )

    def claim_invoice_creation(self, request_key, created_at):
        now = int(time.time())
        lease_token = str(uuid.uuid4())
        try:
            self.table.put_item(
                Item={
                    "pk": f"INVOICE_REQUEST#{request_key}",
                    "status": "processing",
                    "created_at": created_at,
                    "lease_until": now + self.LEASE_SECONDS,
                    "lease_token": lease_token,
                },
                ConditionExpression="attribute_not_exists(pk) OR (#s <> :completed AND (attribute_not_exists(lease_until) OR lease_until <= :now))",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":completed": "completed", ":now": now},
            )
            return {"claimed": True, "status": "processing", "lease_token": lease_token}
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            item = self.table.get_item(
                Key={"pk": f"INVOICE_REQUEST#{request_key}"}, ConsistentRead=True
            ).get("Item", {})
            return {
                "claimed": False,
                "status": item.get("status", "processing"),
                "invoice_id": item.get("invoice_id"),
            }

    def complete_invoice_creation(self, request_key, invoice_id, lease_token):
        self.table.update_item(
            Key={"pk": f"INVOICE_REQUEST#{request_key}"},
            UpdateExpression="SET #s=:s, invoice_id=:id, lease_until=:lease",
            ConditionExpression="lease_token=:token",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "completed",
                ":id": invoice_id,
                ":lease": 0,
                ":token": lease_token,
            },
        )


def _dependencies():
    from .config import Settings

    settings = Settings()
    store = DynamoStore(os.environ["DYNAMODB_TABLE"])
    return settings, store, StarkClient(settings, store)


def lambda_http_handler(event, context):
    _, store, _ = _dependencies()
    body = event.get("body", "")
    try:
        raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
        headers = {str(k).lower(): v for k, v in event.get("headers", {}).items()}
        signature = headers.get("digital-signature", "")
        if not signature:
            raise ValueError("Digital-Signature header is required")
        parsed = starkbank.event.parse(content=raw.decode("utf-8"), signature=signature)
        if getattr(parsed, "subscription", None) != "invoice":
            return {"statusCode": 200, "body": json.dumps({"result": "ignored"})}
        invoice = getattr(getattr(parsed, "log", None), "invoice", None)
        invoice_id = getattr(invoice, "id", None) or getattr(
            invoice, "invoice_id", None
        )
        if not invoice_id or getattr(invoice, "status", None) not in {
            "paid",
            "credited",
        }:
            return {"statusCode": 200, "body": json.dumps({"result": "ignored"})}
    except Exception:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "invalid Stark Bank webhook"}),
        }
    try:
        event_id = getattr(parsed, "id", None) or str(
            uuid.uuid5(uuid.NAMESPACE_URL, raw.decode("utf-8"))
        )
        store.save_event(event_id, invoice_id, datetime.now(timezone.utc).isoformat())
        boto3.client(
            "sqs", region_name=os.getenv("AWS_REGION", "us-east-2")
        ).send_message(
            QueueUrl=os.environ["INVOICE_QUEUE_URL"],
            MessageBody=json.dumps(
                {
                    "event_id": event_id,
                    "body": base64.b64encode(raw).decode(),
                    "signature": signature,
                }
            ),
        )
        return {
            "statusCode": 200,
            "body": json.dumps({"result": "queued", "invoice_id": invoice_id}),
        }
    except Exception:
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "temporary queue failure"}),
        }


def lambda_worker_handler(event, context):
    _, store, client = _dependencies()
    if event.get("action") == "issue_batch":
        batch_key = event.get("idempotency_key") or str(
            uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(event, sort_keys=True))
        )
        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "result": "issued",
                    "count": len(client.issue_batch(idempotency_key=batch_key)),
                }
            ),
        }
    failures = []
    from .service import process_webhook

    for record in event.get("Records", []):
        try:
            message = json.loads(record["body"])
            process_webhook(
                base64.b64decode(message["body"]),
                message.get("signature", ""),
                client,
                store,
            )
        except Exception:
            failures.append({"itemIdentifier": record.get("messageId", "")})
    return {"batchItemFailures": failures}


lambda_handler = lambda_http_handler
