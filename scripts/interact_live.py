"""Drive four realistic patent cases through the deployed PatentPrior contract on
GenLayer Studio Next and record every transaction with its validator consensus.

    .venv/bin/python scripts/interact_live.py

Case 1  software patent, invalidated by a 2018 arXiv paper      -> INVALIDATED
Case 2  hardware architecture, prior art post-dates priority    -> VALID, bond slashed
Case 3  dead link                                               -> AMBIGUOUS_VOID, full refund
Case 4  active patent open for community prior-art submissions  -> stays ACTIVE

Case 5  (probe) a www. citation, stored and fetched as the bare host -> was the redirect followed?

Also demonstrated live: FIFO adjudication (a second challenger queued behind case 1 is
voided and refunded when case 1 invalidates the patent) and rescue_excess (stray funds
credited to the contract by the simulator are swept by the governor).

The tribunal is a live multi-validator LLM round, so outcomes are observed, not
scripted: the run records what consensus decided and exits non-zero if that
differs from the expectation above.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from eth_typing import ChecksumAddress

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chain import EXPLORER, GEN, ROOT, Chain, load_or_create_key, retry  # noqa: E402

DEPLOYMENT = ROOT / "deployments" / "studio-next.json"
PROGRESS = ROOT / ".keys" / "live-progress.json"  # resumable: completed steps are never re-sent
BOND = GEN // 10

CASES: list[dict[str, Any]] = [
    {
        "case": 1,
        "name": "Software patent invalidated by a 2018 arXiv paper",
        "title": "Bidirectional Masked-Token Language Representation Pre-training",
        "priority": "2020-03-01",
        "claim": (
            "A method comprising: pre-training a deep bidirectional language representation model "
            "from unlabeled text by jointly conditioning on both left and right context in all "
            "layers of a transformer; and fine-tuning the pre-trained model with one additional "
            "output layer to produce a task-specific model for question answering or language inference."
        ),
        "bounty": GEN // 2,
        "url": "https://arxiv.org/abs/1810.04805",
        "claimed": "2018-10-11",
        "expect": "INVALIDATED",
    },
    {
        "case": 2,
        "name": "Hardware architecture: cited prior art post-dates priority, bond slashed",
        "title": "Weight-Stationary Systolic Matrix Unit for Neural Network Inference",
        "priority": "2015-01-15",
        "claim": (
            "A neural network inference accelerator comprising: a matrix multiply unit formed as a "
            "systolic array of multiply-accumulate cells; a unified buffer holding input activations; "
            "a weight FIFO that stages weights into the array; and accumulators collecting the array "
            "output, wherein the matrix multiply unit performs 8-bit integer multiplications."
        ),
        "bounty": GEN // 5,
        "url": "https://arxiv.org/abs/1704.04760",
        "claimed": "2017-04-16",
        "expect": "VALID",
    },
    {
        "case": 3,
        "name": "Ambiguous: unreachable source, full refund",
        "title": "Hash-Chained Timestamp Notarization With Merkle Batching",
        "priority": "2021-06-01",
        "claim": (
            "A method of notarizing documents comprising: hashing each document, aggregating the "
            "hashes into a Merkle tree, and anchoring the Merkle root in a hash-chained ledger entry "
            "that carries a trusted timestamp."
        ),
        "bounty": GEN // 10,
        "url": "https://arxiv.org/abs/9912.99999",
        "claimed": "1999-12-01",
        "expect": "AMBIGUOUS_VOID",
    },
]

CASE4 = {
    "case": 4,
    "name": "Active patent open for community prior-art submissions",
    "title": "Verifiable-Delay Leader Election For Permissionless Ledgers",
    "priority": "2022-02-02",
    "claim": (
        "A method of electing a block proposer comprising: evaluating a verifiable delay function "
        "over a public randomness beacon, ordering candidates by the delay function output, and "
        "selecting the candidate with the lowest output as proposer for the slot."
    ),
    "bounty": GEN // 2,
    "extra_funding": GEN // 4,
}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def consensus_record(result: dict[str, Any]) -> dict[str, Any]:
    """Pull the validator-consensus evidence out of a decided transaction."""
    tx = result["tx"]
    cd = tx.get("consensus_data") or {}
    last = tx.get("last_round") or {}
    leaders = cd.get("leader_receipt") or []
    validators = cd.get("validators") or []
    state_hashes = {
        "leader": [r.get("contract_state_hash") for r in leaders],
        "validators": [
            {"address": (v.get("node_config") or {}).get("address"), "vote": v.get("vote"),
             "contract_state_hash": v.get("contract_state_hash")}
            for v in validators
        ],
    }
    return {
        "tx_hash": result["tx_hash"],
        "tx_url": f"{EXPLORER}/tx/{result['tx_hash']}",
        "result_name": tx.get("result_name"),
        "execution": tx.get("txExecutionResultName"),
        "lifecycle": tx.get("lifecycle"),
        "votes": cd.get("votes"),
        "validator_votes_hash": last.get("validator_votes_hash"),
        "validator_votes_name": last.get("validator_votes_name"),
        "state_hashes": state_hashes,
        "eq_blocks_outputs": tx.get("eq_blocks_outputs"),
        "rotations": tx.get("rotation_count"),
        "consensus_digest": hashlib.sha256(
            json.dumps([last.get("validator_votes_hash"), state_hashes], sort_keys=True, default=str).encode()
        ).hexdigest(),
    }


def log_tx(label: str, result: dict[str, Any]) -> dict[str, Any]:
    rec = consensus_record(result)
    print(f"  tx   {label:<34} {rec['tx_hash']}")
    print(f"       consensus={rec['result_name']} lifecycle={rec['lifecycle']}")
    votes = rec["votes"] or {}
    for addr, vote in votes.items():
        print(f"       validator {addr} vote={vote}")
    for h in rec["validator_votes_hash"] or []:
        print(f"       votes_hash {h}")
    for h in rec["state_hashes"]["leader"]:
        print(f"       leader_state_hash {h}")
    for v in rec["state_hashes"]["validators"]:
        print(f"       validator_state_hash {v['address']} {v['vote']} {v['contract_state_hash']}")
    print(f"       consensus_digest {rec['consensus_digest']}")
    return rec


def load_progress() -> dict[str, Any]:
    return json.loads(PROGRESS.read_text()) if PROGRESS.exists() else {}


def step(progress: dict[str, Any], key: str, ch: Chain, method: str, args: list[Any], *,
         value: int = 0, simulate: bool = True) -> tuple[dict[str, Any], bool]:
    """Send a write once. A step already recorded in the progress file is re-read from
    the chain by hash instead of being sent again. Returns (result, was_cached)."""
    if key in progress:
        return ch.fetch(progress[key]), True
    result = ch.write(method, args, value=value, label=method, simulate=simulate)
    progress[key] = result["tx_hash"]
    PROGRESS.write_text(json.dumps(progress, indent=2))
    return result, False


def main() -> int:
    dep = json.loads(DEPLOYMENT.read_text())
    address = dep["contract_address"]
    print(f"PatentPrior {address} on Studio Next (chain {dep['chain_id']})\n")

    names = ["inventor1", "inventor2", "inventor3", "inventor4", "challenger1", "challenger2", "challenger3", "funder"]
    actors = {n: Chain(load_or_create_key(n), address) for n in names}
    deployer = Chain(load_or_create_key("deployer"), address)
    for n, ch in actors.items():
        topped = ch.ensure_funded(3 * GEN)
        print(f"actor {n:<12} {ch.address} {'(funded)' if topped else ''} {ch.balance() / GEN:.3f} GEN")
    print()

    progress = load_progress()
    proofs: list[dict[str, Any]] = []
    failures: list[str] = []

    def record(case: dict[str, Any], **kw: Any) -> None:
        proofs.append({"case": case["case"], "name": case["name"], **kw})

    # ---- cases 1-3: register, challenge, evaluate -----------------------------------
    for case in CASES:
        n = case["case"]
        inv, chal = actors[f"inventor{n}"], actors[f"challenger{n}"]
        print(f"== Case {n}: {case['name']}")
        reg, cached = step(progress, f"case{n}.register", inv, "register_patent",
                           [case["title"], case["priority"], case["claim"]], value=case["bounty"])
        if not cached:
            progress[f"case{n}.pid"] = int(inv.view("get_patent_count"))
        pid = int(progress.setdefault(f"case{n}.pid", int(inv.view("get_patent_count"))))
        PROGRESS.write_text(json.dumps(progress, indent=2))
        reg_rec = log_tx("register_patent", reg)
        bond = int(chal.view("required_bond", [pid]))
        sub, cached = step(progress, f"case{n}.submit", chal, "submit_prior_art",
                           [pid, case["url"], case["claimed"]], value=bond)
        if not cached:
            progress[f"case{n}.cid"] = int(chal.view("get_challenge_count"))
        cid = int(progress.setdefault(f"case{n}.cid", int(chal.view("get_challenge_count"))))
        PROGRESS.write_text(json.dumps(progress, indent=2))
        sub_rec = log_tx("submit_prior_art (0.1 GEN bond)", sub)
        follower: dict[str, Any] | None = None
        if n == 1:
            # a second challenger queues behind case 1, citing a different paper (spelled with a
            # version suffix that canonicalisation must fold into the canonical arXiv abs URL)
            c2 = actors["challenger2"]
            fsub, cached = step(progress, "case1.follower.submit", c2, "submit_prior_art",
                                [pid, "https://arxiv.org/abs/1706.03762v7", "2017-06-12"],
                                value=int(c2.view("required_bond", [pid])))
            if not cached:
                progress["case1.follower.cid"] = int(c2.view("get_challenge_count"))
            fcid = int(progress.setdefault("case1.follower.cid", int(c2.view("get_challenge_count"))))
            PROGRESS.write_text(json.dumps(progress, indent=2))
            follower = {"challenge_id": fcid, "submit": log_tx("submit_prior_art (follower, queued behind #1)", fsub)}
            fch = c2.view("get_challenge", [fcid])
            print(f"  follower #{fcid} stored url={fch['prior_art_url']} status={fch['status']} (canonicalised, queued)")
        ev, _ = step(progress, f"case{n}.evaluate", chal, "evaluate_prior_art", [cid], simulate=False)
        ev_rec = log_tx("evaluate_prior_art", ev)

        verdict = chal.view("get_verdict", [cid])
        challenge = chal.view("get_challenge", [cid])
        patent = chal.view("get_patent", [pid])
        outcome = verdict["outcome"]
        ok = outcome == case["expect"]
        print(f"  verdict  outcome={outcome} (expected {case['expect']}) {'OK' if ok else 'MISMATCH'}")
        print(f"           pub_date={verdict['publication_date']!r} confidence={verdict['confidence_score']}")
        print(f"           temporal={verdict['temporal_priority']} anticipation={verdict['full_anticipation']} enabling={verdict['enabling_detail']}")
        print(f"           reasoning={verdict['consensus_reasoning'][:200]!r}")
        print(f"           challenge={challenge['status']} patent={patent['status']}")
        print(f"           credits: challenger={chal.view('claimable_of', [chal.address.lower()])} "
              f"inventor={inv.view('claimable_of', [inv.address.lower()])}\n")
        if not ok:
            failures.append(f"case {n}: got {outcome}, expected {case['expect']}")
        if follower is not None:
            c2 = actors["challenger2"]
            fch = c2.view("get_challenge", [follower["challenge_id"]])
            fverdict = c2.view("get_verdict", [follower["challenge_id"]])
            fcredit = int(c2.view("claimable_of", [c2.address.lower()]))
            fok = (fch["status"] == "VOIDED" and fverdict["outcome"] == "AMBIGUOUS_VOID"
                   and fcredit == int(fch["challenger_bond"]))
            print(f"  FIFO     follower #{follower['challenge_id']} status={fch['status']} refund_credit={fcredit} "
                  f"{'OK' if fok else 'MISMATCH'}")
            if not fok:
                failures.append("FIFO follower was not voided and refunded when case 1 invalidated the patent")
            follower.update(status=fch["status"], verdict=fverdict, refund_credit_wei=str(fcredit), ok=fok,
                            stored_url=fch["prior_art_url"])
        record(
            case, follower=follower, patent_id=pid, challenge_id=cid, expected=case["expect"], outcome=outcome,
            patent_status=patent["status"], challenge_status=challenge["status"], verdict=verdict,
            inventor=inv.address, challenger=chal.address, source_url=case["url"],
            txs={"register_patent": reg_rec, "submit_prior_art": sub_rec, "evaluate_prior_art": ev_rec},
        )

    # ---- case 4: active patent, funded by the community ------------------------------
    c4 = CASE4
    print(f"== Case 4: {c4['name']}")
    inv4, funder = actors["inventor4"], actors["funder"]
    reg, cached = step(progress, "case4.register", inv4, "register_patent",
                       [c4["title"], c4["priority"], c4["claim"]], value=c4["bounty"])
    if not cached:
        progress["case4.pid"] = int(inv4.view("get_patent_count"))
    pid4 = int(progress.setdefault("case4.pid", int(inv4.view("get_patent_count"))))
    PROGRESS.write_text(json.dumps(progress, indent=2))
    reg_rec = log_tx("register_patent", reg)
    fund, _ = step(progress, "case4.fund", funder, "fund_bounty", [pid4], value=c4["extra_funding"])
    fund_rec = log_tx("fund_bounty (community)", fund)
    p4 = inv4.view("get_patent", [pid4])
    ok4 = p4["status"] == "ACTIVE" and p4["active_bounty"] == str(c4["bounty"] + c4["extra_funding"])
    print(f"  patent #{pid4} status={p4['status']} bounty={int(p4['active_bounty']) / GEN} GEN funders={p4['funder_count']} "
          f"{'OK' if ok4 else 'MISMATCH'}\n")
    if not ok4:
        failures.append("case 4: patent not ACTIVE with the expected bounty")
    record(c4, patent_id=pid4, expected="ACTIVE", outcome=p4["status"], patent_status=p4["status"],
           active_bounty_wei=p4["active_bounty"], inventor=inv4.address, funder=funder.address,
           txs={"register_patent": reg_rec, "fund_bounty": fund_rec})

    # ---- case 5: www. canonicalisation probe -------------------------------------------------
    print("== Case 5: www. citation is stored and fetched as the bare host")
    c5 = {
        "case": 5, "name": "www. canonicalisation probe: does the bare host's redirect get followed?",
        "title": "Multiplexed Streams Over UDP With Integrated TLS 1.3 Handshake",
        "priority": "2025-01-01",
        "claim": ("A transport protocol comprising: multiplexing independent ordered byte streams over UDP "
                  "datagrams; integrating a TLS 1.3 handshake into the transport handshake; and supporting "
                  "connection migration across network paths using connection identifiers."),
        "bounty": GEN // 10,
    }
    inv5, chal5 = actors["inventor4"], actors["funder"]
    reg5, cached = step(progress, "case5.register", inv5, "register_patent", [c5["title"], c5["priority"], c5["claim"]], value=c5["bounty"])
    if not cached:
        progress["case5.pid"] = int(inv5.view("get_patent_count"))
    pid5 = int(progress.setdefault("case5.pid", int(inv5.view("get_patent_count"))))
    PROGRESS.write_text(json.dumps(progress, indent=2))
    reg5_rec = log_tx("register_patent", reg5)
    bond5 = int(chal5.view("required_bond", [pid5]))
    sub5, cached = step(progress, "case5.submit", chal5, "submit_prior_art",
                        [pid5, "https://www.rfc-editor.org/rfc/rfc9000", "2021-05-27"], value=bond5)
    if not cached:
        progress["case5.cid"] = int(chal5.view("get_challenge_count"))
    cid5 = int(progress.setdefault("case5.cid", int(chal5.view("get_challenge_count"))))
    PROGRESS.write_text(json.dumps(progress, indent=2))
    sub5_rec = log_tx("submit_prior_art (www. URL)", sub5)
    stored5 = chal5.view("get_challenge", [cid5])["prior_art_url"]
    ev5, _ = step(progress, "case5.evaluate", chal5, "evaluate_prior_art", [cid5], simulate=False)
    ev5_rec = log_tx("evaluate_prior_art", ev5)
    v5 = chal5.view("get_verdict", [cid5])
    followed = v5["outcome"] != "AMBIGUOUS_VOID"
    print(f"  submitted https://www.rfc-editor.org/rfc/rfc9000 -> stored {stored5}")
    print(f"  verdict outcome={v5['outcome']} pub_date={v5['publication_date']!r} confidence={v5['confidence_score']}")
    print(f"  redirect from the bare host {'WAS followed' if followed else 'was NOT followed (or the fetch failed): the challenge voided'}")
    print(f"  reasoning={v5['consensus_reasoning'][:200]!r}\n")
    if stored5 != "https://rfc-editor.org/rfc/rfc9000":
        failures.append(f"www. was not stripped: stored {stored5}")
    record(c5, patent_id=pid5, challenge_id=cid5, expected="not AMBIGUOUS_VOID (redirect followed)", outcome=v5["outcome"],
           stored_url=stored5, redirect_followed=followed, verdict=v5,
           txs={"register_patent": reg5_rec, "submit_prior_art": sub5_rec, "evaluate_prior_art": ev5_rec})

    # ---- rescue_excess: stray funds credited to the contract, swept by the governor --------
    print("== rescue_excess")
    rescue: dict[str, Any] = {}
    stray = GEN * 3 // 10
    before = deployer.view("get_ledger")
    retry(lambda: deployer.client.fund_account(cast(ChecksumAddress, address), stray))  # simulator credit
    time.sleep(3)
    mid = deployer.view("get_ledger")
    excess = int(mid["contract_balance"]) - int(mid["tracked_total"])
    print(f"  stray funds credited: balance {int(before['contract_balance']) / GEN} -> {int(mid['contract_balance']) / GEN} GEN, "
          f"tracked {int(mid['tracked_total']) / GEN} GEN, excess {excess / GEN} GEN")
    if excess == stray:
        rr, _ = step(progress, "rescue_excess", deployer, "rescue_excess", [])
        rrec = log_tx("rescue_excess (governor)", rr)
        msgs = rr["tx"].get("messages") or []
        dest_ok = len(msgs) == 1 and msgs[0].get("recipient", "").lower() == deployer.address.lower() \
            and int(msgs[0].get("value", 0)) == excess
        print(f"  emitted message recipient={msgs[0].get('recipient') if msgs else None} value={msgs[0].get('value') if msgs else None} "
              f"-> governor only: {dest_ok}")
        if not dest_ok:
            failures.append("rescue_excess did not emit exactly one transfer of the excess to the governor")
        end = deployer.view("get_ledger")
        untouched = all(end[k] == mid[k] for k in ("active_bounties", "locked_bonds", "claimable_credits",
                                                   "protocol_vault", "tracked_total", "total_deposited", "total_withdrawn"))
        print(f"  tracked liabilities untouched: {untouched}; balance now {int(end['contract_balance']) / GEN} GEN")
        rescue = {"stray_wei": str(stray), "excess_wei": str(excess), "tracked_untouched": untouched, "message_recipient_is_governor": dest_ok,
                  "messages": msgs,
                  "balance_after_wei": end["contract_balance"], "tx": rrec}
        if not untouched:
            failures.append("rescue_excess changed tracked liabilities")
    else:
        failures.append(f"expected excess {stray} after the simulator credit, saw {excess}")

    # ---- settlement: pull withdrawals + governor sweep --------------------------------
    print("== Settlement (pull pattern)")
    settle: dict[str, Any] = {}
    withdrawers = [("challenger1", "case 1 bond + whole bounty"), ("inventor2", "case 2 defender share (50% of slashed bond)"),
                   ("challenger3", "case 3 full bond refund")]
    for name, why in withdrawers:
        ch = actors[name]
        owed = int(ch.view("claimable_of", [ch.address.lower()]))
        if owed == 0:
            print(f"  {name}: nothing owed ({why})")
            failures.append(f"{name} had no credit ({why})")
            continue
        w, _ = step(progress, f"withdraw.{name}", ch, "pull_withdraw", [])
        rec = log_tx(f"pull_withdraw {name} ({owed / GEN} GEN)", w)
        settle[name] = {"amount_wei": str(owed), "why": why, "tx": rec}

    vault = int(deployer.view("get_ledger")["protocol_vault"])
    if vault > 0:
        sw, _ = step(progress, "sweep_vault", deployer, "sweep_vault", [deployer.address.lower(), vault])
        settle["sweep_vault"] = {"amount_wei": str(vault), "tx": log_tx("sweep_vault (governor)", sw)}
        w, _ = step(progress, "withdraw.governor", deployer, "pull_withdraw", [])
        settle["governor_withdraw"] = {"amount_wei": str(vault), "tx": log_tx("pull_withdraw governor", w)}

    # ---- ledger: identity now, balance equality once emitted transfers finalize ---------
    print("\n== Ledger")
    ledger: dict[str, Any] = {}
    for _ in range(40):
        ledger = deployer.view("get_ledger")
        if ledger["balance_equals_liabilities"]:
            break
        time.sleep(15)
    print(json.dumps(ledger, indent=2))
    if not ledger["ledger_identity_holds"]:
        failures.append("ledger identity broken")
    if not ledger["balance_equals_liabilities"]:
        failures.append(
            "contract balance did not converge to tracked liabilities: Studio Next marked the emitted "
            "payout transfers skipped and refunded them to the contract (see README, Known limitations)"
        )

    dep["live_proofs"] = {
        "recorded_at": now(),
        "cases": proofs,
        "settlement": settle,
        "rescue_excess": rescue,
        "final_ledger": ledger,
        "failures": failures,
        "actors": {n: ch.address for n, ch in actors.items()},
    }
    DEPLOYMENT.write_text(json.dumps(dep, indent=2, default=str) + "\n")
    print(f"\nrecorded -> {DEPLOYMENT.relative_to(ROOT)}")
    if failures:
        print("FAILURES:", *failures, sep="\n  ")
        return 1
    print("all live cases matched expectations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
