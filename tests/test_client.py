from types import SimpleNamespace
from unittest.mock import Mock

import starkbank

from starkbank_trial.client import StarkClient, _random_cpf


def _client():
    client = object.__new__(StarkClient)
    client.settings = SimpleNamespace(invoice_min_amount=1000, invoice_max_amount=5000)
    client.store = Mock()
    return client
def test_random_cpf_has_valid_check_digits():
    cpf = _random_cpf()
    assert len(cpf) == 11
    assert len(set(cpf)) > 1
    for position in (9, 10):
        total = sum(int(digit) * (position + 1 - index) for index, digit in enumerate(cpf[:position]))
        assert int(cpf[position]) == (total * 10) % 11 % 10




def test_issue_batch_passes_a_list_to_starkbank(monkeypatch):
    client = _client()
    created = SimpleNamespace(id="invoice-1")

    def create(invoices):
        assert isinstance(invoices, list)
        assert len(invoices) == 1
        assert isinstance(invoices[0], starkbank.Invoice)
        return [created]

    monkeypatch.setattr(starkbank.invoice, "create", create)

    assert client.issue_batch(minimum=1, maximum=1) == [created]
    client.store.save_invoice.assert_called_once()


def test_transfer_passes_a_list_to_starkbank(monkeypatch):
    client = _client()
    client.store.claim.return_value = True
    created = SimpleNamespace(id="transfer-1")

    monkeypatch.setattr(starkbank.invoice, "payment", lambda _: {"amount": 1250, "fee": 25})

    def create(transfers):
        assert isinstance(transfers, list)
        assert len(transfers) == 1
        assert isinstance(transfers[0], starkbank.Transfer)
        return [created]

    monkeypatch.setattr(starkbank.transfer, "create", create)

    assert client.transfer_paid_invoice("invoice-1", None) == 1225
    client.store.complete.assert_called_once_with("invoice-1", 1225, "transfer-1")
