"""Small auth helpers shared by every role."""
import hashlib
import hmac

from fastapi import HTTPException, Request


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def check_token(presented: str | None, expected_sha256: str) -> bool:
    if not presented or not expected_sha256:
        return False
    return hmac.compare_digest(sha256(presented), expected_sha256)


def bearer(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:] if h.lower().startswith("bearer ") else None


def require_internal(request: Request, secret: str) -> None:
    """Service-to-service auth via the X-Internal-Secret header (fails closed)."""
    got = request.headers.get("x-internal-secret", "")
    if not secret or not hmac.compare_digest(got, secret):
        raise HTTPException(401, "bad internal secret")
