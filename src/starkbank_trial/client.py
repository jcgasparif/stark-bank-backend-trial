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


class LeaseBusyError(RuntimeError):
    """Signals that another worker is already processing the same invoice."""


CUSTOMER_NAMES = (
    "Ana Silva",
    "Bruno Costa",
    "Carla Souza",
    "Diego Oliveira",
    "Eduardo Martins",
    "Fernanda Almeida",
    "Gabriel Santos",
    "Helena Rodrigues",
    "Igor Carvalho",
    "Juliana Ferreira",
    "Lucas Gomes",
    "Mariana Ribeiro",
)


def _log(event, **details):
    logger.info(json.dumps({"event": event, **details}, default=str, sort_keys=True))


def _log_exception(event, **details):
    """Write a structured error event while preserving the traceback."""
    logger.exception(
        json.dumps({"event": event, **details}, default=str, sort_keys=True)
    )


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


def _cpf_for_name(name):
    """Return a stable, valid CPF for the given sandbox customer name."""
    digits = [value % 10 for value in hashlib.sha256(name.encode()).digest()[:9]]
    if len(set(digits)) == 1:
        digits[0] = (digits[0] + 1) % 10

    for weight in (10, 11):
        check = sum(
            digit * current_weight
            for digit, current_weight in zip(digits, range(weight, 1, -1))
        )
        digits.append((check * 10) % 11 % 10)

    return "".join(map(str, digits))


def _with_retry(operation, attempts=3, base_delay=0.5):
    """Retry unexpected temporary failures with exponential backoff.

    Input and signature errors are deterministic, so retrying them only delays
    the message and can send it unnecessarily to the DLQ.
    """
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
        """Create or recover a deterministic batch of sandbox invoices."""
        batch_key = idempotency_key or str(uuid.uuid4())
        result = []
        size = minimum + int(hashlib.sha256(batch_key.encode()).hexdigest()[:8], 16) % (
            maximum - minimum + 1
        )
        name_offset = int(hashlib.sha256(batch_key.encode()).hexdigest()[8:16], 16)
        _log("invoice_batch_started", idempotency_key=batch_key, size=size)
        for index in range(size):
            # Each invoice gets its own lease so a Lambda retry cannot create it twice.
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
            customer_name = CUSTOMER_NAMES[(name_offset + index) % len(CUSTOMER_NAMES)]

            invoices = _with_retry(
                lambda: self._create_or_recover_invoice(request_key, tag, customer_name)
            )
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

    def _create_or_recover_invoice(self, request_key, tag, customer_name):
        """Recover an invoice after a timeout, or create it when absent."""
        _log("invoice_creation_requested", request_key=request_key, tag=tag)
        # The SDK returns a generator for queries, so materialize it once.
        existing = list(starkbank.invoice.query(tags=[tag], limit=1))
        if existing:
            _log(
                "invoice_found_at_starkbank",
                request_key=request_key,
                invoice_id=existing[0].id,
            )
            return existing
        return starkbank.invoice.create(
            [
                starkbank.Invoice(
                    amount=random.randint(
                        self.settings.invoice_min_amount,
                        self.settings.invoice_max_amount,
                    ),
                    name=customer_name,
                    tax_id=_cpf_for_name(customer_name),
                    due=datetime.now(timezone.utc) + timedelta(hours=3),
                    expiration=10800,
                    tags=["starkbank-trial", tag],
                )
            ]
        )

    def transfer_paid_invoice(self, invoice_id, event_invoice):
        """Transfer a paid invoice once, returning its net amount."""
        now = datetime.now(timezone.utc).isoformat()
        # DynamoDB conditionally grants the lease to only one worker at a time.
        claim = self.store.claim(invoice_id, now)
        if not claim["claimed"]:
            if claim["status"] == "completed":
                _log("transfer_already_completed", invoice_id=invoice_id)
                return None
            _log("transfer_lease_busy", invoice_id=invoice_id, status=claim["status"])
            raise LeaseBusyError("transfer lease is active")
        _log("transfer_processing_started", invoice_id=invoice_id)
        try:
            # Read the authoritative payment before calculating the transfer amount.
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
            # The external ID is stable and uses only characters accepted by Stark Bank.
            response = _with_retry(
                lambda: self._create_transfer(invoice_id, receipt.net_amount)
            )
            transfer = response[0]
            _log(
                "transfer_created",
                invoice_id=invoice_id,
                transfer_id=getattr(transfer, "id", ""),
                amount=receipt.net_amount,
            )
            # Mark completed only after Stark Bank confirms transfer creation.
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
            # Releasing the lease as retryable lets SQS retry the record later.
            _log_exception("transfer_failed", invoice_id=invoice_id)
            try:
                self.store.mark_retryable(invoice_id, claim["lease_token"])
            finally:
                raise

    @staticmethod
    def _create_transfer(invoice_id, amount):
        """Create one transfer using the configured destination account."""
        return starkbank.transfer.create(
            [
                starkbank.Transfer(
                    amount=amount,
                    external_id=f"starkbank-trial-{invoice_id}",
                    **DESTINATION,
                )
            ]
        )
