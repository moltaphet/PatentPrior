"""Shared helpers for PatentPrior direct-mode tests."""

import json

import pytest

_COUNT = {"assert": 0, "revert": 0}


def pytest_assertion_pass(item, lineno, orig, expl):  # noqa: ARG001
    _COUNT["assert"] += 1


@pytest.fixture(autouse=True)
def _count_reverts(monkeypatch):
    """Every expect_revert block that sees its revert is an assertion too."""
    from gltest.direct.vm import VMContext

    original = VMContext.expect_revert

    def counted(self, message=None):
        cm = original(self, message)

        class _Wrap:
            def __enter__(_s):
                return cm.__enter__()

            def __exit__(_s, *a):
                r = cm.__exit__(*a)
                if a[0] is None or r:
                    _COUNT["revert"] += 1
                return r

        return _Wrap()

    monkeypatch.setattr(VMContext, "expect_revert", counted)


def pytest_terminal_summary(terminalreporter):
    a, r = _COUNT["assert"], _COUNT["revert"]
    terminalreporter.write_line(f"ASSERTIONS: {a} assert statements + {r} verified reverts = {a + r}")

CONTRACT = "contracts/patent_prior.py"
GEN = 10**18
BOND = GEN // 10
MIN_CONTRIB = GEN // 1000
DAY = 86400

PRIORITY = "2020-06-15"
CLAIM = (
    "A method for coordinating autonomous agents comprising: receiving a signed intent, "
    "escrowing collateral in a smart contract, and releasing the collateral upon "
    "multi-validator consensus."
)
TITLE = "Consensus-Gated Collateral Release"
ARXIV = "https://arxiv.org/abs/1801.00001"


def fund(vm, who, amount=1000 * GEN):
    vm.deal(who, amount)


def register(c, vm, who, bounty=0, title=TITLE, priority=PRIORITY, claim=CLAIM):
    fund(vm, who)
    vm.sender = who
    vm.value = bounty
    pid = c.register_patent(title, priority, claim)
    vm.value = 0
    return pid


def submit(c, vm, who, pid, url=ARXIV, claimed="2018-01-02", value=BOND):
    fund(vm, who)
    vm.sender = who
    vm.value = value
    cid = c.submit_prior_art(pid, url, claimed)
    vm.value = 0
    return cid


def source_page(pub_date: str, body: str = "Full disclosure of the claimed method.") -> dict:
    html = f"<html><body><h1>Whitepaper</h1><p>Submitted on {pub_date}</p><p>{body}</p></body></html>"
    return {"status": 200, "body": html}


def mock_source(vm, pattern: str, pub_date: str, body: str = "Full disclosure of the claimed method."):
    vm.mock_web(pattern, source_page(pub_date, body))


def tribunal(
    pub="2018-01-02",
    anticipation=True,
    enabling=True,
    confidence=90,
    conflicting=False,
    reasoning="Source discloses all claim elements.",
    cites=("signed intent", "escrow collateral"),
):
    return {
        "publication_date": pub,
        "source_conflicting": conflicting,
        "full_anticipation": anticipation,
        "enabling_detail": enabling,
        "citations_matched": list(cites),
        "confidence": confidence,
        "reasoning": reasoning,
    }


def mock_tribunal(vm, pattern: str = r".*", **kw):
    # Double-encoded: the harness json.loads()s the mock once and the SDK's
    # exec_prompt(response_format="json") decodes the resulting string again.
    vm.mock_llm(pattern, json.dumps(json.dumps(tribunal(**kw))))


def ledger(c) -> dict:
    return c.get_ledger()


def assert_solvent(c):
    L = c.get_ledger()
    assert L["ledger_identity_holds"], L
    return L


from datetime import datetime, timezone  # noqa: E402


def hx(addr) -> str:
    """Canonical lowercase hex the contract uses as a storage key."""
    return "0x" + bytes(addr).hex()


def warp_to(vm, unix_ts: int) -> None:
    vm.warp(datetime.fromtimestamp(unix_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def make_account(n: int) -> bytes:
    """Deterministic distinct 20-byte test account."""
    return bytes([n]) * 20


def run_challenge(c, vm, challenger, pid, url, pub, evaluator=None, **kw):
    """Submit and evaluate one challenge against a fresh pair of mocks. Returns
    (challenge_id, outcome)."""
    esc = url.replace(".", r"\.")
    vm.clear_mocks()  # first registered match wins, so never let a stale mock shadow this one
    mock_source(vm, esc, pub)
    mock_tribunal(vm, esc, pub=pub, **kw)
    cid = submit(c, vm, challenger, pid, url=url)
    vm.sender = evaluator if evaluator is not None else challenger
    return cid, c.evaluate_prior_art(cid)


def credit(c, who) -> int:
    return int(c.claimable_of(hx(who)))
