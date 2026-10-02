"""Slashing arithmetic, stale-timeout voiding, pull withdrawals, governance."""

import pytest

from conftest import *  # noqa: F401,F403

HALF = BOND // 2
WEEK = 7 * DAY


# ------------------------------------------------------------- slashing math
@pytest.mark.parametrize("n", [1, 2, 3, 5, 8])
def test_n_rejections_split_exactly_fifty_fifty(direct_vm, direct_deploy, direct_alice, n):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    for i in range(n):
        who = make_account(10 + i)
        _, out = run_challenge(c, direct_vm, who, pid, f"https://arxiv.org/abs/2201.{i:05d}", "2022-01-01")
        assert out == "VALID"
        assert credit(c, who) == 0
    L = assert_solvent(c)
    assert credit(c, direct_alice) == n * HALF
    assert int(L["protocol_vault"]) == n * HALF
    assert credit(c, direct_alice) + int(L["protocol_vault"]) == n * BOND
    assert L["active_bounties"] == str(GEN)
    assert c.get_patent(pid)["status"] == "ACTIVE"


def test_defender_is_the_inventor_not_the_funder(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice)
    fund(direct_vm, direct_charlie)
    direct_vm.sender = direct_charlie
    direct_vm.value = GEN
    c.fund_bounty(pid)
    direct_vm.value = 0
    run_challenge(c, direct_vm, direct_bob, pid, ARXIV, "2022-01-01")
    assert credit(c, direct_alice) == HALF
    assert credit(c, direct_charlie) == 0
    assert credit(c, direct_bob) == 0


def test_bond_constant_has_no_rounding_remainder():
    # 0.1 GEN is an even number of wei, so the 50/50 split is exact and the vault's
    # "remainder" share is zero. The contract still routes odd wei to the vault.
    assert BOND % 2 == 0
    assert HALF + (BOND - HALF) == BOND


def test_slash_never_touches_bounty_pool(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=5 * GEN)
    run_challenge(c, direct_vm, direct_bob, pid, ARXIV, "2022-01-01")
    assert c.get_patent(pid)["active_bounty"] == str(5 * GEN)
    assert assert_solvent(c)["active_bounties"] == str(5 * GEN)


# -------------------------------------------------------------- stale voiding
def test_stale_challenge_voids_after_seven_days_with_full_refund(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    cid = submit(c, direct_vm, direct_bob, pid)
    created = c.get_challenge(cid)["created_at"]
    warp_to(direct_vm, created + WEEK)
    direct_vm.sender = direct_charlie  # anyone at all
    c.void_stale_challenge(cid)
    ch = c.get_challenge(cid)
    assert ch["status"] == "VOIDED"
    assert c.get_verdict(cid)["outcome"] == "AMBIGUOUS_VOID"
    assert credit(c, direct_bob) == BOND
    assert credit(c, direct_alice) == 0
    assert credit(c, direct_charlie) == 0
    p = c.get_patent(pid)
    assert p["pending_challenges"] == 0
    assert p["status"] == "ACTIVE"
    assert p["active_bounty"] == str(GEN)
    L = assert_solvent(c)
    assert L["locked_bonds"] == "0"
    assert L["protocol_vault"] == "0"


@pytest.mark.parametrize("offset", [0, 1, DAY, 3 * DAY, WEEK - DAY, WEEK - 1])
def test_stale_void_too_early_reverts(direct_vm, direct_deploy, direct_alice, direct_bob, offset):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice)
    cid = submit(c, direct_vm, direct_bob, pid)
    created = c.get_challenge(cid)["created_at"]
    warp_to(direct_vm, created + offset)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("ERR_NOT_STALE"):
        c.void_stale_challenge(cid)
    assert c.get_challenge(cid)["status"] == "PENDING"
    assert assert_solvent(c)["locked_bonds"] == str(BOND)


@pytest.mark.parametrize("offset", [WEEK, WEEK + 1, 30 * DAY, 365 * DAY])
def test_stale_void_at_or_after_deadline_succeeds(direct_vm, direct_deploy, direct_alice, direct_bob, offset):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice)
    cid = submit(c, direct_vm, direct_bob, pid)
    warp_to(direct_vm, c.get_challenge(cid)["created_at"] + offset)
    c.void_stale_challenge(cid)
    assert credit(c, direct_bob) == BOND
    assert c.get_challenge(cid)["status"] == "VOIDED"


def test_stale_void_only_once_and_never_after_settlement(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice)
    stale = submit(c, direct_vm, direct_bob, pid)
    warp_to(direct_vm, c.get_challenge(stale)["created_at"] + WEEK)
    c.void_stale_challenge(stale)
    with direct_vm.expect_revert("ERR_INVALID_STATE"):
        c.void_stale_challenge(stale)
    assert credit(c, direct_bob) == BOND  # refunded once
    # a settled (non-pending) challenge can never be voided retroactively
    cid, _ = run_challenge(c, direct_vm, direct_bob, pid, "https://arxiv.org/abs/2201.00001", "2022-01-01")
    warp_to(direct_vm, c.get_challenge(cid)["created_at"] + 2 * WEEK)
    with direct_vm.expect_revert("ERR_INVALID_STATE"):
        c.void_stale_challenge(cid)
    with direct_vm.expect_revert("ERR_UNKNOWN_ID"):
        c.void_stale_challenge(404)


def test_stale_void_frees_citation_and_evaluate_is_then_closed(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice)
    cid = submit(c, direct_vm, direct_bob, pid)
    warp_to(direct_vm, c.get_challenge(cid)["created_at"] + WEEK)
    c.void_stale_challenge(cid)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("ERR_INVALID_STATE"):
        c.evaluate_prior_art(cid)
    assert submit(c, direct_vm, direct_bob, pid) == 2  # same source can be cited afresh


def test_stale_void_unlocks_inventor_expiry(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    cid = submit(c, direct_vm, direct_bob, pid)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("ERR_INVALID_STATE"):
        c.expire_patent(pid)
    warp_to(direct_vm, c.get_challenge(cid)["created_at"] + WEEK)
    c.void_stale_challenge(cid)
    direct_vm.sender = direct_alice
    c.expire_patent(pid)
    assert c.get_patent(pid)["status"] == "EXPIRED"
    assert credit(c, direct_alice) == GEN
    assert credit(c, direct_bob) == BOND


# ----------------------------------------------------------------- withdrawals
def test_pull_withdraw_pays_once(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    run_challenge(c, direct_vm, direct_bob, pid, ARXIV, "2018-01-02")
    before = assert_solvent(c)
    direct_vm.sender = direct_bob
    assert c.pull_withdraw() == str(BOND + GEN)
    L = assert_solvent(c)
    assert L["claimable_credits"] == "0"
    assert L["total_withdrawn"] == str(BOND + GEN)
    assert L["tracked_total"] == "0"
    assert before["tracked_total"] == str(BOND + GEN)
    assert credit(c, direct_bob) == 0
    with direct_vm.expect_revert("ERR_NO_CLAIMABLE_BALANCE"):
        c.pull_withdraw()


def test_pull_withdraw_requires_a_credit(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy(CONTRACT)
    register(c, direct_vm, direct_alice, bounty=GEN)  # a bounty is not a credit
    for who in (direct_alice, direct_bob):
        direct_vm.sender = who
        with direct_vm.expect_revert("ERR_NO_CLAIMABLE_BALANCE"):
            c.pull_withdraw()
    assert assert_solvent(c)["total_withdrawn"] == "0"


def test_withdrawals_are_isolated_per_account(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice, bounty=GEN)
    run_challenge(c, direct_vm, direct_bob, pid, "https://arxiv.org/abs/2201.00001", "2022-01-01")
    run_challenge(c, direct_vm, direct_charlie, pid, "https://arxiv.org/abs/2201.00002", "2022-01-01")
    assert credit(c, direct_alice) == BOND  # two half-bonds
    direct_vm.sender = direct_alice
    assert c.pull_withdraw() == str(BOND)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("ERR_NO_CLAIMABLE_BALANCE"):
        c.pull_withdraw()
    L = assert_solvent(c)
    assert L["protocol_vault"] == str(BOND)  # vault is not withdrawable by users
    assert L["total_withdrawn"] == str(BOND)


def test_claimable_lookup_is_case_insensitive(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice)
    run_challenge(c, direct_vm, direct_bob, pid, ARXIV, "2022-01-01")
    h = hx(direct_alice)
    assert c.claimable_of(h) == c.claimable_of(h.upper().replace("0X", "0x")) == str(HALF)
    assert c.claimable_of("0x" + "ab" * 20) == "0"


def test_credits_accumulate_across_roles(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Alice is a defender on one patent and a winning challenger on another."""
    c = direct_deploy(CONTRACT)
    p_alice = register(c, direct_vm, direct_alice)
    p_bob = register(c, direct_vm, direct_bob, bounty=GEN)
    run_challenge(c, direct_vm, direct_bob, p_alice, "https://arxiv.org/abs/2201.00001", "2022-01-01")  # alice +HALF
    run_challenge(c, direct_vm, direct_alice, p_bob, "https://arxiv.org/abs/1801.00001", "2018-01-02")  # alice +BOND+GEN
    assert credit(c, direct_alice) == HALF + BOND + GEN
    direct_vm.sender = direct_alice
    assert c.pull_withdraw() == str(HALF + BOND + GEN)
    assert assert_solvent(c)["protocol_vault"] == str(HALF)


# ------------------------------------------------------------------ governance
def test_governor_is_deployer_and_sweeps_vault_via_pull(direct_vm, direct_deploy, direct_alice, direct_bob, direct_owner):
    c = direct_deploy(CONTRACT)
    assert c.get_governor() == hx(direct_owner)
    pid = register(c, direct_vm, direct_alice)
    run_challenge(c, direct_vm, direct_bob, pid, ARXIV, "2022-01-01")
    treasury = make_account(0xAA)
    direct_vm.sender = direct_owner
    c.sweep_vault(hx(treasury), HALF)
    L = assert_solvent(c)
    assert L["protocol_vault"] == "0"
    assert credit(c, treasury) == HALF
    direct_vm.sender = treasury
    assert c.pull_withdraw() == str(HALF)
    assert assert_solvent(c)["claimable_credits"] == str(HALF)  # alice's defender share remains


def test_sweep_vault_guards(direct_vm, direct_deploy, direct_alice, direct_bob, direct_owner):
    c = direct_deploy(CONTRACT)
    pid = register(c, direct_vm, direct_alice)
    run_challenge(c, direct_vm, direct_bob, pid, ARXIV, "2022-01-01")
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("ERR_UNAUTHORIZED"):
        c.sweep_vault(hx(direct_bob), 1)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("ERR_UNAUTHORIZED"):
        c.sweep_vault(hx(direct_alice), 1)
    direct_vm.sender = direct_owner
    for bad in (0, HALF + 1, 10**30):
        with direct_vm.expect_revert("ERR_INVALID_VALUE"):
            c.sweep_vault(hx(direct_owner), bad)
    assert assert_solvent(c)["protocol_vault"] == str(HALF)


def test_governor_rotation(direct_vm, direct_deploy, direct_alice, direct_owner):
    c = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("ERR_UNAUTHORIZED"):
        c.transfer_governor(hx(direct_alice))
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("ERR_INVALID_INPUT"):
        c.transfer_governor("0x" + "0" * 40)
    c.transfer_governor(hx(direct_alice))
    assert c.get_governor() == hx(direct_alice)
    with direct_vm.expect_revert("ERR_UNAUTHORIZED"):  # the old governor lost the key
        c.transfer_governor(hx(direct_owner))
