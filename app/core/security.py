import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from app.core.config import settings


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 260_000
    ).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, salt, stored_digest = password_hash.split("$", 2)
    except ValueError:
        return False

    if algorithm != "pbkdf2_sha256":
        return False

    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 260_000
    ).hex()
    return hmac.compare_digest(digest, stored_digest)


def make_temporary_password() -> str:
    return secrets.token_urlsafe(10)


def generate_numeric_code(length: int = 6) -> str:
    upper_bound = 10**length
    return f"{secrets.randbelow(upper_bound):0{length}d}"


def hash_one_time_code(code: str) -> str:
    normalized = "".join(character for character in code if character.isdigit())
    digest = hmac.new(
        settings.secret_key.encode("utf-8"),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"hmac_sha256${digest}"


def verify_one_time_code(code: str, code_hash: str | None) -> bool:
    if not code_hash:
        return False
    try:
        algorithm, stored_digest = code_hash.split("$", 1)
    except ValueError:
        return False
    if algorithm != "hmac_sha256":
        return False
    candidate_digest = hash_one_time_code(code).split("$", 1)[1]
    return hmac.compare_digest(candidate_digest, stored_digest)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def sign_session(payload: dict[str, Any], max_age_seconds: int = 43_200) -> str:
    body = dict(payload)
    body["exp"] = int(time.time()) + max_age_seconds
    encoded = _b64(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(
        settings.secret_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded}.{_b64(signature)}"


def unsign_session(token: str) -> dict[str, Any] | None:
    try:
        encoded, signature = token.split(".", 1)
    except ValueError:
        return None

    expected = hmac.new(
        settings.secret_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        received = _unb64(signature)
    except ValueError:
        return None

    if not hmac.compare_digest(expected, received):
        return None

    try:
        payload = json.loads(_unb64(encoded))
    except (ValueError, json.JSONDecodeError):
        return None

    if int(payload.get("exp", 0)) < int(time.time()):
        return None

    return payload
