from uuid import uuid4

from gateway.security import hash_pin, sign_call_token, verify_call_token, verify_pin


def test_call_token_round_trip():
    approval_id = uuid4()
    token = sign_call_token("secret", approval_id, ttl_seconds=60)
    assert verify_call_token("secret", token) == approval_id


def test_call_token_rejects_wrong_secret_tampering_and_expiry():
    approval_id = uuid4()
    token = sign_call_token("secret", approval_id, ttl_seconds=60)
    assert verify_call_token("other-secret", token) is None
    assert verify_call_token("secret", token.replace(str(approval_id), str(uuid4()))) is None
    assert (
        verify_call_token("secret", sign_call_token("secret", approval_id, ttl_seconds=-1)) is None
    )
    assert verify_call_token("secret", "garbage") is None


def test_pin_hash():
    stored = hash_pin("4821")
    assert "4821" not in stored
    assert verify_pin("4821", stored)
    assert not verify_pin("4820", stored)
    assert not verify_pin("4821", "not-a-hash")
