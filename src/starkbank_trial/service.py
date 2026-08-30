from datetime import datetime, timezone
import uuid, starkbank


def process_webhook(raw: bytes, signature: str, client, store):
    event = starkbank.event.parse(content=raw.decode("utf-8"), signature=signature)
    event_id = getattr(event, "id", None) or str(
        uuid.uuid5(uuid.NAMESPACE_URL, raw.decode("utf-8"))
    )
    if getattr(event, "subscription", None) != "invoice":
        return "ignored"
    invoice = getattr(getattr(event, "log", None), "invoice", None)
    invoice_id = getattr(invoice, "id", None) or getattr(invoice, "invoice_id", None)
    if not invoice_id or getattr(invoice, "status", None) not in {"paid", "credited"}:
        return "ignored"
    store.save_event(event_id, invoice_id, datetime.now(timezone.utc).isoformat())
    return (
        "duplicate"
        if client.transfer_paid_invoice(invoice_id, invoice) is None
        else "transferred"
    )
