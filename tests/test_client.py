from types import SimpleNamespace
from unittest.mock import Mock

import starkbank
import pytest

from starkbank_trial.client import StarkClient, _cpf_for_name, _random_cpf, _with_retry


def _client():
    client = object.__new__(StarkClient)
    client.settings = SimpleNamespace(invoice_min_amount=1000, invoice_max_amount=5000)
    client.store = Mock()
    client.store.claim_invoice_creation.return_value = {
        "claimed": True,
        "status": "processing",
        "lease_token": "lease-1",
    }
    client.store.mark_retryable.return_value = None
    return client


def test_random_cpf_has_valid_check_digits():
    cpf = _random_cpf()
    assert len(cpf) == 11
    assert len(set(cpf)) > 1
    for position in (9, 10):
        total = sum(
            int(digit) * (position + 1 - index)
            for index, digit in enumerate(cpf[:position])
        )
        assert int(cpf[position]) == (total * 10) % 11 % 10


def test_cpf_is_stable_for_the_same_customer_name():
    first = _cpf_for_name("Ana Silva")
    second = _cpf_for_name("Ana Silva")

    assert first == second
    assert len(first) == 11
    for position in (9, 10):
        total = sum(
            int(digit) * (position + 1 - index)
            for index, digit in enumerate(first[:position])
        )
        assert int(first[position]) == (total * 10) % 11 % 10


def test_issue_batch_passes_a_list_to_starkbank(monkeypatch):
    client = _client()
    created = SimpleNamespace(id="invoice-1")
    monkeypatch.setattr(starkbank.invoice, "query", lambda **_: iter(()))

    def create(invoices):
        assert isinstance(invoices, list) and len(invoices) == 1
        assert len(invoices) == 1
        assert isinstance(invoices[0], starkbank.Invoice)
        return [created]

    monkeypatch.setattr(starkbank.invoice, "create", create)

    assert client.issue_batch(minimum=1, maximum=1, idempotency_key="same-run") == [
        created
    ]
    client.store.save_invoice.assert_called_once()
    client.store.complete_invoice_creation.assert_called_once_with(
        "same-run:0", "invoice-1", "lease-1"
    )


def test_issue_batch_recovers_invoice_from_query_generator(monkeypatch):
    client = _client()
    existing = SimpleNamespace(id="invoice-existing")
    monkeypatch.setattr(
        starkbank.invoice, "query", lambda **_: (invoice for invoice in [existing])
    )
    monkeypatch.setattr(
        starkbank.invoice,
        "create",
        lambda *_: pytest.fail("must not create when query finds an invoice"),
    )

    result = client.issue_batch(minimum=1, maximum=1, idempotency_key="same-run")

    assert result == [existing]
    client.store.save_invoice.assert_called_once()
    client.store.complete_invoice_creation.assert_called_once_with(
        "same-run:0", "invoice-existing", "lease-1"
    )


def test_issue_batch_uses_distinct_customer_names(monkeypatch):
    client = _client()
    invoices_created = []
    monkeypatch.setattr(starkbank.invoice, "query", lambda **_: iter(()))

    def create(invoices):
        invoices_created.extend(invoices)
        return [SimpleNamespace(id=f"invoice-{len(invoices_created)}")]

    monkeypatch.setattr(starkbank.invoice, "create", create)

    client.issue_batch(minimum=12, maximum=12, idempotency_key="distinct-names")

    names = [invoice.name for invoice in invoices_created]
    assert len(names) == len(set(names)) == 12


def test_transfer_passes_a_list_to_starkbank(monkeypatch):
    client = _client()
    client.store.claim.return_value = {
        "claimed": True,
        "status": "processing",
        "lease_token": "lease-1",
    }
    created = SimpleNamespace(id="transfer-1")

    monkeypatch.setattr(
        starkbank.invoice, "payment", lambda _: {"amount": 1250, "fee": 25}
    )

    def create(transfers):
        assert isinstance(transfers, list)
        assert len(transfers) == 1
        assert isinstance(transfers[0], starkbank.Transfer)
        assert transfers[0].external_id == "starkbank-trial-invoice-1"
        return [created]

    monkeypatch.setattr(starkbank.transfer, "create", create)

    assert client.transfer_paid_invoice("invoice-1", None) == 1225
    client.store.complete.assert_called_once_with(
        "invoice-1", 1225, "transfer-1", "lease-1"
    )


def test_issue_batch_reuses_completed_invoice_request(monkeypatch):
    client = _client()
    client.store.claim_invoice_creation.return_value = {
        "claimed": False,
        "status": "completed",
        "invoice_id": "invoice-existing",
    }
    monkeypatch.setattr(
        starkbank.invoice, "create", lambda *_: pytest.fail("must not create")
    )

    result = client.issue_batch(minimum=1, maximum=1, idempotency_key="same-run")

    assert result[0].id == "invoice-existing"


def test_with_retry_uses_exponential_backoff(monkeypatch):
    attempts = iter([RuntimeError("temporary"), RuntimeError("temporary"), "ok"])
    delays = []
    monkeypatch.setattr("starkbank_trial.client.time.sleep", delays.append)

    def operation():
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    assert _with_retry(operation, attempts=3, base_delay=0.25) == "ok"
    assert delays == [0.25, 0.5]
