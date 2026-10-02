"""Shared Studio Next client helpers for deploy.py and interact_live.py.

Studio Next is GenLayer chain 61997. studio-next.genlayer.com and
studio-dev.genlayer.com serve the same chain; the endpoint is pinned explicitly
because genlayer-py's bundled `studio_devnet` preset still names studio-dev.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, TypeVar, cast

from eth_typing import ChecksumAddress
from genlayer_py import create_account, create_client
from genlayer_py.chains import studio_devnet  # type: ignore[attr-defined]

ROOT = Path(__file__).resolve().parent.parent
KEY_DIR = ROOT / ".keys"
RPC_URL = "https://studio-next.genlayer.com/api"
EXPLORER = "https://explorer-studio-next.genlayer.com"
CHAIN_ID = 61997
GEN = 10**18

T = TypeVar("T")


class ChainError(Exception):
    """A transaction reverted, or consensus did not reach agreement."""


def retry(fn: Callable[[], T], attempts: int = 4, wait_s: float = 5.0) -> T:
    """Retry transient transport failures; ChainError is a verdict, never retried."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except ChainError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport errors are heterogeneous
            last = exc
            if i + 1 < attempts:
                time.sleep(wait_s * (i + 1))
    raise ChainError(f"transient RPC failure after {attempts} attempts: {last}")


_PRE_SEND_ERRORS = ("Can't assign requested address", "Connection refused", "Name or service not known")


def submit_once(fn: Callable[[], T], attempts: int = 5, wait_s: float = 4.0) -> T:
    """Submit a transaction at most once. Only errors raised while *connecting* (before any
    byte of the request leaves the machine) are retried; anything else could mean the
    transaction was accepted, so it propagates rather than risk a duplicate write."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if i + 1 < attempts and any(m in str(exc) for m in _PRE_SEND_ERRORS):
                time.sleep(wait_s * (i + 1))
                continue
            raise
    raise ChainError("unreachable")


def load_or_create_key(name: str) -> Any:
    """Persist one fresh ECDSA key per role under .keys/ (chmod 600, gitignored)."""
    KEY_DIR.mkdir(exist_ok=True)
    path = KEY_DIR / f"{name}.key.json"
    if path.exists():
        key = json.loads(path.read_text())["private_key"]
        return create_account(key if key.startswith("0x") else "0x" + key)
    account = create_account()
    path.write_text(json.dumps({"name": name, "private_key": account.key.hex()}, indent=2))
    os.chmod(path, 0o600)
    return account


class Chain:
    """One account bound to Studio Next, with optional contract address."""

    def __init__(self, account: Any, contract: str | None = None) -> None:
        self.account = account
        self.client = create_client(chain=studio_devnet, endpoint=RPC_URL, account=account)
        self.contract: ChecksumAddress | None = cast(ChecksumAddress, contract) if contract else None

    def _bound(self) -> ChecksumAddress:
        if self.contract is None:
            raise ChainError("no contract bound")
        return self.contract

    @property
    def address(self) -> str:
        return cast(str, self.account.address)

    def balance(self, who: str | None = None) -> int:
        target = who or self.address
        resp = retry(lambda: self.client.provider.make_request("eth_getBalance", [target, "latest"]))
        return int(resp.get("result", "0x0"), 16)

    def ensure_funded(self, target_wei: int) -> bool:
        """Top up through the network's sim_fundAccount faucet. True if a top-up was sent."""
        current = self.balance()
        if current >= target_wei:
            return False
        retry(lambda: self.client.fund_account(cast(ChecksumAddress, self.address), target_wei - current + 1))
        return True

    def view(self, method: str, args: list[Any] | None = None) -> Any:
        addr = self._bound()
        return retry(lambda: self.client.read_contract(addr, method, args=args or []))

    def _fees(self, method: str | None, args: list[Any], value: int) -> dict[str, Any]:
        """Prefer a simulated estimate; fall back to the policy estimate when the
        simulation clock disagrees with the block clock."""
        from genlayer_py.contracts.actions import (  # noqa: PLC0415
            _estimate_transaction_fees_with_policy,
            get_current_fee_policy,
        )

        est: Any = None
        if method and self.contract is not None:
            addr = self.contract
            try:
                est = retry(
                    lambda: self.client.estimate_transaction_fees_for_write(
                        addr, method, args=args, value=value
                    ),
                    attempts=1,
                )
            except Exception:  # noqa: BLE001
                est = None
        if est is None:
            est = _estimate_transaction_fees_with_policy(
                self.client, None, get_current_fee_policy(self.client)
            )
        fees: dict[str, Any] = {
            "distribution": est["distribution"],
            "feeValue": est.get("feeValue") or est.get("fee_value") or 0,
        }
        if est.get("messageAllocations") is not None:
            fees["messageAllocations"] = est["messageAllocations"]
        return fees

    def fetch(self, tx_hash: str) -> dict[str, Any]:
        """Re-read a decided transaction, shaped like the result of write()."""
        tx = retry(lambda: cast(dict[str, Any], self.client.get_transaction(cast(Any, tx_hash))))
        return {"tx_hash": tx_hash, "receipt": {}, "tx": tx}

    def _wait(self, tx_hash: Any, label: str) -> dict[str, Any]:
        receipt = retry(
            lambda: cast(
                dict[str, Any],
                self.client.wait_for_transaction_receipt(
                    tx_hash,
                    wait_until="decided",  # type: ignore[call-arg]
                    interval=4,
                    retries=150,
                ),
            ),
            attempts=3,
        )
        exec_name = receipt.get("txExecutionResultName") or receipt.get("tx_execution_result_name")
        consensus = receipt.get("result_name")
        if exec_name is not None and exec_name != "FINISHED_WITH_RETURN":
            raise ChainError(f"{label}: execution {exec_name}")
        if consensus is not None and consensus != "MAJORITY_AGREE":
            raise ChainError(f"{label}: consensus {consensus}")
        return receipt

    def write(
        self,
        method: str,
        args: list[Any] | None = None,
        value: int = 0,
        label: str = "",
        simulate: bool = True,
    ) -> dict[str, Any]:
        """Submit a write, block until decided, return {tx_hash, receipt, tx}."""
        addr = self._bound()
        args = args or []
        # Simulating a write that runs web + LLM calls executes the whole non-deterministic
        # round before anything is sent and can stall for minutes, so those writes pass
        # simulate=False and use the policy-derived fee estimate instead.
        fees = self._fees(method if simulate else None, args, value)
        tx_hash = submit_once(
            lambda: self.client.write_contract(addr, method, args=args, value=value, fees=fees)
        )
        receipt = self._wait(tx_hash, label or method)
        tx = retry(lambda: cast(dict[str, Any], self.client.get_transaction(tx_hash)))
        return {"tx_hash": _hex(tx_hash), "receipt": receipt, "tx": tx}

    def deploy(self, code: bytes, args: list[Any] | None = None) -> dict[str, Any]:
        fees = self._fees(None, [], 0)
        tx_hash = submit_once(lambda: self.client.deploy_contract(code=code, args=args or [], fees=fees))
        receipt = self._wait(tx_hash, "deploy")
        tx = retry(lambda: cast(dict[str, Any], self.client.get_transaction(tx_hash)))
        return {"tx_hash": _hex(tx_hash), "receipt": receipt, "tx": tx}


def _hex(h: Any) -> str:
    s = h.hex() if hasattr(h, "hex") else str(h)
    return s if s.startswith("0x") else "0x" + s


def contract_address_from(result: dict[str, Any]) -> str | None:
    """Deployed address, wherever this SDK/network version puts it."""
    tx, receipt = result["tx"], result["receipt"]
    for src in (
        (tx.get("txDataDecoded") or {}).get("contractAddress"),
        (tx.get("data") or {}).get("contract_address") if isinstance(tx.get("data"), dict) else None,
        (receipt.get("data") or {}).get("contract_address") if isinstance(receipt.get("data"), dict) else None,
        tx.get("to_address"),
        tx.get("recipient"),
        receipt.get("contractAddress"),
        receipt.get("contract_address"),
    ):
        if isinstance(src, str) and src.startswith("0x") and len(src) == 42:
            return src
    return None
