"""Small auth helpers shared by every role."""
import base64
import hashlib
import hmac
import os
import time

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


# ---------------------------------------------------------------- passwords people choose
# A password a person picks has far less entropy than a random 256-bit token, so it is never kept as a plain sha256: it is salted and run through scrypt
# with the OWASP-recommended minimum cost (N=2^17, r=8, p=1, about 128 MiB and a fraction of a second). Random tokens (api tokens, the admin password,
# the per-goblin fallback token) are high entropy, so the fast sha256 check above stays correct for them.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 17, 8, 1
MIN_PASSWORD = 12


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=256 * 1024 * 1024)


def check_password_strength(password: str, name: str = "") -> None:
    """Raises ValueError with a plain message if the password is too weak to be a login."""
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"a password needs at least {MIN_PASSWORD} characters (a short phrase of a few words works well)")
    if name and password.lower() == name.lower():
        raise ValueError("a password cannot be the same as the name")


def hash_password(password: str) -> str:
    """'scrypt$N$r$p$salt$hash', salt and hash together, safe to store."""
    salt = os.urandom(16)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(_scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P))}"


def verify_password(password: str | None, stored: str | None) -> bool:
    if not password or not stored:
        return False
    try:
        scheme, n, r, p, salt, want = stored.split("$")
        if scheme != "scrypt":
            return False
        got = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
        return hmac.compare_digest(got, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


class Throttle:
    """Slows down guessing: after a few failures for a name, further attempts for it are refused for a while, doubling each time up to 15 minutes.
    Kept in memory only (nothing logged, gone on restart). A right answer clears it. The cap keeps a stranger from locking someone out for long."""
    FREE = 5            # failures before any delay
    MAX_WAIT = 900.0
    MAX_KEYS = 5000

    def __init__(self, clock=None):
        self.clock = clock or (lambda: time.monotonic())
        self.state: dict[str, list[float]] = {}          # key -> [failures, locked_until]

    def wait_for(self, key: str) -> float:
        s = self.state.get(key)
        return max(0.0, s[1] - self.clock()) if s else 0.0

    def check(self, key: str) -> None:
        w = self.wait_for(key)
        if w > 0:
            raise HTTPException(429, f"too many wrong attempts; try again in {int(w) + 1} seconds", headers={"Retry-After": str(int(w) + 1)})

    def fail(self, key: str) -> None:
        if key not in self.state and len(self.state) >= self.MAX_KEYS:
            self.state.pop(next(iter(self.state)))
        s = self.state.setdefault(key, [0, 0.0])
        s[0] += 1
        if s[0] > self.FREE:
            s[1] = self.clock() + min(self.MAX_WAIT, 2.0 ** (s[0] - self.FREE))

    def ok(self, key: str) -> None:
        self.state.pop(key, None)
