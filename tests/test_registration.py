"""register_patent, fund_bounty, expire_patent and submit_prior_art validation."""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import *  # noqa: F401,F403


def test_register_stores_dossier(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    p = c.get_patent(pid)
    assert pid == 1
    assert p["patent_id"] == 1
    assert p["patent_title"] == TITLE
    assert p["priority_date"] == PRIORITY
    assert p["claim_text"] == CLAIM
    assert p["inventor_address"] == hx(direct_alice)
    assert p["active_bounty"] == str(GEN)
    assert p["status"] == "ACTIVE"
    assert p["pending_challenges"] == 0
    assert p["funder_count"] == 1
    assert c.get_patent_count() == 1
    L = assert_solvent(c)
    assert L["active_bounties"] == str(GEN)
    assert L["total_deposited"] == str(GEN)


def test_register_without_bounty(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice)
    p = c.get_patent(pid)
    assert p["active_bounty"] == "0"
    assert p["funder_count"] == 0
    assert assert_solvent(c)["tracked_total"] == "0"


def test_ids_increment_and_titles_are_trimmed(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy(CONTRACT)
    a = register(c, direct_vm, direct_alice, title="  Spaced Title  ")
    b = register(c, direct_vm, direct_bob)
    assert (a, b) == (1, 2)
    assert c.get_patent(1)["patent_title"] == "Spaced Title"
    assert c.get_patent(2)["inventor_address"] == hx(direct_bob)
    assert c.get_patent_count() == 2


TITLE_BAD = ["", "ab", "  a ", "x" * 201]
CLAIM_BAD = ["", "short claim", " " * 40, "y" * 4001]
DATE_BAD = [
    "", "2020-13-01", "2020-02-30", "2021-02-29", "20200615", "2020-6-15", "2020/06/15",
    "June 15 2020", "2020-06-15T25:00:00Z", "2020-06-15T10:61:00Z", "2020-06-15T10:00:61Z",
    "2020-06-15T10:00:00", "2020-06-15 10:00:00", "2020-06-15T10:00:00+01:00", "1789-12-31",
    "0000-01-01", "2020-00-10", "2020-01-00", "2020-06-15Z", " 2020-06-15", "2020-06-15 ",
]
DATE_OK = [
    "2020-06-15", "2020-06-15T10:30:00Z", "2020-06-15T10:30:00+00:00", "1790-01-01",
    "2020-02-29", "1999-12-31T23:59:59Z", "2001-09-09",
]


def test_register_rejects_bad_titles(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    for t in TITLE_BAD:
        with direct_vm.expect_revert("ERR_INVALID_INPUT"):
            c.register_patent(t, PRIORITY, CLAIM)
    assert c.get_patent_count() == 0


def test_register_rejects_bad_claims(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    for cl in CLAIM_BAD:
        with direct_vm.expect_revert("ERR_INVALID_INPUT"):
            c.register_patent(TITLE, PRIORITY, cl)
    assert c.get_patent_count() == 0


def test_register_rejects_bad_dates(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    for d in DATE_BAD:
        with direct_vm.expect_revert("ERR_INVALID_DATE"):
            c.register_patent(TITLE, d, CLAIM)
    assert c.get_patent_count() == 0


def test_register_accepts_good_dates(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    for i, d in enumerate(DATE_OK, start=1):
        assert c.register_patent(TITLE, d, CLAIM) == i
        assert c.get_patent(i)["priority_date"] == d


def test_priority_date_cannot_be_in_the_future(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    today = datetime.now(timezone.utc).date()
    with direct_vm.expect_revert("ERR_INVALID_DATE"):
        c.register_patent(TITLE, (today + timedelta(days=2)).isoformat(), CLAIM)
    with direct_vm.expect_revert("ERR_INVALID_DATE"):
        c.register_patent(TITLE, "2999-01-01", CLAIM)
    assert c.register_patent(TITLE, (today - timedelta(days=1)).isoformat(), CLAIM) == 1


def test_initial_bounty_floor(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    fund(direct_vm, direct_alice)
    direct_vm.sender = direct_alice
    for v in (1, MIN_CONTRIB - 1):
        direct_vm.value = v
        with direct_vm.expect_revert("ERR_INVALID_VALUE"):
            c.register_patent(TITLE, PRIORITY, CLAIM)
    direct_vm.value = MIN_CONTRIB
    assert c.register_patent(TITLE, PRIORITY, CLAIM) == 1
    assert c.get_patent(1)["active_bounty"] == str(MIN_CONTRIB)
    assert assert_solvent(c)["tracked_total"] == str(MIN_CONTRIB)


def test_unknown_ids_revert(direct_vm, direct_deploy):
    c = direct_deploy(CONTRACT)
    for call in (c.get_patent, c.get_challenge, c.get_verdict):
        for bad in (0, 1, 99):
            with direct_vm.expect_revert("ERR_UNKNOWN_ID"):
                call(bad)


# ------------------------------------------------------------------ funding
def test_fund_bounty_accumulates_per_funder(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    for who, amt in ((direct_bob, 2 * GEN), (direct_charlie, GEN // 2), (direct_bob, GEN)):
        fund(direct_vm, who)
        direct_vm.sender = who
        direct_vm.value = amt
        c.fund_bounty(pid)
        direct_vm.value = 0
    p = c.get_patent(pid)
    assert p["active_bounty"] == str(GEN + 2 * GEN + GEN // 2 + GEN)
    assert p["funder_count"] == 3  # bob topping up does not add a slot
    assert c.contribution_of(pid, hx(direct_bob)) == str(3 * GEN)
    assert c.contribution_of(pid, hx(direct_charlie)) == str(GEN // 2)
    assert c.contribution_of(pid, hx(direct_alice)) == str(GEN)
    assert assert_solvent(c)["active_bounties"] == p["active_bounty"]


def test_fund_bounty_validation(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice)
    fund(direct_vm, direct_bob)
    direct_vm.sender = direct_bob
    for v in (0, 1, MIN_CONTRIB - 1):
        direct_vm.value = v
        with direct_vm.expect_revert("ERR_INVALID_VALUE"):
            c.fund_bounty(pid)
    direct_vm.value = MIN_CONTRIB
    with direct_vm.expect_revert("ERR_UNKNOWN_ID"):
        c.fund_bounty(77)
    c.fund_bounty(pid)
    assert c.get_patent(pid)["active_bounty"] == str(MIN_CONTRIB)


def test_funder_cap_blocks_new_funders_but_not_topups(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice)
    for i in range(1, 33):
        who = make_account(i)
        fund(direct_vm, who)
        direct_vm.sender = who
        direct_vm.value = MIN_CONTRIB
        c.fund_bounty(pid)
    assert c.get_patent(pid)["funder_count"] == 32
    late = make_account(99)
    fund(direct_vm, late)
    direct_vm.sender = late
    direct_vm.value = MIN_CONTRIB
    with direct_vm.expect_revert("ERR_FUNDER_LIMIT"):
        c.fund_bounty(pid)
    first = make_account(1)
    direct_vm.sender = first
    c.fund_bounty(pid)  # existing funder may still top up
    assert c.contribution_of(pid, hx(first)) == str(2 * MIN_CONTRIB)
    assert c.get_patent(pid)["active_bounty"] == str(33 * MIN_CONTRIB)
    assert assert_solvent(c)["active_bounties"] == str(33 * MIN_CONTRIB)


# ------------------------------------------------------------------- expiry
def test_expire_refunds_every_funder(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    amounts = {hx(direct_bob): 3 * GEN, hx(direct_charlie): GEN // 4}
    for who, amt in ((direct_bob, 3 * GEN), (direct_charlie, GEN // 4)):
        fund(direct_vm, who)
        direct_vm.sender = who
        direct_vm.value = amt
        c.fund_bounty(pid)
    direct_vm.value = 0
    direct_vm.sender = direct_alice
    c.expire_patent(pid)
    p = c.get_patent(pid)
    assert p["status"] == "EXPIRED"
    assert p["active_bounty"] == "0"
    assert credit(c, direct_alice) == GEN
    assert credit(c, direct_bob) == amounts[hx(direct_bob)]
    assert credit(c, direct_charlie) == amounts[hx(direct_charlie)]
    L = assert_solvent(c)
    assert L["active_bounties"] == "0"
    assert L["claimable_credits"] == str(GEN + 3 * GEN + GEN // 4)
    # an expired claim takes no more money and no more challenges
    direct_vm.sender = direct_bob
    direct_vm.value = MIN_CONTRIB
    with direct_vm.expect_revert("ERR_INVALID_STATE"):
        c.fund_bounty(pid)
    direct_vm.value = BOND
    with direct_vm.expect_revert("ERR_INVALID_STATE"):
        c.submit_prior_art(pid, ARXIV, "2018-01-02")


def test_expire_access_and_state_gates(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("ERR_UNAUTHORIZED"):
        c.expire_patent(pid)
    submit(c, direct_vm, direct_bob, pid)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("ERR_INVALID_STATE"):  # open challenge pins the claim
        c.expire_patent(pid)
    with direct_vm.expect_revert("ERR_UNKNOWN_ID"):
        c.expire_patent(42)
    assert c.get_patent(pid)["status"] == "ACTIVE"
    assert c.get_patent(pid)["active_bounty"] == str(GEN)


def test_expire_twice_reverts(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    direct_vm.sender = direct_alice
    c.expire_patent(pid)
    with direct_vm.expect_revert("ERR_INVALID_STATE"):
        c.expire_patent(pid)
    assert credit(c, direct_alice) == GEN  # refunded exactly once


# --------------------------------------------------------------- submission
def test_submit_requires_exact_bond(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    fund(direct_vm, direct_bob)
    direct_vm.sender = direct_bob
    for v in (0, 1, BOND - 1, BOND + 1, 2 * BOND, GEN):
        direct_vm.value = v
        with direct_vm.expect_revert("ERR_BOND_MUST_BE_EXACTLY_0.1_GEN"):
            c.submit_prior_art(pid, ARXIV, "2018-01-02")
    assert c.get_challenge_count() == 0
    assert assert_solvent(c)["locked_bonds"] == "0"
    direct_vm.value = BOND
    assert c.submit_prior_art(pid, ARXIV, "2018-01-02") == 1


def test_submit_records_challenge(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice)
    cid = submit(c, direct_vm, direct_bob, pid, url="HTTPS://ARXIV.ORG/abs/1801.00001#x", claimed="2018-01-02")
    ch = c.get_challenge(cid)
    assert ch["patent_id"] == pid
    assert ch["challenger_address"] == hx(direct_bob)
    assert ch["prior_art_url"] == "https://arxiv.org/abs/1801.00001"
    assert ch["claimed_pub_date"] == "2018-01-02"
    assert ch["challenger_bond"] == str(BOND)
    assert ch["status"] == "PENDING"
    assert ch["disputed_at"] == 0
    assert c.get_patent(pid)["pending_challenges"] == 1
    L = assert_solvent(c)
    assert L["locked_bonds"] == str(BOND)
    with direct_vm.expect_revert("ERR_UNKNOWN_ID"):
        c.get_verdict(cid)  # no verdict until evaluation


def test_submit_input_validation(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice)
    fund(direct_vm, direct_bob)
    direct_vm.sender = direct_bob
    direct_vm.value = BOND
    with direct_vm.expect_revert("ERR_UNSAFE_URL"):
        c.submit_prior_art(pid, "http://arxiv.org/abs/1", "2018-01-02")
    with direct_vm.expect_revert("ERR_UNSAFE_URL"):
        c.submit_prior_art(pid, "https://arxiv.org.evil.com/abs/1", "2018-01-02")
    for bad in ("", "2018-13-01", "yesterday", "2018-1-2", "1700-01-01"):
        with direct_vm.expect_revert("ERR_INVALID_DATE"):
            c.submit_prior_art(pid, ARXIV, bad)
    with direct_vm.expect_revert("ERR_UNKNOWN_ID"):
        c.submit_prior_art(55, ARXIV, "2018-01-02")
    assert c.get_challenge_count() == 0
    assert c.get_patent(pid)["pending_challenges"] == 0


def test_inventor_cannot_challenge_own_patent(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    fund(direct_vm, direct_alice)
    direct_vm.sender = direct_alice
    direct_vm.value = BOND
    with direct_vm.expect_revert("ERR_INVENTOR_CANNOT_CHALLENGE_OWN_PATENT"):
        c.submit_prior_art(pid, ARXIV, "2018-01-02")
    assert assert_solvent(c)["locked_bonds"] == "0"


def test_duplicate_citation_rules(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    c = direct_deploy(CONTRACT)
    p1 = register(c, direct_vm, direct_alice)
    p2 = register(c, direct_vm, direct_alice)
    submit(c, direct_vm, direct_bob, p1, url=ARXIV)
    # same source, same patent, any spelling, any challenger: refused while live
    for variant in (ARXIV, "HTTPS://ARXIV.ORG/abs/1801.00001", ARXIV + "#frag", "https://arxiv.org:443/abs/1801.00001"):
        for who in (direct_bob, direct_charlie):
            fund(direct_vm, who)
            direct_vm.sender = who
            direct_vm.value = BOND
            with direct_vm.expect_revert("ERR_DUPLICATE_CHALLENGE"):
                c.submit_prior_art(p1, variant, "2018-01-02")
    # but it can be cited against a different patent
    assert submit(c, direct_vm, direct_charlie, p2, url=ARXIV) == 2
    # and a different source against the same patent
    assert submit(c, direct_vm, direct_charlie, p1, url="https://arxiv.org/abs/1801.00002") == 3


def test_challenge_against_invalidated_patent_reverts(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    _, outcome = run_challenge(c, direct_vm, direct_bob, pid, ARXIV, "2018-01-02")
    assert outcome == "INVALIDATED"
    fund(direct_vm, direct_charlie)
    direct_vm.sender = direct_charlie
    direct_vm.value = BOND
    with direct_vm.expect_revert("ERR_INVALID_STATE"):
        c.submit_prior_art(pid, "https://arxiv.org/abs/1801.00009", "2018-01-02")
    direct_vm.value = MIN_CONTRIB
    with direct_vm.expect_revert("ERR_INVALID_STATE"):
        c.fund_bounty(pid)
    direct_vm.sender = direct_alice
    direct_vm.value = 0
    with direct_vm.expect_revert("ERR_INVALID_STATE"):
        c.expire_patent(pid)
