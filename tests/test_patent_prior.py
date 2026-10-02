"""Regression tests for the three audited vulnerabilities (host spoofing / bond sizing,
URL front-running / FIFO, stranded balance) and the dust-backer fix."""

import pytest

from conftest import *  # noqa: F401,F403

WEEK = 7 * DAY


# ------------------------------------------------------------------ Issue 1: whitelist
@pytest.mark.parametrize(
    "url",
    [
        "https://web.archive.org/web/20180101000000/https://arxiv.org/abs/1801.00001",
        "https://web.archive.org/web/2018/https://example.com/",
        "https://openreview.net/forum?id=abc123",
        "https://hal.science/hal-01234567",
        "https://www.semanticscholar.org/paper/abc",
    ],
)
def test_web_archive_rejected_from_whitelist(direct_vm, direct_deploy, direct_alice, direct_bob, url):
    """Hosts where anyone can publish or replace content cannot be cited, at the URL check
    and at submission, and no bond is locked by the attempt."""
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    with direct_vm.expect_revert("ERR_UNSAFE_URL"):
        c.check_url(url)
    fund(direct_vm, direct_bob)
    direct_vm.sender, direct_vm.value = direct_bob, BOND
    with direct_vm.expect_revert("ERR_UNSAFE_URL"):
        c.submit_prior_art(pid, url, "2018-01-02")
    assert c.get_challenge_count() == 0
    assert assert_solvent(c)["locked_bonds"] == "0"
    domains = c.get_constants()["allowed_domains"]
    for purged in ("web.archive.org", "openreview.net", "hal.science", "semanticscholar.org"):
        assert purged not in domains
    assert "arxiv.org" in domains and "patents.google.com" in domains


@pytest.mark.parametrize(
    "bounty,expected",
    [
        (0, BOND),
        (GEN, BOND),
        (5 * GEN, BOND),                 # 2% == 0.1 GEN exactly
        (5 * GEN + 50 * GEN // 100 // 1, 2 * (5 * GEN + 50 * GEN // 100) // 100),
        (10 * GEN, 2 * GEN // 10),       # 0.2 GEN
        (100 * GEN, 2 * GEN),            # 2 GEN
        (123 * GEN + 7, 2 * (123 * GEN + 7) // 100),
    ],
)
def test_proportional_bond_scaling_with_large_bounty(direct_vm, direct_deploy, direct_alice, direct_bob, bounty, expected):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=bounty) if bounty == 0 or bounty >= MIN_CONTRIB else None
    assert expected == max(BOND, bounty * 2 // 100)
    assert c.required_bond(pid) == str(expected)
    assert c.get_patent(pid)["required_bond"] == str(expected)
    fund(direct_vm, direct_bob, 5000 * GEN)
    direct_vm.sender = direct_bob
    if expected > BOND:
        for short in (BOND, expected - 1):
            direct_vm.value = short
            with direct_vm.expect_revert("ERR_BOND_BELOW_MINIMUM"):
                c.submit_prior_art(pid, ARXIV, "2018-01-02")
    direct_vm.value = expected
    cid = c.submit_prior_art(pid, ARXIV, "2018-01-02")
    assert c.get_challenge(cid)["challenger_bond"] == str(expected)
    assert assert_solvent(c)["locked_bonds"] == str(expected)


def test_scaled_bond_is_slashed_and_refunded_in_full(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    """A 3 GEN-class pool needs a bigger bond, and every settlement path handles that
    amount, not the 0.1 GEN base."""
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=20 * GEN)  # bond = 0.4 GEN
    bond = 4 * GEN // 10
    assert required_bond(c, pid) == bond
    cid, out = run_challenge(c, direct_vm, direct_bob, pid, "https://arxiv.org/abs/2201.00001", "2022-01-01")
    assert out == "VALID"
    assert credit(c, direct_alice) == bond // 2
    assert int(assert_solvent(c)["protocol_vault"]) == bond - bond // 2
    cid2, out2 = run_challenge(c, direct_vm, direct_charlie, pid, "https://arxiv.org/abs/2201.00002", "2018-01-02")
    assert out2 == "INVALIDATED"
    assert credit(c, direct_charlie) == bond + 20 * GEN
    assert_solvent(c)


def test_bond_requirement_tracks_the_pool_as_it_grows(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    assert required_bond(c, pid) == BOND
    fund(direct_vm, direct_bob, 5000 * GEN)
    direct_vm.sender, direct_vm.value = direct_bob, 49 * GEN
    c.fund_bounty(pid)  # 50 GEN -> 1 GEN bond
    assert required_bond(c, pid) == GEN


# ------------------------------------------------------------ Issue 2: canonicalisation
FRONT_RUN_VARIANTS = [
    "https://arxiv.org/abs/1801.00001v1",
    "https://arxiv.org/abs/1801.00001v2",
    "https://arxiv.org/abs/1801.00001v17",
    "https://arxiv.org/abs/1801.00001/",
    "https://arxiv.org/abs/1801.00001v2/",
    "https://arxiv.org/abs/1801.00001?context=cs.CR",
    "https://arxiv.org/abs/1801.00001#abstract",
    "https://arxiv.org/abs/1801.00001v3?x=1#y",
    "HTTPS://ARXIV.ORG/abs/1801.00001",
    "https://arxiv.org:443/abs/1801.00001",
    "https://export.arxiv.org/abs/1801.00001",
    "https://www.arxiv.org/abs/1801.00001",
    "https://arxiv.org/pdf/1801.00001",
    "https://arxiv.org/pdf/1801.00001v2",
    "https://arxiv.org/pdf/1801.00001v2.pdf",
    "https://arxiv.org/html/1801.00001v1",
    "https://arxiv.org//abs//1801.00001",
    "https://arxiv.org/abs/1801%2E00001",
    "https://arxiv.org/%61bs/1801.00001",
]


def test_arxiv_version_canonicalization_blocks_frontrunning(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    c = direct_deploy(CONTRACT)
    canonical = "https://arxiv.org/abs/1801.00001"
    for v in FRONT_RUN_VARIANTS:
        assert c.check_url(v) == canonical, v
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    cid = submit(c, direct_vm, direct_bob, pid, url="https://arxiv.org/abs/1801.00001v2")
    assert c.get_challenge(cid)["prior_art_url"] == canonical  # stored (and fetched) canonically
    fund(direct_vm, direct_charlie)
    for v in FRONT_RUN_VARIANTS:  # a front-runner cannot re-cite the same paper under another spelling
        direct_vm.sender, direct_vm.value = direct_charlie, BOND
        with direct_vm.expect_revert("ERR_DUPLICATE_CHALLENGE"):
            c.submit_prior_art(pid, v, "2018-01-02")
    assert c.get_challenge_count() == 1
    # a different paper is, of course, a different citation; so is the same paper on another patent
    other = register(c, direct_vm, direct_alice)
    assert submit(c, direct_vm, direct_charlie, other, url="https://arxiv.org/abs/1801.00001v9") == 2
    assert submit(c, direct_vm, direct_charlie, pid, url="https://arxiv.org/abs/1801.00002v1") == 3


@pytest.mark.parametrize(
    "raw,canonical",
    [
        ("https://arxiv.org/abs/hep-th/9901001v2", "https://arxiv.org/abs/hep-th/9901001"),
        ("https://arxiv.org/abs/math.GT/0309136v1", "https://arxiv.org/abs/math.GT/0309136"),
        ("https://arxiv.org/abs/2101.12345v10", "https://arxiv.org/abs/2101.12345"),
        ("https://arxiv.org/abs/1801.0001v2", "https://arxiv.org/abs/1801.0001"),
        ("https://patents.google.com/patent/US1234567A/en?oq=x#top", "https://patents.google.com/patent/US1234567A/en"),
        ("https://DOI.ORG/10.1000/XYZ/", "https://doi.org/10.1000/XYZ"),
        ("https://doi.org/10.1000/a%41b", "https://doi.org/10.1000/aAb"),
        ("https://doi.org/10.1000/a%2fb", "https://doi.org/10.1000/a%2Fb"),
        ("https://www.rfc-editor.org/rfc/rfc9000///", "https://www.rfc-editor.org/rfc/rfc9000"),
        ("https://arxiv.org/abs/notanid", "https://arxiv.org/abs/notanid"),  # unknown shapes are left alone
    ],
)
def test_canonical_forms(direct_deploy, raw, canonical):
    c = direct_deploy(CONTRACT)
    assert c.check_url(raw) == canonical


def test_canonicalization_rejects_dot_segments(direct_vm, direct_deploy):
    c = direct_deploy(CONTRACT)
    for bad in ("https://arxiv.org/abs/../abs/1801.00001", "https://arxiv.org/./abs/1801.00001",
                "https://arxiv.org/abs/1801.00001/..", "https://arxiv.org/%2e%2e/abs/1801.00001"):
        with direct_vm.expect_revert("ERR_UNSAFE_URL"):
            c.check_url(bad)


# ------------------------------------------------------------------------ FIFO
def test_fifo_adjudication_refunds_subsequent_challenges_on_invalidation(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=2 * GEN)
    challengers = [make_account(10 + i) for i in range(4)]
    urls = [f"https://arxiv.org/abs/1801.0010{i}" for i in range(4)]
    cids = [submit(c, direct_vm, who, pid, url=u) for who, u in zip(challengers, urls)]
    assert cids == [1, 2, 3, 4]
    assert c.next_evaluable(pid) == 1
    assert c.get_patent(pid)["pending_challenges"] == 4

    # nobody can jump the queue
    direct_vm.clear_mocks()
    mock_source(direct_vm, r"arxiv\.org", "2018-01-02")
    mock_tribunal(direct_vm)
    for late in (2, 3, 4):
        direct_vm.sender = challengers[late - 1]
        with direct_vm.expect_revert("ERR_FIFO_ORDER"):
            c.evaluate_prior_art(late)
    assert c.get_challenge(2)["status"] == "PENDING"

    direct_vm.sender = challengers[0]
    assert c.evaluate_prior_art(1) == "INVALIDATED"
    assert c.get_patent(pid)["status"] == "INVALIDATED"
    assert credit(c, challengers[0]) == BOND + 2 * GEN
    for i in (1, 2, 3):
        ch = c.get_challenge(i + 1)
        assert ch["status"] == "VOIDED"
        assert c.get_verdict(i + 1)["outcome"] == "AMBIGUOUS_VOID"
        assert credit(c, challengers[i]) == BOND  # 100% refund, no slash
    assert credit(c, direct_alice) == 0
    assert c.next_evaluable(pid) == 0
    L = assert_solvent(c)
    assert L["locked_bonds"] == "0"
    assert L["protocol_vault"] == "0"
    assert L["claimable_credits"] == str(BOND * 4 + 2 * GEN)


def test_fifo_head_rejection_hands_the_queue_to_the_next(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    mock_source(direct_vm, r"arxiv\.org", "2022-02-02")
    mock_tribunal(direct_vm, pub="2022-02-02")
    first = submit(c, direct_vm, direct_bob, pid, url="https://arxiv.org/abs/2201.00001")
    second = submit(c, direct_vm, direct_charlie, pid, url="https://arxiv.org/abs/2201.00002")
    direct_vm.sender = direct_bob
    assert c.evaluate_prior_art(first) == "VALID"
    assert c.next_evaluable(pid) == second
    assert c.get_challenge(second)["status"] == "PENDING"  # a rejection voids nobody
    direct_vm.sender = direct_charlie
    assert c.evaluate_prior_art(second) == "VALID"
    assert credit(c, direct_alice) == BOND  # two half bonds
    assert c.next_evaluable(pid) == 0


def test_fifo_is_per_patent(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    c = direct_deploy(CONTRACT)
    p1 = register(c, direct_vm, direct_alice)
    p2 = register(c, direct_vm, direct_alice)
    a = submit(c, direct_vm, direct_bob, p1, url="https://arxiv.org/abs/2201.00001")
    b = submit(c, direct_vm, direct_charlie, p2, url="https://arxiv.org/abs/2201.00002")
    mock_source(direct_vm, r"arxiv\.org", "2022-02-02")
    mock_tribunal(direct_vm, pub="2022-02-02")
    direct_vm.sender = direct_charlie
    assert c.evaluate_prior_art(b) == "VALID"  # p2's head, even though p1's challenge is older
    assert c.get_challenge(a)["status"] == "PENDING"


def test_stale_void_of_the_head_unblocks_the_queue(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice)
    first = submit(c, direct_vm, direct_bob, pid, url="https://arxiv.org/abs/2201.00001")
    second = submit(c, direct_vm, direct_charlie, pid, url="https://arxiv.org/abs/2201.00002")
    direct_vm.clear_mocks()
    mock_source(direct_vm, r"arxiv\.org", "2022-02-02")
    mock_tribunal(direct_vm, pub="2022-02-02")
    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("ERR_FIFO_ORDER"):
        c.evaluate_prior_art(second)
    warp_to(direct_vm, c.get_challenge(first)["created_at"] + WEEK)
    c.void_stale_challenge(first)
    assert c.next_evaluable(pid) == second
    assert c.evaluate_prior_art(second) == "VALID"


def test_queue_capacity_is_bounded(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice)
    for i in range(16):
        submit(c, direct_vm, make_account(100 + i), pid, url=f"https://arxiv.org/abs/2201.{i:05d}")
    fund(direct_vm, make_account(200))
    direct_vm.sender, direct_vm.value = make_account(200), BOND
    with direct_vm.expect_revert("ERR_CHALLENGE_QUEUE_FULL"):
        c.submit_prior_art(pid, "https://arxiv.org/abs/2201.99999", "2018-01-02")
    assert c.get_patent(pid)["pending_challenges"] == 16


# --------------------------------------------------------------------- Issue 3: rescue
def seed_excess(direct_vm, c, amount):
    """Direct mode does not credit msg.value to the contract balance, so give it a
    balance equal to its tracked liabilities plus `amount` of stray funds."""
    tracked = int(c.get_ledger()["tracked_total"])
    direct_vm.deal(c.address, tracked + amount)


def test_rescue_excess_preserves_accounting_invariants(direct_vm, direct_deploy, direct_alice, direct_bob, direct_owner):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=3 * GEN)
    run_challenge(c, direct_vm, direct_bob, pid, "https://arxiv.org/abs/2201.00001", "2022-01-01")  # slash -> credit + vault
    submit(c, direct_vm, direct_bob, pid, url="https://arxiv.org/abs/2201.00002")                  # a live bond
    before = c.get_ledger()
    tracked = int(before["tracked_total"])
    assert tracked == 3 * GEN + BOND + BOND // 2 + BOND // 2  # bounty + locked + credit + vault
    seed_excess(direct_vm, c, 7 * GEN // 10)
    assert c.get_ledger()["contract_balance"] == str(tracked + 7 * GEN // 10)
    sink = make_account(0xBB)
    direct_vm.sender = direct_owner
    assert c.rescue_excess(hx(sink)) == str(7 * GEN // 10)
    after = c.get_ledger()
    for k in ("active_bounties", "locked_bonds", "claimable_credits", "protocol_vault", "tracked_total",
              "total_deposited", "total_withdrawn"):
        assert after[k] == before[k], k  # not one wei of tracked liability moved
    assert after["ledger_identity_holds"] is True
    # whatever the chain does with the transfer, a second call can never take more than the
    # excess that remains: with no new surplus it is refused
    direct_vm.deal(c.address, tracked)
    with direct_vm.expect_revert("ERR_NO_EXCESS_BALANCE"):
        c.rescue_excess(hx(sink))
    assert_solvent(c)


def test_rescue_excess_guards(direct_vm, direct_deploy, direct_alice, direct_bob, direct_owner):
    c = direct_deploy(CONTRACT)
    register(c, direct_vm, direct_alice, bounty=GEN)
    sink = make_account(0xBB)
    for who in (direct_alice, direct_bob):
        direct_vm.sender = who
        with direct_vm.expect_revert("ERR_UNAUTHORIZED"):
            c.rescue_excess(hx(sink))
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("ERR_NO_EXCESS_BALANCE"):  # balance 0 < tracked
        c.rescue_excess(hx(sink))
    seed_excess(direct_vm, c, 0)
    with direct_vm.expect_revert("ERR_NO_EXCESS_BALANCE"):  # balance == tracked exactly
        c.rescue_excess(hx(sink))
    seed_excess(direct_vm, c, 1)
    with direct_vm.expect_revert("ERR_INVALID_INPUT"):
        c.rescue_excess("0x" + "0" * 40)
    assert c.rescue_excess(hx(sink)) == "1"  # one wei of surplus is enough


def test_rescue_waits_out_in_flight_payouts(direct_vm, direct_deploy, direct_alice, direct_bob, direct_owner):
    """A payout that has been emitted but not yet settled makes balance exceed tracked
    liabilities. The governor must not be able to sweep that as 'excess'."""
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    run_challenge(c, direct_vm, direct_bob, pid, ARXIV, "2018-01-02")
    seed_excess(direct_vm, c, 0)  # balance == tracked (1.1 GEN credited to bob)
    direct_vm.sender = direct_bob
    c.pull_withdraw()
    sink = make_account(0xBB)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("ERR_PAYOUTS_IN_FLIGHT"):
        c.rescue_excess(hx(sink))
    last = int(c.get_constants()["rescue_grace_seconds"])
    warp_to(direct_vm, __import__("time").time() + last - 60)
    with direct_vm.expect_revert("ERR_PAYOUTS_IN_FLIGHT"):
        c.rescue_excess(hx(sink))
    warp_to(direct_vm, __import__("time").time() + last + 60)
    assert int(c.rescue_excess(hx(sink))) == BOND + GEN


def test_rescue_cannot_touch_vault_credits_or_bonds(direct_vm, direct_deploy, direct_alice, direct_bob, direct_owner):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    run_challenge(c, direct_vm, direct_bob, pid, "https://arxiv.org/abs/2201.00001", "2022-01-01")
    L = c.get_ledger()
    direct_vm.deal(c.address, int(L["tracked_total"]) - 1)  # one wei short of solvent
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("ERR_NO_EXCESS_BALANCE"):
        c.rescue_excess(hx(make_account(0xBB)))
    assert c.get_ledger()["protocol_vault"] == L["protocol_vault"]
    assert c.get_ledger()["claimable_credits"] == L["claimable_credits"]


# -------------------------------------------------------------------- dust backers
def test_dust_backer_rejected(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice)
    fund(direct_vm, direct_bob)
    direct_vm.sender = direct_bob
    for dust in (1, 1000, GEN // 1000, GEN // 100, GEN // 20 - 1):
        direct_vm.value = dust
        with direct_vm.expect_revert("ERR_INVALID_VALUE"):
            c.fund_bounty(pid)
    assert c.get_patent(pid)["funder_count"] == 0
    assert c.get_patent(pid)["active_bounty"] == "0"
    direct_vm.value = GEN // 20  # exactly 0.05 GEN is accepted
    c.fund_bounty(pid)
    assert c.get_patent(pid)["funder_count"] == 1
    assert c.get_patent(pid)["active_bounty"] == str(GEN // 20)
    assert c.get_constants()["min_bounty_contribution"] == str(GEN // 20)
    assert_solvent(c)


def test_initial_bounty_obeys_the_same_floor(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    fund(direct_vm, direct_alice)
    direct_vm.sender = direct_alice
    direct_vm.value = GEN // 20 - 1
    with direct_vm.expect_revert("ERR_INVALID_VALUE"):
        c.register_patent(TITLE, PRIORITY, CLAIM)
    direct_vm.value = GEN // 20
    assert c.register_patent(TITLE, PRIORITY, CLAIM) == 1


def test_dust_cannot_exhaust_the_backer_slots(direct_vm, direct_deploy, direct_alice):
    """Thirty-two dust attempts each fail; a legitimate backer can still join."""
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice)
    for i in range(32):
        who = make_account(60 + i)
        fund(direct_vm, who)
        direct_vm.sender, direct_vm.value = who, 1
        with direct_vm.expect_revert("ERR_INVALID_VALUE"):
            c.fund_bounty(pid)
    legit = make_account(150)
    fund(direct_vm, legit)
    direct_vm.sender, direct_vm.value = legit, GEN // 20
    c.fund_bounty(pid)
    assert c.get_patent(pid)["funder_count"] == 1
