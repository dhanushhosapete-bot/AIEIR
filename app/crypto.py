"""Field encryption for founder text at rest (messages, reflections, intake answers,
classifier rationales, review notes, feedback comments).

AES-256-GCM with a random 96-bit nonce. The founder's id is bound in as associated data,
so a ciphertext copied onto another founder's row will not decrypt.

Keys: AIEIR_DATA_KEYS="k2:<base64 32 bytes>,k1:<base64 32 bytes>". The first key encrypts;
any listed key decrypts, which allows rotation. In production these come from a KMS or
secret manager, never from the database. Generate one with:
    python -c "import os,base64;print('k1:'+base64.b64encode(os.urandom(32)).decode())"
"""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "enc1"


class CryptoError(RuntimeError):
    pass


def _keys() -> list[tuple[str, bytes]]:
    raw = os.environ.get("AIEIR_DATA_KEYS", "")
    keys = []
    for part in filter(None, (p.strip() for p in raw.split(","))):
        kid, _, b64 = part.partition(":")
        key = base64.b64decode(b64)
        if len(key) != 32:
            raise CryptoError(f"key {kid} must be 32 bytes")
        keys.append((kid, key))
    if not keys:
        raise CryptoError("AIEIR_DATA_KEYS is not set; refusing to store founder text unencrypted")
    return keys


def encrypt(plaintext: str | None, user_id: str) -> str | None:
    if plaintext is None:
        return None
    kid, key = _keys()[0]
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), str(user_id).encode())
    return f"{PREFIX}:{kid}:{base64.b64encode(nonce + ct).decode()}"


def decrypt(token: str | None, user_id: str) -> str | None:
    if token is None:
        return None
    try:
        prefix, kid, b64 = token.split(":", 2)
    except ValueError as e:
        raise CryptoError("not an encrypted value") from e
    if prefix != PREFIX:
        raise CryptoError("unknown format")
    key = dict(_keys()).get(kid)
    if key is None:
        raise CryptoError(f"key {kid} not available")
    blob = base64.b64decode(b64)
    try:
        return AESGCM(key).decrypt(blob[:12], blob[12:], str(user_id).encode()).decode()
    except Exception as e:
        raise CryptoError("decryption failed (wrong key or wrong founder)") from e
