import base64, json, os, hashlib
import boto3
from .client import StarkClient

class DynamoStore:
    def __init__(self, name):
        self.table = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-2")).Table(name)
    def save_invoice(self, invoice_id, created_at): self.table.put_item(Item={"pk": f"INVOICE#{invoice_id}", "type": "invoice", "created_at": created_at})
    def save_event(self, event_id, invoice_id, received_at):
        self.table.put_item(Item={"pk": f"EVENT#{event_id}", "type": "event", "invoice_id": invoice_id, "received_at": received_at})
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
    settings = Settings()
    store = DynamoStore(os.environ["DYNAMODB_TABLE"])
    return settings, store, StarkClient(settings, store)

def lambda_http_handler(event, context):
    from datetime import datetime, timezone
    settings, store, _ = _dependencies()
    body = event.get("body", "")
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    headers = {str(k).lower():v for k,v in event.get("headers",{}).items()}
    signature = headers.get("digital-signature", "")
    event_id = hashlib.sha256(raw + signature.encode()).hexdigest()
    try:
        store.save_event(event_id, "unknown", datetime.now(timezone.utc).isoformat())
        boto3.client("sqs", region_name=os.getenv("AWS_REGION","us-east-2")).send_message(
            QueueUrl=os.environ["INVOICE_QUEUE_URL"],
            MessageBody=json.dumps({"event_id":event_id,"body":base64.b64encode(raw).decode(),"signature":signature}))
    except Exception:
        # O endpoint confirma 200 conforme o contrato do webhook; o erro fica nos logs.
        pass
    return {"statusCode":200,"body":json.dumps({"result":"queued","event_id":event_id})}

def lambda_worker_handler(event, context):
    settings, store, client = _dependencies()
    if event.get("action") == "issue_batch":
        if not store.claim_issue_run(): return {"statusCode":200,"body":json.dumps({"result":"window-complete"})}
        return {"statusCode":200,"body":json.dumps({"result":"issued","count":len(client.issue_batch())})}
    failures = []
    from .service import process_webhook
    for record in event.get("Records", []):
        try:
            message = json.loads(record["body"])
            raw = base64.b64decode(message["body"])
            process_webhook(raw, message.get("signature", ""), client, store)
        except Exception:
            failures.append({"itemIdentifier": record.get("messageId", "")})
    return {"batchItemFailures": failures}

lambda_handler = lambda_http_handler
