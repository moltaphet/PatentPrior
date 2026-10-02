"""Solvency identity under concurrent claims, withdrawals and expiry, checked
against an independent shadow ledger after every single step."""

import random

from conftest import *  # noqa: F401,F403

HALF = BOND // 2
WEEK = 7 * DAY


class Shadow:
    """Straight-line re-implementation of the money rules, kept deliberately dumb."""

    def __init__(self):
        self.bounties = {}      # pid -> int
        self.contrib = {}       # (pid, who) -> int
        self.locked = 0
        self.credits = {}       # who -> int
        self.vault = 0
        self.deposited = 0
        self.withdrawn = 0
        self.open = {}          # cid -> (pid, challenger)
        self.inventor = {}      # pid -> who
        self.alive = set()      # active pids

    def credit(self, who, n):
        self.credits[who] = self.credits.get(who, 0) + n

    def check(self, c):
        L = c.get_ledger()
        assert L["active_bounties"] == str(sum(self.bounties[p] for p in self.alive)), L
        assert L["locked_bonds"] == str(self.locked), L
        assert L["claimable_credits"] == str(sum(self.credits.values())), L
        assert L["protocol_vault"] == str(self.vault), L
        assert L["total_deposited"] == str(self.deposited), L
        assert L["total_withdrawn"] == str(self.withdrawn), L
        assert L["ledger_identity_holds"] is True, L
        assert self.deposited - self.withdrawn == (
            sum(self.bounties[p] for p in self.alive) + self.locked + sum(self.credits.values()) + self.vault
        )
        for who, amt in self.credits.items():
            assert credit(c, who) == amt


def test_concurrent_claims_and_withdrawals_scripted(direct_vm, direct_deploy):
    c = direct_deploy(CONTRACT)
    sh = Shadow()
    inv = [make_account(1), make_account(2), make_account(3)]
    ch = [make_account(10 + i) for i in range(6)]

    pids = []
    for i, who in enumerate(inv):
        amt = (i + 1) * GEN
        pid = register(c, direct_vm, who, bounty=amt)
        pids.append(pid)
        sh.bounties[pid] = amt
        sh.alive.add(pid)
        sh.inventor[pid] = who
        sh.deposited += amt
        sh.check(c)

    # six concurrent challenges, two per patent
    cids = []
    mock_source(direct_vm, r"arxiv\.org", "2018-01-02")
    for i, who in enumerate(ch):
        pid = pids[i % 3]
        url = f"https://arxiv.org/abs/1801.{i:05d}"
        direct_vm.clear_mocks()
        cid = submit(c, direct_vm, who, pid, url=url)
        cids.append((cid, pid, who, url))
        sh.open[cid] = (pid, who)
        sh.locked += BOND
        sh.deposited += BOND
        sh.check(c)

    def settle(cid, pid, who, url, pub, **kw):
        esc = url.replace(".", r"\.")
        direct_vm.clear_mocks()
        mock_source(direct_vm, esc, pub)
        mock_tribunal(direct_vm, esc, pub=pub, **kw)
        direct_vm.sender = who
        return c.evaluate_prior_art(cid)

    # 0: patent 1 invalidated by challenger 0
    cid, pid, who, url = cids[0]
    assert settle(cid, pid, who, url, "2018-01-02") == "INVALIDATED"
    sh.locked -= BOND
    sh.credit(who, BOND + sh.bounties[pid])
    sh.bounties[pid] = 0
    sh.alive.discard(pid)
    del sh.open[cid]
    sh.check(c)

    # 3: second challenger on patent 1 is mooted -> refund
    cid, pid, who, url = cids[3]
    assert settle(cid, pid, who, url, "2018-01-02") == "AMBIGUOUS_VOID"
    sh.locked -= BOND
    sh.credit(who, BOND)
    del sh.open[cid]
    sh.check(c)

    # 1 and 4 on patent 2: one rejected (slash), one void (dead link)
    cid, pid, who, url = cids[1]
    assert settle(cid, pid, who, url, "2022-01-01") == "VALID"
    sh.locked -= BOND
    sh.credit(sh.inventor[pid], HALF)
    sh.vault += BOND - HALF
    del sh.open[cid]
    sh.check(c)

    cid, pid, who, url = cids[4]
    direct_vm.clear_mocks()
    direct_vm.mock_web(r"arxiv\.org", {"status": 404, "body": ""})
    direct_vm.sender = who
    assert c.evaluate_prior_art(cid) == "AMBIGUOUS_VOID"
    sh.locked -= BOND
    sh.credit(who, BOND)
    del sh.open[cid]
    sh.check(c)

    # 2 and 5 on patent 3: stale-void one, reject the other
    cid, pid, who, url = cids[2]
    warp_to(direct_vm, c.get_challenge(cid)["created_at"] + WEEK)
    direct_vm.sender = make_account(77)
    c.void_stale_challenge(cid)
    sh.locked -= BOND
    sh.credit(who, BOND)
    del sh.open[cid]
    sh.check(c)

    cid, pid, who, url = cids[5]
    assert settle(cid, pid, who, url, "2023-03-03") == "VALID"
    sh.locked -= BOND
    sh.credit(sh.inventor[pid], HALF)
    sh.vault += BOND - HALF
    del sh.open[cid]
    sh.check(c)
    assert sh.locked == 0

    # patent 3 retires with nothing pending: bounty returns to its funder
    direct_vm.sender = inv[2]
    c.expire_patent(pids[2])
    sh.credit(inv[2], sh.bounties[pids[2]])
    sh.bounties[pids[2]] = 0
    sh.alive.discard(pids[2])
    sh.check(c)

    # everyone withdraws; the vault stays behind
    for who in list(sh.credits):
        if sh.credits[who] == 0:
            continue
        direct_vm.sender = who
        paid = int(c.pull_withdraw())
        assert paid == sh.credits[who]
        sh.withdrawn += paid
        sh.credits[who] = 0
        sh.check(c)
    assert sh.vault == BOND  # two rejections, half-bond each
    assert c.get_ledger()["claimable_credits"] == "0"
    # remaining liabilities: patent 2's live bounty and the vault
    L = c.get_ledger()
    assert int(L["tracked_total"]) == sh.bounties[pids[1]] + sh.vault


def test_randomised_walk_never_breaks_the_ledger(direct_vm, direct_deploy):
    rng = random.Random(61997)
    c = direct_deploy(CONTRACT)
    sh = Shadow()
    inventors = [make_account(1 + i) for i in range(4)]
    challengers = [make_account(20 + i) for i in range(5)]
    funders = [make_account(40 + i) for i in range(3)]
    created_at = {}
    url_n = [0]
    pids = []
    for who in inventors:
        amt = rng.choice([0, GEN, 2 * GEN])
        pid = register(c, direct_vm, who, bounty=amt)
        pids.append(pid)
        sh.bounties[pid] = amt
        sh.contrib[(pid, who)] = amt
        sh.alive.add(pid)
        sh.inventor[pid] = who
        sh.deposited += amt
    sh.check(c)

    seen = set()
    for step in range(90):
        action = rng.choice(["fund", "fund", "submit", "submit", "eval_slash", "eval_slash", "eval_void", "stale", "withdraw", "expire", "expire", "eval_ok"])
        if action == "fund":
            live = sorted(sh.alive)
            if not live:
                continue
            pid, who, amt = rng.choice(live), rng.choice(funders), rng.choice([MIN_CONTRIB, GEN])
            fund(direct_vm, who)
            direct_vm.sender, direct_vm.value = who, amt
            c.fund_bounty(pid)
            direct_vm.value = 0
            sh.bounties[pid] += amt
            sh.contrib[(pid, who)] = sh.contrib.get((pid, who), 0) + amt
            sh.deposited += amt
        elif action == "submit":
            live = sorted(sh.alive)
            if not live:
                continue
            pid, who = rng.choice(live), rng.choice(challengers)
            url_n[0] += 1
            url = f"https://arxiv.org/abs/3001.{url_n[0]:05d}"
            cid = submit(c, direct_vm, who, pid, url=url)
            created_at[cid] = c.get_challenge(cid)["created_at"]
            sh.open[cid] = (pid, who)
            sh.locked += BOND
            sh.deposited += BOND
            sh.url = getattr(sh, "url", {})
            sh.url[cid] = url
        elif action in ("eval_ok", "eval_slash", "eval_void") and sh.open:
            cid = rng.choice(sorted(sh.open))
            pid, who = sh.open[cid]
            url = sh.url[cid].replace(".", r"\.")
            direct_vm.clear_mocks()
            direct_vm.sender = rng.choice(challengers + inventors)
            if pid not in sh.alive:
                mock_source(direct_vm, url, "2018-01-02")
                mock_tribunal(direct_vm, url)
                assert c.evaluate_prior_art(cid) == "AMBIGUOUS_VOID"
                sh.locked -= BOND
                sh.credit(who, BOND)
            elif action == "eval_ok":
                mock_source(direct_vm, url, "2018-01-02")
                mock_tribunal(direct_vm, url)
                assert c.evaluate_prior_art(cid) == "INVALIDATED"
                sh.locked -= BOND
                sh.credit(who, BOND + sh.bounties[pid])
                sh.bounties[pid] = 0
                sh.alive.discard(pid)
            elif action == "eval_slash":
                mock_source(direct_vm, url, "2022-01-01")
                mock_tribunal(direct_vm, url, pub="2022-01-01")
                assert c.evaluate_prior_art(cid) == "VALID"
                sh.locked -= BOND
                sh.credit(sh.inventor[pid], HALF)
                sh.vault += BOND - HALF
            else:
                direct_vm.mock_web(url, {"status": 404, "body": ""})
                assert c.evaluate_prior_art(cid) == "AMBIGUOUS_VOID"
                sh.locked -= BOND
                sh.credit(who, BOND)
            del sh.open[cid]
        elif action == "stale" and sh.open:
            cid = rng.choice(sorted(sh.open))
            pid, who = sh.open[cid]
            warp_to(direct_vm, created_at[cid] + WEEK)
            direct_vm.sender = rng.choice(challengers)
            c.void_stale_challenge(cid)
            sh.locked -= BOND
            sh.credit(who, BOND)
            del sh.open[cid]
        elif action == "withdraw":
            owed = [w for w, a in sh.credits.items() if a > 0]
            if not owed:
                continue
            who = rng.choice(sorted(owed))
            direct_vm.sender = who
            assert int(c.pull_withdraw()) == sh.credits[who]
            sh.withdrawn += sh.credits[who]
            sh.credits[who] = 0
        elif action == "expire":
            idle = [p for p in sorted(sh.alive) if all(pp != p for pp, _ in sh.open.values())]
            if not idle:
                continue
            pid = rng.choice(idle)
            direct_vm.sender = sh.inventor[pid]
            c.expire_patent(pid)
            # independent expectation: each funder is credited exactly what they put in
            for (p_, w), amt in sorted(sh.contrib.items()):
                if p_ == pid and amt:
                    sh.credit(w, amt)
                    sh.contrib[(p_, w)] = 0
            assert sum(v for (p_, _), v in sh.contrib.items() if p_ == pid) == 0
            sh.alive.discard(pid)
            sh.bounties[pid] = 0
        seen.add(action)
        sh.check(c)

    assert {"fund", "submit", "eval_ok", "eval_slash", "eval_void", "stale", "withdraw", "expire"} <= seen, seen

    # final drain: settle every open challenge by stale-void, withdraw all
    for cid in sorted(sh.open):
        pid, who = sh.open[cid]
        warp_to(direct_vm, created_at[cid] + WEEK)
        direct_vm.sender = challengers[0]
        c.void_stale_challenge(cid)
        sh.locked -= BOND
        sh.credit(who, BOND)
    sh.open.clear()
    sh.check(c)
    assert sh.locked == 0
    assert c.get_ledger()["locked_bonds"] == "0"
