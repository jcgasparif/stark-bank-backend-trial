import pytest
from starkbank_trial.domain import DESTINATION,receipt_from
from starkbank_trial.scheduler import run_for_24_hours
def test_net_amount(): assert receipt_from("i",{"amount":1250,"fee":25}).net_amount==1225
def test_invalid_net():
    with pytest.raises(ValueError): receipt_from("i",{"amount":100,"fee":100}).net_amount
def test_destination(): assert DESTINATION["bank_code"]=="20018183" and DESTINATION["account_type"]=="payment"
def test_scheduler():
    calls=[]; run_for_24_hours(type("C",(),{"issue_batch":lambda self,a,b:calls.append((a,b))})(),sleep=lambda _:None); assert len(calls)==8 and all(x==(8,12) for x in calls)
