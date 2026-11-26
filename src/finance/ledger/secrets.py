"""The ledger's signing key: Secrets Manager, JSON with a ``signing_key`` field (contract
section 2). Cached for five minutes per container."""

from __future__ import annotations

import json

from aws_lambda_powertools.utilities import parameters

CACHE_SECONDS = 300


class SigningKeyError(RuntimeError):
    pass


def load_signing_key(secret_name: str) -> str:
    raw = parameters.get_secret(secret_name, max_age=CACHE_SECONDS)
    try:
        key = json.loads(str(raw))["signing_key"]
    except (ValueError, KeyError, TypeError):
        raise SigningKeyError("the ledger secret has no signing_key field") from None
    if not isinstance(key, str) or not key:
        raise SigningKeyError("the ledger secret's signing_key is empty")
    return key
