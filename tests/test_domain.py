import pytest
from starkbank_trial.domain import receipt_from


def test_net_amount():
    assert receipt_from("i", {"amount": 1250, "fee": 25}).net_amount == 1225


def test_invalid_net():
    with pytest.raises(ValueError):
        receipt_from("i", {"amount": 100, "fee": 100}).net_amount
