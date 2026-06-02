"""Safe signed account checks for Toobit."""

from __future__ import annotations

from triak_trade.exchange.base import SignedCheckResult
from triak_trade.exchange.toobit.client import ToobitClient


class ToobitAccountClient:
    def __init__(self, client: ToobitClient, safe_account_path: str) -> None:
        self.client = client
        self.safe_account_path = safe_account_path

    async def safe_account_check(self) -> SignedCheckResult:
        if not self.safe_account_path.strip():
            return SignedCheckResult(
                success=True,
                skipped=True,
                endpoint_path=None,
                response_type=None,
                key_accepted=None,
                message="TOOBIT_SAFE_ACCOUNT_PATH not configured; signed account check skipped",
            )
        payload = await self.client.signed_request("GET", self.safe_account_path)
        return SignedCheckResult(
            success=True,
            skipped=False,
            endpoint_path=self.safe_account_path,
            response_type=type(payload).__name__,
            key_accepted=True,
            message="signed account endpoint reachable",
        )
