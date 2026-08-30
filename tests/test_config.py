from starkbank_trial.config import _load_private_key


def test_private_key_normalizes_escaped_newlines(monkeypatch):
    monkeypatch.setenv(
        "STARK_PRIVATE_KEY",
        "  -----BEGIN EC PRIVATE KEY-----\nkey\n-----END EC PRIVATE KEY-----  ",
    )
    assert (
        _load_private_key()
        == "-----BEGIN EC PRIVATE KEY-----\nkey\n-----END EC PRIVATE KEY-----"
    )
