from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import random
import time
import uuid
from types import SimpleNamespace
import starkbank
from starkcore.error import InputErrors, InvalidSignatureError
from .domain import DESTINATION, receipt_from

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _log(event, **details):
    logger.info(json.dumps({"event": event, **details}, default=str, sort_keys=True))


def _random_cpf():
    digits = [random.randint(0, 9) for _ in range(9)]
    while len(set(digits)) == 1:
        digits = [random.randint(0, 9) for _ in range(9)]
    for weight in (10, 11):
        check = sum(
            digit * current_weight
            for digit, current_weight in zip(digits, range(weight, 1, -1))
        )
        digits.append((check * 10) % 11 % 10)
    return "".join(map(str, digits))


def _with_retry(operation, attempts=3, base_delay=0.5):
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as error:
            if isinstance(error, (InputErrors, InvalidSignatureError, ValueError)):
                raise
            if attempt == attempts - 1:
                raise
            time.sleep(base_delay * (2**attempt))


class StarkClient:
    def __init__(self, settings, store):
        settings.validate()
        starkbank.user = starkbank.Project(
            environment=settings.environment,
            id=settings.project_id,
            private_key=settings.private_key,
        )
        self.settings, self.store = settings, store

    def issue_batch(self, minimum=8, maximum=12, idempotency_key=None):
        batch_key = idempotency_key or str(uuid.uuid4())
        result = []
        size = minimum + int(hashlib.sha256(batch_key.encode()).hexdigest()[:8], 16) % (
            maximum - minimum + 1
        )
        _log("invoice_batch_started", idempotency_key=batch_key, size=size)
        for index in range(size):
            request_key = f"{batch_key}:{index}"
            state = self.store.claim_invoice_creation(
                request_key, datetime.now(timezone.utc).isoformat()
            )
            if state["status"] == "completed":
                _log(
                    "invoice_creation_reused",
                    request_key=request_key,
                    invoice_id=state["invoice_id"],
                )
                result.append(SimpleNamespace(id=state["invoice_id"]))
                continue
            if not state["claimed"]:
                raise RuntimeError("invoice creation lease is active")
            tag = f"starkbank-trial:{request_key}"

            def create_or_recover():
                _log(
                    "invoice_creation_requested",
                    request_key=request_key,
                    tag=tag,
                )
                # The Stark Bank SDK returns a generator from ``query``.
                # Materialize it once so the result can be checked and indexed
                # consistently with the list returned by ``create``.
                existing = list(starkbank.invoice.query(tags=[tag], limit=1))
                if existing:
                    _log(
                        "invoice_found_at_starkbank",
                        request_key=request_key,
                        invoice_id=existing[0].id,
                    )
                return existing or starkbank.invoice.create(
                    [
                        starkbank.Invoice(
                            amount=random.randint(
                                self.settings.invoice_min_amount,
                                self.settings.invoice_max_amount,
                            ),
                            name=random.choice(
                                [
                                    "Ana Silva",
                                    "Bruno Costa",
                                    "Carla Souza",
                                    "Diego Oliveira",
                                ]
                            ),
                            tax_id=_random_cpf(),
                            due=datetime.now(timezone.utc) + timedelta(hours=3),
                            expiration=10800,
                            tags=["starkbank-trial", tag],
                        )
                    ]
                )

            invoices = _with_retry(create_or_recover)
            invoice = invoices[0]
            _log(
                "invoice_created_or_recovered",
                request_key=request_key,
                invoice_id=invoice.id,
            )
            self.store.save_invoice(invoice.id, datetime.now(timezone.utc).isoformat())
            self.store.complete_invoice_creation(
                request_key, invoice.id, state["lease_token"]
            )
            _log(
                "invoice_creation_completed",
                request_key=request_key,
                invoice_id=invoice.id,
            )
            result.append(invoice)
        _log("invoice_batch_completed", idempotency_key=batch_key, count=len(result))
        return result

    def transfer_paid_invoice(self, invoice_id, event_invoice):
        now = datetime.now(timezone.utc).isoformat()
        claim = self.store.claim(invoice_id, now)
        if not claim["claimed"]:
            if claim["status"] == "completed":
                _log("transfer_already_completed", invoice_id=invoice_id)
                return None
            _log("transfer_lease_busy", invoice_id=invoice_id, status=claim["status"])
            raise RuntimeError("transfer lease is active")
        _log("transfer_processing_started", invoice_id=invoice_id)
        try:
            receipt = receipt_from(
                invoice_id,
                _with_retry(lambda: starkbank.invoice.payment(invoice_id)),
                event_invoice,
            )
            _log(
                "invoice_payment_loaded",
                invoice_id=invoice_id,
                amount=receipt.amount,
                fee=receipt.fee,
                net_amount=receipt.net_amount,
            )
            response = _with_retry(
                lambda: starkbank.transfer.create(
                    [
                        starkbank.Transfer(
                            amount=receipt.net_amount,
                            external_id=f"starkbank-trial:{invoice_id}",
                            **DESTINATION,
                        )
                    ]
                )
            )
            transfer = response[0]
            _log(
                "transfer_created",
                invoice_id=invoice_id,
                transfer_id=getattr(transfer, "id", ""),
                amount=receipt.net_amount,
            )
            self.store.complete(
                invoice_id,
                receipt.net_amount,
                getattr(transfer, "id", ""),
                claim["lease_token"],
            )
            _log(
                "transfer_completed",
                invoice_id=invoice_id,
                transfer_id=getattr(transfer, "id", ""),
                amount=receipt.net_amount,
            )
            return receipt.net_amount
        except Exception:
            logger.exception(
                json.dumps({"event": "transfer_failed", "invoice_id": invoice_id})
            )
            try:
                self.store.mark_retryable(invoice_id, claim["lease_token"])
            finally:
                raise
