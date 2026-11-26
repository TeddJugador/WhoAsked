"""Finance service settings from cdk.json context, validated at synth time.

The ledger's coordinates come from context, never from code: the ledger is a separate,
independently deployed stack (FinancialLedgerAPI) that publishes no CloudFormation
outputs (docs/ledger-contract.md, section 1). It is assumed to be in the same account
and region as this app.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from constructs import Node

_TABLE_NAME = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")
_SECRET_NAME = re.compile(r"^[A-Za-z0-9/_+=.@-]{1,512}$")
_PROVIDER = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
# API Gateway's default endpoint is
# https://<API ID: 10 lowercase letters/digits>.execute-api.<region>.amazonaws.com
_EXECUTE_API_HOST = re.compile(r"^([^.]+)\.execute-api\.[a-z0-9-]+\.amazonaws\.com$")
_API_ID = re.compile(r"^[a-z0-9]{10}$")
_PLACEHOLDERS = ("your-api-id", "your_api_id", "example", "replace", "placeholder", "changeme")


class FinanceConfigError(ValueError):
    pass


@dataclass(frozen=True)
class FinanceConfig:
    ledger_api_url: str  # e.g. https://abc123.execute-api.us-east-1.amazonaws.com
    ledger_signing_secret_name: str  # ledger/webhook-signing-secret
    ledger_table_name: str  # FinancialLedger
    # Deploy-time switch for the optional payout-provider resources (phase h). Which
    # schools *use* a provider is per-school configuration in the Finance table; with
    # no provider here, no payout resources exist and no payout secret is readable.
    payout_provider: str | None = None

    @classmethod
    def from_node(cls, node: Node) -> FinanceConfig:
        def get(key: str) -> Any:  # noqa: ANN401 - raw cdk.json context
            value = node.try_get_context(key)
            if value is None or value == "":
                raise FinanceConfigError(
                    f"missing cdk.json context value '{key}' (see README: finance setup)"
                )
            return value

        config = cls(
            ledger_api_url=str(get("financeLedgerApiUrl")).rstrip("/"),
            ledger_signing_secret_name=str(get("financeLedgerSigningSecretName")),
            ledger_table_name=str(get("financeLedgerTableName")),
            payout_provider=node.try_get_context("financePayoutProvider") or None,
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not re.fullmatch(
            r"https://[A-Za-z0-9.-]+(?::\d+)?(/[A-Za-z0-9._~-]+)*", self.ledger_api_url
        ):
            raise FinanceConfigError(
                "financeLedgerApiUrl must be the ledger API's https base URL, without a path "
                "to the webhook route"
            )
        host = self.ledger_api_url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
        if any(word in host.lower() for word in _PLACEHOLDERS):
            raise FinanceConfigError(
                "financeLedgerApiUrl still holds a placeholder: use the URL that "
                "`aws apigatewayv2 get-apis` prints for financial-ledger-api"
            )
        execute_api = _EXECUTE_API_HOST.fullmatch(host)
        if execute_api and not _API_ID.fullmatch(execute_api.group(1)):
            raise FinanceConfigError(
                "financeLedgerApiUrl: an execute-api host starts with a 10-character API ID "
                "(lowercase letters and digits)"
            )
        if self.ledger_api_url.endswith("/v1/webhooks/payment"):
            raise FinanceConfigError(
                "financeLedgerApiUrl is the base URL; the adapter adds the route"
            )
        if (
            not _SECRET_NAME.fullmatch(self.ledger_signing_secret_name)
            or "*" in self.ledger_signing_secret_name
        ):
            raise FinanceConfigError(
                "financeLedgerSigningSecretName must be a secret name, no wildcards"
            )
        if not _TABLE_NAME.fullmatch(self.ledger_table_name):
            raise FinanceConfigError("financeLedgerTableName must be a DynamoDB table name")
        if self.payout_provider is not None and not _PROVIDER.fullmatch(self.payout_provider):
            raise FinanceConfigError(
                "financePayoutProvider must be a short lowercase name, or null"
            )
