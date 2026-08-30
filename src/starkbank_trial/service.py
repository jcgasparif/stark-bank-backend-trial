from datetime import datetime, timezone
import uuid
import starkbank

# This module is the application layer between Lambda and Stark Bank.


def parse_webhook(raw: bytes, signature: str):
    """Parse a Stark Bank webhook and return the fields used by the app.

    Keeping this logic in one place prevents the HTTP handler and the worker
    from interpreting the same webhook differently.
    """
    # Decode once because the payload is also used to generate the fallback ID.
    content = raw.decode("utf-8")
    event = starkbank.event.parse(content=content, signature=signature)
    event_id = getattr(event, "id", None) or str(
        uuid.uuid5(uuid.NAMESPACE_URL, content)
    )
    invoice = getattr(getattr(event, "log", None), "invoice", None)
    invoice_id = getattr(invoice, "id", None) or getattr(invoice, "invoice_id", None)
    return event, event_id, invoice_id, invoice


def is_paid_invoice(event, invoice_id, invoice) -> bool:
    """Return whether the event is an invoice event that can be processed."""
    return (
        getattr(event, "subscription", None) == "invoice"
        and bool(invoice_id)
        and getattr(invoice, "status", None) in {"paid", "credited"}
    )


def process_webhook(raw: bytes, signature: str, client, store):
    """Validate one queued webhook and transfer its paid invoice."""
    # The worker validates the signature again instead of trusting SQS data.
    event, event_id, invoice_id, invoice = parse_webhook(raw, signature)
    if not is_paid_invoice(event, invoice_id, invoice):
        return "ignored"

    # Persisting the event makes duplicate webhook deliveries traceable.
    store.save_event(event_id, invoice_id, datetime.now(timezone.utc).isoformat())

    # The client performs the idempotent transfer using the DynamoDB lease.
    return (
        "duplicate"
        if client.transfer_paid_invoice(invoice_id, invoice) is None
        else "transferred"
    )
