import base64, json, os, uuid
import boto3
import starkbank
from .client import StarkClient

class DynamoStore:
    def __init__(self, name):
        self.table = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-2")).Table(name)
    def save_invoice(self, invoice_id, created_at): self.table.put_item(Item={"pk": f"INVOICE#{invoice_id}", "type": "invoice", "created_at": created_at})
    def save_event(self, event_id, invoice_id, received_at): self.table.put_item(Item={"pk": f"EVENT#{event_id}", "type": "event", "invoice_id": invoice_id, "received_at": received_at})
    def claim(self, invoice_id, created_at):
        try:
            self.table.put_item(Item={"pk": f"TRANSFER#{invoice_id}", "status": "processing", "created_at": created_at}, ConditionExpression="attribute_not_exists(pk)")
            return True
        except self.table.meta.client.exceptions.ConditionalCheckFailedException: return False
    def release(self, invoice_id): self.table.delete_item(Key={"pk": f"TRANSFER#{invoice_id}"})
    def complete(self, invoice_id, amount, stark_id): self.table.update_item(Key={"pk": f"TRANSFER#{invoice_id}"}, UpdateExpression="SET #s=:s, amount=:a, stark_id=:i", ExpressionAttributeNames={"#s":"status"}, ExpressionAttributeValues={":s":"completed",":a":amount,":i":stark_id})
    def claim_issue_run(self, maximum=8):
        try:
            self.table.update_item(Key={"pk":"SCHEDULE"}, UpdateExpression="ADD issue_runs :one", ConditionExpression="attribute_not_exists(issue_runs) OR issue_runs < :max", ExpressionAttributeValues={":one":1,":max":maximum})
            return True
        except self.table.meta.client.exceptions.ConditionalCheckFailedException: return False

def _dependencies():
    from .config import Settings
    store = DynamoStore(os.environ["DYNAMODB_TABLE"])
    return Settings(), store, StarkClient(Settings(), store)

def lambda_http_handler(event, context):
    settings, store, client = _dependencies()
    body = event.get("body", "")
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    headers = {str(k).lower():v for k,v in event.get("headers",{}).items()}
    signature = headers.get("digital-signature")
    if not signature:
        return {"statusCode":400,"body":json.dumps({"error":"Digital-Signature header is required"})}
    try:
        parsed = starkbank.event.parse(content=raw.decode("utf-8"), signature=signature)
        if getattr(parsed, "subscription", None) != "invoice":
            return {"statusCode":200,"body":json.dumps({"result":"ignored"})}
        invoice = getattr(getattr(parsed, "log", None), "invoice", None)
        invoice_id = getattr(invoice, "id", None) or getattr(invoice, "invoice_id", None)
        if not invoice_id or getattr(invoice, "status", None) not in {"paid","credited"}:
            return {"statusCode":200,"body":json.dumps({"result":"ignored"})}
        event_id = getattr(parsed, "id", None) or str(uuid.uuid5(uuid.NAMESPACE_URL, raw.decode("utf-8")))
        store.save_event(event_id, invoice_id, __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())
        boto3.client("sqs", region_name=os.getenv("AWS_REGION","us-east-2")).send_message(
            QueueUrl=os.environ["INVOICE_QUEUE_URL"],
            MessageBody=json.dumps({"event_id":event_id,"invoice_id":invoice_id}))
        return {"statusCode":202,"body":json.dumps({"result":"queued","invoice_id":invoice_id})}
    except Exception:
        return {"statusCode":500,"body":json.dumps({"error":"temporary processing failure"})}

def lambda_worker_handler(event, context):
    settings, store, client = _dependencies()
    if event.get("action") == "issue_batch":
        if not store.claim_issue_run(): return {"statusCode":200,"body":json.dumps({"result":"window-complete"})}
        return {"statusCode":200,"body":json.dumps({"result":"issued","count":len(client.issue_batch())})}
    failures = []
    for record in event.get("Records", []):
        try:
            message = json.loads(record["body"])
            client.transfer_paid_invoice(message["invoice_id"], {})
        except Exception:
            failures.append({"itemIdentifier": record.get("messageId", "")})
    return {"batchItemFailures": failures}

lambda_handler = lambda_http_handler
