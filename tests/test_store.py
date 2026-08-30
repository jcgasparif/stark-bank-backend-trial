from starkbank_trial.store import Store


def test_transfer_claim_is_exclusive_until_completed(tmp_path):
    store = Store(tmp_path / "state.sqlite3")

    first = store.claim("invoice-1", "now")
    second = store.claim("invoice-1", "later")

    assert first["claimed"] is True
    assert second == {"claimed": False, "status": "processing"}

    store.complete("invoice-1", 975, "transfer-1", first["lease_token"])
    assert store.claim("invoice-1", "again")["status"] == "completed"


def test_invoice_request_is_idempotent(tmp_path):
    store = Store(tmp_path / "state.sqlite3")

    first = store.claim_invoice_creation("run-1:0", "now")
    store.complete_invoice_creation("run-1:0", "invoice-1", first["lease_token"])
    second = store.claim_invoice_creation("run-1:0", "again")

    assert second == {
        "claimed": False,
        "status": "completed",
        "invoice_id": "invoice-1",
    }
