"""Encrypt secrets (Plaid access tokens) at rest with a per-goblin Fernet key."""
import os

from cryptography.fernet import Fernet


def fernet() -> Fernet:
    key = os.environ.get("FIELD_KEY", "")
    if not key:
        raise RuntimeError("FIELD_KEY not set; refusing to store secrets unencrypted")
    return Fernet(key.encode())


def encrypt(s: str) -> str:
    return fernet().encrypt(s.encode()).decode()


def decrypt(s: str) -> str:
    return fernet().decrypt(s.encode()).decode()
