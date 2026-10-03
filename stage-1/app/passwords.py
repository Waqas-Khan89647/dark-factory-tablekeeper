"""scrypt password hashing, run off the event loop in a bounded thread pool.

Hashes are stored as `scrypt$<n>$<r>$<p>$<salt hex>$<key hex>`, so a stored hash is
always verified with the parameters it was made with. The cost (n=2^12, r=8, p=1,
~30 ms on one vCPU) keeps 50 concurrent logins well inside the 5-second request budget
on the 2-vCPU limit.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from concurrent.futures import ThreadPoolExecutor

ALGORITHM = "scrypt"
N, R, P = 2 ** 12, 8, 1
MAX_N = 2 ** 16
MAXMEM = 128 * 1024 * 1024
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="scrypt")


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8", "surrogatepass"),
                          salt=salt, n=n, r=r, p=p, maxmem=MAXMEM)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key = _derive(password, salt, N, R, P)
    return f"{ALGORITHM}${N}${R}${P}${salt.hex()}${key.hex()}"


def _parse(encoded) -> tuple[int, int, int, bytes, bytes] | None:
    if not isinstance(encoded, str):
        return None
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != ALGORITHM:
        return None
    try:
        n, r, p = (int(x) for x in parts[1:4])
        salt, key = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    except ValueError:
        return None
    if not (1 < n <= MAX_N and n & (n - 1) == 0 and 1 <= r <= 16 and 1 <= p <= 4
            and salt and key):
        return None
    return n, r, p, salt, key


def is_valid_hash(encoded) -> bool:
    return _parse(encoded) is not None


def verify_password(password: str, encoded: str) -> bool:
    parsed = _parse(encoded)
    if parsed is None:
        return False
    n, r, p, salt, key = parsed
    try:
        return hmac.compare_digest(_derive(password, salt, n, r, p), key)
    except (ValueError, UnicodeError):
        return False


async def hash_async(password: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(_POOL, hash_password, password)


async def verify_async(password: str, encoded: str) -> bool:
    return await asyncio.get_running_loop().run_in_executor(
        _POOL, verify_password, password, encoded)
