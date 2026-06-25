"""Toobit HMAC signer."""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode


class ToobitSigner:
    def __init__(self, secret: str) -> None:
        self._secret = secret

    def sign(self, params: dict[str, object]) -> str:
        # Toobit signs the query string IN INSERTION ORDER (same order sent in URL).
        # Sorting alphabetically breaks the signature when recvWindow is present
        # because 'recvWindow' sorts before 'timestamp' but the URL sends timestamp first.
        pairs = [(key, str(value)) for key, value in params.items()]
        query = urlencode(pairs)
        digest = hmac.new(self._secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256)
        return digest.hexdigest().lower()

    def __repr__(self) -> str:
        return "ToobitSigner(secret=**redacted**)"
