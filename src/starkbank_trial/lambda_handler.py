import base64, json, os
import boto3
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

def lambda_handler(event, context):
    from .config import Settings
    settings, store = Settings(), DynamoStore(os.environ["DYNAMODB_TABLE"])
    client = StarkClient(settings, store)
    if event.get("action") == "issue_batch":
        if not store.claim_issue_run(): return {"statusCode":200,"body":json.dumps({"result":"window-complete"})}
        return {"statusCode":200,"body":json.dumps({"result":"issued","count":len(client.issue_batch())})}
    body = event.get("body", "")
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    headers = {str(k).lower():v for k,v in event.get("headers",{}).items()}
    signature = headers.get("digital-signature")
    if not signature: return {"statusCode":400,"body":json.dumps({"error":"Digital-Signature header is required"})}
    try:
        from .service import process_webhook
        return {"statusCode":200,"body":json.dumps({"result":process_webhook(raw,signature,client,store)})}
    except Exception:
        return {"statusCode":500,"body":json.dumps({"error":"temporary processing failure"})}
