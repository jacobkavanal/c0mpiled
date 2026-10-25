import hashlib
import hmac
import secrets
import time
from uuid import UUID

from twilio.request_validator import RequestValidator

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def sign_call_token(secret: str, approval_id: UUID, ttl_seconds: int) -> str:
    """Token that lets one ConversationRelay session act on one approval request."""
    payload = f"{approval_id}.{int(time.time()) + ttl_seconds}"
    return f"{payload}.{_mac(secret, payload)}"


def verify_call_token(secret: str, token: str) -> UUID | None:
    try:
        approval_id, expires, mac = token.split(".")
        payload = f"{approval_id}.{expires}"
        if not hmac.compare_digest(mac, _mac(secret, payload)):
            return None
        if int(expires) < time.time():
            return None
        return UUID(approval_id)
    except ValueError:
        return None


def _mac(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(
        pin.encode(), salt=bytes.fromhex(salt_hex), n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


def twilio_signature_valid(
    auth_token: str, url: str, params: dict[str, str], signature: str | None
) -> bool:
    if not signature:
        return False
    return RequestValidator(auth_token).validate(url, params, signature)
