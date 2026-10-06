"""The .fhpack container: a tar stream encrypted with AES-256-GCM in authenticated chunks.

Layout (version 1)
  magic      8 bytes  b"FHPACK\\x01\\n"
  hlen       4 bytes  big-endian length of the header
  header     JSON     {"version", "cipher", "kdf", "n", "r", "p", "salt", "nonce_prefix",
                       "chunk_size", "created_at", "check"}
  chunks     repeated: 4-byte big-endian length, then ciphertext + 16-byte GCM tag

Key = scrypt(passphrase, salt, n, r, p) -> 32 bytes. Each chunk is sealed with nonce =
nonce_prefix (4 random bytes) + 8-byte chunk counter, and associated data = SHA-256(magic +
hlen + header) + counter + a final-chunk flag. So chunks cannot be reordered, dropped,
truncated or spliced into another package, and any change to the header breaks every chunk.
``check`` is a sealed constant: a wrong passphrase is reported as such before any payload is
read, while later corruption is reported as an integrity failure with the chunk number.

Both directions stream: memory use is one chunk (1 MiB) regardless of package size.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from datetime import datetime, timezone
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"FHPACK\x01\n"
VERSION = 1
CHUNK = 1 << 20
MAX_HEADER = 64 * 1024
MAX_CHUNK = CHUNK + 1024
SCRYPT = {"n": 1 << 15, "r": 8, "p": 1}
CHECK_PLAINTEXT = b"face-hunger-package-key-check"


class PackageError(Exception):
    """Base class: the message is safe to show to the user."""


class WrongPassphrase(PackageError):
    pass


class IntegrityError(PackageError):
    pass


def _key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    if not passphrase:
        raise PackageError("A passphrase is required")
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode("utf-8"))


def _aad(header_hash: bytes, counter: int, final: bool) -> bytes:
    return header_hash + struct.pack(">QB", counter, 1 if final else 0)


class EncryptedWriter:
    """File-like object: write plaintext, sealed chunks go to ``raw``. Call close() to finish."""

    def __init__(self, raw: BinaryIO, passphrase: str, *, chunk_size: int = CHUNK, scrypt: dict | None = None):
        params = {**SCRYPT, **(scrypt or {})}
        salt, prefix = os.urandom(16), os.urandom(4)
        self._aes = AESGCM(_key(passphrase, salt, **params))
        self._prefix = prefix
        header = {"version": VERSION, "cipher": "AES-256-GCM", "kdf": "scrypt", **params,
                  "salt": base64.b64encode(salt).decode(), "nonce_prefix": base64.b64encode(prefix).decode(),
                  "chunk_size": chunk_size, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        # The key check is sealed with a nonce outside the chunk counter space (prefix + all ones).
        check = self._aes.encrypt(prefix + b"\xff" * 8, CHECK_PLAINTEXT, MAGIC)
        header["check"] = base64.b64encode(check).decode()
        blob = json.dumps(header, sort_keys=True).encode()
        head = MAGIC + struct.pack(">I", len(blob)) + blob
        raw.write(head)
        self._hash = hashlib.sha256(head).digest()
        self._raw, self._chunk, self._buf, self._counter, self._closed = raw, chunk_size, bytearray(), 0, False
        self.bytes_written = len(head)

    def _seal(self, data: bytes, final: bool) -> None:
        nonce = self._prefix + struct.pack(">Q", self._counter)
        sealed = self._aes.encrypt(nonce, data, _aad(self._hash, self._counter, final))
        self._raw.write(struct.pack(">I", len(sealed)) + sealed)
        self.bytes_written += 4 + len(sealed)
        self._counter += 1

    def write(self, data) -> int:
        self._buf += data
        while len(self._buf) > self._chunk:  # keep one chunk back so the last one can be flagged final
            self._seal(bytes(self._buf[:self._chunk]), False)
            del self._buf[:self._chunk]
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._seal(bytes(self._buf), True)
            self._buf.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.close()


def read_header(raw: BinaryIO) -> tuple[dict, bytes]:
    magic = raw.read(len(MAGIC))
    if magic != MAGIC:
        raise PackageError("This is not a Face Hunger package (.fhpack)")
    size_bytes = raw.read(4)
    if len(size_bytes) != 4:
        raise IntegrityError("The package is truncated")
    (size,) = struct.unpack(">I", size_bytes)
    if size > MAX_HEADER:
        raise IntegrityError("The package header is damaged")
    blob = raw.read(size)
    try:
        header = json.loads(blob)
        if header.get("version") != VERSION:
            raise PackageError(f"Unsupported package version {header.get('version')}; update Face Hunger to open it")
        if header.get("cipher") != "AES-256-GCM" or header.get("kdf") != "scrypt":
            raise PackageError("Unsupported package encryption")
        n, r, p = int(header["n"]), int(header["r"]), int(header["p"])
        # Refuse absurd KDF costs (a crafted header must not make us allocate gigabytes).
        if not (1 << 10 <= n <= 1 << 20) or n & (n - 1) or not 1 <= r <= 16 or not 1 <= p <= 4:
            raise IntegrityError("The package header is damaged")
        if not 1024 <= int(header["chunk_size"]) <= CHUNK:
            raise IntegrityError("The package header is damaged")
    except (ValueError, KeyError, TypeError) as exc:
        raise IntegrityError("The package header is damaged") from exc
    return header, hashlib.sha256(magic + size_bytes + blob).digest()


class DecryptedReader:
    """File-like object yielding the verified plaintext of a package."""

    def __init__(self, raw: BinaryIO, passphrase: str):
        self.header, self._hash = read_header(raw)
        try:
            salt = base64.b64decode(self.header["salt"])
            self._prefix = base64.b64decode(self.header["nonce_prefix"])
            check = base64.b64decode(self.header["check"])
        except (ValueError, KeyError) as exc:
            raise IntegrityError("The package header is damaged") from exc
        self._aes = AESGCM(_key(passphrase, salt, int(self.header["n"]), int(self.header["r"]), int(self.header["p"])))
        try:
            if self._aes.decrypt(self._prefix + b"\xff" * 8, check, MAGIC) != CHECK_PLAINTEXT:
                raise InvalidTag
        except InvalidTag as exc:
            raise WrongPassphrase("Wrong passphrase") from exc
        self._raw, self._counter, self._buf, self._done = raw, 0, b"", False
        self._max = int(self.header["chunk_size"]) + 16 + 8

    def _next(self) -> bytes:
        size_bytes = self._raw.read(4)
        if len(size_bytes) < 4:
            raise IntegrityError("The package is truncated (it ends before its final block)")
        (size,) = struct.unpack(">I", size_bytes)
        if size < 16 or size > self._max:
            raise IntegrityError(f"Integrity check failed at block {self._counter}: the package was modified or is damaged")
        sealed = self._raw.read(size)
        nonce = self._prefix + struct.pack(">Q", self._counter)
        for final in (False, True):
            try:
                data = self._aes.decrypt(nonce, sealed, _aad(self._hash, self._counter, final))
            except InvalidTag:
                continue
            self._counter += 1
            if final:
                self._done = True
                if self._raw.read(1):
                    raise IntegrityError("Unexpected data after the end of the package")
            return data
        raise IntegrityError(f"Integrity check failed at block {self._counter}: the package was modified or is damaged")

    def read(self, size: int = -1) -> bytes:
        chunks = [self._buf]
        have = len(self._buf)
        while (size < 0 or have < size) and not self._done:
            data = self._next()
            chunks.append(data)
            have += len(data)
        data = b"".join(chunks)
        if size < 0:
            self._buf = b""
            return data
        self._buf = data[size:]
        return data[:size]

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def verify_to_end(self) -> None:
        """Consume the rest; raises IntegrityError unless the final block is reached."""
        while not self._done:
            self._next()
        self._buf = b""
