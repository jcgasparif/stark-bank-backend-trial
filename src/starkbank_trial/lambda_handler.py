import base64
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
import boto3

# The client contains Stark Bank operations; the service contains shared
# webhook parsing and validation used by both Lambda entry points.
from .client import LeaseBusyError, StarkClient
from .service import is_paid_invoice, parse_webhook

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _json_response(status_code, **payload):
    """Build the API Gateway response format used by both handler branches."""
    # API Gateway expects the HTTP status and a JSON string in the body.
    return {"statusCode": status_code, "body": json.dumps(payload)}


def _log(event, **details):
    # Structured JSON makes the event easy to filter in CloudWatch Logs.
    logger.info(json.dumps({"event": event, **details}, default=str, sort_keys=True))


def _log_exception(event, **details):
    """Write a structured error event while preserving the traceback."""
    logger.exception(
        json.dumps({"event": event, **details}, default=str, sort_keys=True)
    )


class DynamoStore:
    """DynamoDB adapter for invoices, webhooks and idempotency leases."""

    LEASE_SECONDS = 120

    def __init__(self, name):
        # The table name comes from the CloudFormation environment variable.
        self.table = boto3.resource(
            "dynamodb", region_name=os.getenv("AWS_REGION", "us-east-2")
        ).Table(name)

    def save_invoice(self, invoice_id, created_at):
        """Record invoice metadata in DynamoDB."""
        self.table.put_item(
            Item={
                "pk": f"INVOICE#{invoice_id}",
                "type": "invoice",
                "created_at": created_at,
            }
        )

    def save_event(self, event_id, invoice_id, received_at):
        """Record a received webhook event for auditing and deduplication."""
        self.table.put_item(
            Item={
                "pk": f"EVENT#{event_id}",
                "type": "event",
                "invoice_id": invoice_id,
                "received_at": received_at,
            }
        )

    def _claim(self, key, created_at):
        """Atomically create or renew a record lease in DynamoDB."""
        # A unique token identifies the worker that is allowed to finish this job.
        now = int(time.time())
        lease_token = str(uuid.uuid4())
        item = {
            "pk": key,
            "status": "processing",
            "created_at": created_at,
            "lease_until": now + self.LEASE_SECONDS,
            "lease_token": lease_token,
        }
        try:
            # This conditional write succeeds only for a new or expired record.
            self.table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(pk) OR (#s <> :completed AND (attribute_not_exists(lease_until) OR lease_until <= :now))",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":completed": "completed", ":now": now},
            )
            return {"claimed": True, "status": "processing", "lease_token": lease_token}
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            # Someone else owns the lease, or the operation was already completed.
            current = self.table.get_item(Key={"pk": key}, ConsistentRead=True).get(
                "Item", {}
            )
            return {
                "claimed": False,
                "status": current.get("status", "processing"),
                "invoice_id": current.get("invoice_id"),
            }

    def _update_claim(self, key, lease_token, expression, values):
        """Apply a state update only while this worker still owns the lease."""
        # The token condition prevents an expired worker from changing newer state.
        self.table.update_item(
            Key={"pk": key},
            UpdateExpression=expression,
            ConditionExpression="lease_token=:token",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={**values, ":token": lease_token},
        )

    def claim(self, invoice_id, created_at):
        """Acquire the transfer lease, or return the current transfer status."""
        return self._claim(f"TRANSFER#{invoice_id}", created_at)

    def mark_retryable(self, invoice_id, lease_token):
        """Make a failed transfer available for a later SQS retry."""
        self._update_claim(
            f"TRANSFER#{invoice_id}",
            lease_token,
            "SET #s=:status, lease_until=:lease",
            {":status": "retryable", ":lease": int(time.time()) + self.LEASE_SECONDS},
        )

    def complete(self, invoice_id, amount, stark_id, lease_token):
        """Persist the successful transfer and release its lease."""
        self._update_claim(
            f"TRANSFER#{invoice_id}",
            lease_token,
            "SET #s=:status, amount=:amount, stark_id=:stark_id, lease_until=:lease",
            {
                ":status": "completed",
                ":amount": amount,
                ":stark_id": stark_id,
                ":lease": 0,
            },
        )

    def claim_invoice_creation(self, request_key, created_at):
        """Acquire the lease that protects one invoice-creation request."""
        return self._claim(f"INVOICE_REQUEST#{request_key}", created_at)

    def complete_invoice_creation(self, request_key, invoice_id, lease_token):
        """Persist the created invoice ID and release its creation lease."""
        self._update_claim(
            f"INVOICE_REQUEST#{request_key}",
            lease_token,
            "SET #s=:status, invoice_id=:invoice_id, lease_until=:lease",
            {":status": "completed", ":invoice_id": invoice_id, ":lease": 0},
        )


def _dependencies():
    """Create the settings, persistence adapter and Stark Bank client."""
    from .config import Settings

    # Dependencies are created inside the handler so Lambda can reuse the warm
    # runtime while still reading the deployed environment variables.
    settings = Settings()
    store = DynamoStore(os.environ["DYNAMODB_TABLE"])
    return settings, store, StarkClient(settings, store)


def lambda_http_handler(event, context):
    """Receive a Stark Bank webhook, validate it and enqueue it for processing."""
    # HTTP requests only validate and enqueue work; the worker does the payment.
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
        # API Gateway may base64-encode the body, so decode it only when required.
        raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
        # Header names are case-insensitive; normalize them before lookup.
        headers = {str(k).lower(): v for k, v in event.get("headers", {}).items()}
        signature = headers.get("digital-signature", "")
        if not signature:
            # Without the signature, the payload cannot be trusted.
            raise ValueError("Digital-Signature header is required")
        # Parsing also verifies the Stark Bank digital signature.
        parsed, event_id, invoice_id, invoice = parse_webhook(raw, signature)
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
            # Other Stark Bank event types are valid but irrelevant to this app.
            _log(
                "webhook_ignored",
                request_id=request_id,
                reason="unsupported_subscription",
            )
            return _json_response(200, result="ignored")
        if not is_paid_invoice(parsed, invoice_id, invoice):
            # Only paid or credited invoices should start a transfer.
            _log(
                "webhook_ignored",
                request_id=request_id,
                reason="invoice_not_paid",
                invoice_id=invoice_id,
                status=status,
            )
            return _json_response(200, result="ignored")
    except Exception as error:
        # Malformed or unsigned webhooks receive 400 and are not retried by SQS.
        _log_exception(
            "webhook_rejected",
            request_id=request_id,
            error_type=type(error).__name__,
        )
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "invalid Stark Bank webhook"}),
        }
    try:
        # Save the event before enqueueing so there is an audit record.
        store.save_event(event_id, invoice_id, datetime.now(timezone.utc).isoformat())
        _log(
            "webhook_event_persisted",
            request_id=request_id,
            event_id=event_id,
            invoice_id=invoice_id,
        )
        # Put the original payload and signature in SQS for asynchronous work.
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
        return _json_response(200, result="queued", invoice_id=invoice_id)
    except Exception as error:
        # A queue failure is temporary, so the caller receives 500 and can retry.
        _log_exception(
            "webhook_queue_failed",
            request_id=request_id,
            invoice_id=invoice_id,
            error_type=type(error).__name__,
        )
        return _json_response(500, error="temporary queue failure")


def lambda_worker_handler(event, context):
    """Process an invoice batch command or records delivered by SQS."""
    # Lambda reports only failed SQS records so successful records are not retried.
    _, store, client = _dependencies()
    request_id = getattr(context, "aws_request_id", None)
    _log(
        "worker_started",
        request_id=request_id,
        action=event.get("action"),
        record_count=len(event.get("Records", [])),
    )
    if event.get("action") == "issue_batch":
        # GitHub Actions uses this command to create the scheduled invoice batch.
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
    # Import lazily because the issue_batch command does not need webhook parsing.
    from .service import process_webhook

    # SQS can deliver several records in one Lambda invocation.
    for record in event.get("Records", []):
        message_id = record.get("messageId", "")
        try:
            # The body contains the signed webhook encoded by the HTTP handler.
            message = json.loads(record["body"])
            result = process_webhook(
                base64.b64decode(message["body"]),
                message.get("signature", ""),
                client,
                store,
            )
            # No entry in batchItemFailures means SQS removes this message.
            _log(
                "worker_record_completed",
                request_id=request_id,
                message_id=message_id,
                event_id=message.get("event_id"),
                result=result,
            )
        except LeaseBusyError:
            # Another worker owns this invoice; its result will update DynamoDB.
            # Treating this duplicate as handled prevents a retry loop to the DLQ.
            _log(
                "worker_record_deferred",
                request_id=request_id,
                message_id=message_id,
                reason="transfer_lease_busy",
            )
        except Exception as error:
            # Returning this identifier asks Lambda/SQS to retry only this record.
            _log_exception(
                "worker_record_failed",
                request_id=request_id,
                message_id=message_id,
                error_type=type(error).__name__,
            )
            failures.append({"itemIdentifier": message_id})
    _log("worker_finished", request_id=request_id, failed_count=len(failures))
    # Partial batch response preserves successful records when one record fails.
    return {"batchItemFailures": failures}


lambda_handler = lambda_http_handler
