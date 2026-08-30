import base64, json, logging, os, time, uuid
from datetime import datetime, timezone
import boto3
import starkbank
from .client import StarkClient

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _log(event, **details):
    logger.info(json.dumps({"event": event, **details}, default=str, sort_keys=True))


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
    request_id = getattr(context, "aws_request_id", None)
    _log(
        "webhook_received",
        request_id=request_id,
        encoded=bool(event.get("isBase64Encoded")),
        body_size=len(body or ""),
    )
    try:
        raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
        headers = {str(k).lower(): v for k, v in event.get("headers", {}).items()}
        signature = headers.get("digital-signature", "")
        if not signature:
            raise ValueError("Digital-Signature header is required")
        parsed = starkbank.event.parse(content=raw.decode("utf-8"), signature=signature)
        event_id = getattr(parsed, "id", None) or str(
            uuid.uuid5(uuid.NAMESPACE_URL, raw.decode("utf-8"))
        )
        invoice = getattr(getattr(parsed, "log", None), "invoice", None)
        invoice_id = getattr(invoice, "id", None) or getattr(
            invoice, "invoice_id", None
        )
        status = getattr(invoice, "status", None)
        _log(
            "webhook_validated",
            request_id=request_id,
            event_id=event_id,
            subscription=getattr(parsed, "subscription", None),
            invoice_id=invoice_id,
            status=status,
        )
        if getattr(parsed, "subscription", None) != "invoice":
            _log(
                "webhook_ignored",
                request_id=request_id,
                reason="unsupported_subscription",
            )
            return {"statusCode": 200, "body": json.dumps({"result": "ignored"})}
        if not invoice_id or status not in {
            "paid",
            "credited",
        }:
            _log(
                "webhook_ignored",
                request_id=request_id,
                reason="invoice_not_paid",
                invoice_id=invoice_id,
                status=status,
            )
            return {"statusCode": 200, "body": json.dumps({"result": "ignored"})}
    except Exception as error:
        logger.exception(
            json.dumps(
                {
                    "event": "webhook_rejected",
                    "request_id": request_id,
                    "error_type": type(error).__name__,
                }
            )
        )
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "invalid Stark Bank webhook"}),
        }
    try:
        store.save_event(event_id, invoice_id, datetime.now(timezone.utc).isoformat())
        _log(
            "webhook_event_persisted",
            request_id=request_id,
            event_id=event_id,
            invoice_id=invoice_id,
        )
        queue_response = boto3.client(
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
        _log(
            "webhook_queued",
            request_id=request_id,
            event_id=event_id,
            invoice_id=invoice_id,
            sqs_message_id=queue_response.get("MessageId"),
        )
        return {
            "statusCode": 200,
            "body": json.dumps({"result": "queued", "invoice_id": invoice_id}),
        }
    except Exception as error:
        logger.exception(
            json.dumps(
                {
                    "event": "webhook_queue_failed",
                    "request_id": request_id,
                    "invoice_id": invoice_id,
                    "error_type": type(error).__name__,
                }
            )
        )
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "temporary queue failure"}),
        }


def lambda_worker_handler(event, context):
    _, store, client = _dependencies()
    request_id = getattr(context, "aws_request_id", None)
    _log(
        "worker_started",
        request_id=request_id,
        action=event.get("action"),
        record_count=len(event.get("Records", [])),
    )
    if event.get("action") == "issue_batch":
        batch_key = event.get("idempotency_key") or str(
            uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(event, sort_keys=True))
        )
        result = {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "result": "issued",
                    "count": len(client.issue_batch(idempotency_key=batch_key)),
                }
            ),
        }
        _log(
            "worker_issue_batch_completed",
            request_id=request_id,
            idempotency_key=batch_key,
            count=json.loads(result["body"])["count"],
        )
        return result
    failures = []
    from .service import process_webhook

    for record in event.get("Records", []):
        message_id = record.get("messageId", "")
        try:
            message = json.loads(record["body"])
            result = process_webhook(
                base64.b64decode(message["body"]),
                message.get("signature", ""),
                client,
                store,
            )
            _log(
                "worker_record_completed",
                request_id=request_id,
                message_id=message_id,
                event_id=message.get("event_id"),
                result=result,
            )
        except Exception as error:
            logger.exception(
                json.dumps(
                    {
                        "event": "worker_record_failed",
                        "request_id": request_id,
                        "message_id": message_id,
                        "error_type": type(error).__name__,
                    }
                )
            )
            failures.append({"itemIdentifier": message_id})
    _log("worker_finished", request_id=request_id, failed_count=len(failures))
    return {"batchItemFailures": failures}


lambda_handler = lambda_http_handler
