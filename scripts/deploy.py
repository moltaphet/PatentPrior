"""Deploy contracts/patent_prior.py to GenLayer Studio Next (chain 61997).

    .venv/bin/python scripts/deploy.py

Creates a clean deployer key under .keys/ on first run, funds it through the
network's sim_fundAccount faucet if it is short, deploys, verifies the deployment
by reading it back, and records the result in deployments/studio-next.json.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from eth_typing import ChecksumAddress

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chain import CHAIN_ID, EXPLORER, GEN, ROOT, RPC_URL, Chain, contract_address_from, load_or_create_key  # noqa: E402

CONTRACT_PATH = ROOT / "contracts" / "patent_prior.py"
OUT_PATH = ROOT / "deployments" / "studio-next.json"
DEPLOYER_FLOOR = 5 * GEN


def main() -> int:
    code = CONTRACT_PATH.read_bytes()
    source_sha256 = hashlib.sha256(code).hexdigest()
    runner = code.decode().splitlines()[1].split('"Depends": "')[1].split('"')[0]

    deployer = Chain(load_or_create_key("deployer"))
    print(f"deployer  {deployer.address}")
    if deployer.ensure_funded(DEPLOYER_FLOOR):
        print(f"funded    via sim_fundAccount -> {deployer.balance() / GEN:.4f} GEN")
    else:
        print(f"balance   {deployer.balance() / GEN:.4f} GEN")

    print("deploying contracts/patent_prior.py ...")
    result = deployer.deploy(code)
    address = contract_address_from(result)
    if not address:
        raise SystemExit(f"deploy decided but no contract address found: {json.dumps(result['receipt'], default=str)[:800]}")

    deployer.contract = cast(ChecksumAddress, address)
    constants = deployer.view("get_constants")
    governor = deployer.view("get_governor")
    assert str(constants["challenger_bond"]) == str(GEN // 10), constants
    assert governor.lower() == deployer.address.lower(), (governor, deployer.address)

    record = {
        "network": "studio-next",
        "chain_id": CHAIN_ID,
        "rpc_url": RPC_URL,
        "contract_address": address,
        "explorer_url": f"{EXPLORER}/address/{address}",
        "deployer": deployer.address,
        "deploy_tx_hash": result["tx_hash"],
        "deploy_tx_url": f"{EXPLORER}/tx/{result['tx_hash']}",
        "bytecode_sha256": source_sha256,
        "bytecode_bytes": len(code),
        "runner": runner,
        "source": "contracts/patent_prior.py",
        "deployed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "consensus_result": result["receipt"].get("result_name"),
        "execution_result": result["receipt"].get("txExecutionResultName"),
        "verified_by_readback": {"governor": governor, "challenger_bond_wei": constants["challenger_bond"]},
    }
    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
