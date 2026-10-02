# PatentPrior

Autonomous patent prior-art adjudication and claim invalidation on [GenLayer](https://genlayer.com).
A pure smart-contract protocol: no frontend, no operator, no patent-office queue.

| | |
|---|---|
| Contract | [`contracts/patent_prior.py`](contracts/patent_prior.py) |
| Network | GenLayer Studio Next, chain `61997` |
| Live address (v2, hardened) | [`0xC94c5774C393f9e068Dc651c4251484DC85bD3e3`](https://explorer-studio-next.genlayer.com/address/0xC94c5774C393f9e068Dc651c4251484DC85bD3e3) |
| Tests | 176 tests, 1,772 executed assertions, all passing (direct mode) |
| Lint | `genvm-lint check contracts/patent_prior.py`: 0 errors |

> **Read §9 first if you intend to rely on payouts.** (v2 hardening is described in §9.1.) On Studio Next, the contract's outbound
> native-token transfers were observed to be *skipped by the network*, not by the contract.
> Everything else (adjudication, consensus, accounting) works live.

---

## 1. What it does

A patent troll asserts a vague claim and relies on the fact that invalidating it costs a defendant
more than settling. Patent offices answer with `inter partes` review queues measured in years.
PatentPrior replaces the queue with a market:

1. An inventor **registers** a claim, optionally seeding a defensive bounty.
2. Anyone may **fund** the bounty: a standing reward for breaking that specific claim.
3. A challenger **posts a 0.1 GEN bond** and cites a prior-art URL.
4. Anyone **triggers evaluation**. GenVM validators independently fetch the source, extract its
   publication date, and judge anticipation. Agreement is reached by custom equivalence consensus.
5. The contract applies the verdict mechanically: the claim falls and the challenger collects the
   bounty, or the challenger's bond is slashed, or (if the evidence is unusable) everyone is made whole.

No party can be paid by the verdict-maker, nobody has to trust a person to act, and a stuck
challenge can be unlocked by anyone after 7 days.

## 2. Lifecycle and state machine

```
                    register_patent (+ optional bounty)
                              │
                              ▼
        ┌────────────────  ACTIVE  ◄──── fund_bounty (anyone, ≥ 0.001 GEN)
        │                     │
        │       submit_prior_art (bond ≥ max(0.1 GEN, 2% of pool), whitelisted https URL, canonicalised)
        │                     │
        │                     ▼
        │             challenge PENDING ───────────── void_stale_challenge (anyone, ≥ 7 days)
        │                     │                                    │
        │   evaluate_prior_art (anyone, strictly FIFO per patent)   ▼
        │                     │                          VOIDED: bond refunded 100%
        │     ┌───────────────┼────────────────┐
        │     ▼               ▼                ▼
        │  INVALIDATED      VALID        AMBIGUOUS_VOID
        │  challenge UPHELD challenge REJECTED   challenge VOIDED
        │  patent INVALIDATED  patent stays ACTIVE   patent stays ACTIVE
        │  challenger ← bond + 100% bounty   bond ⅟₂ → inventor, ⅟₂ → vault   bond refunded 100%
        │
        └─ expire_patent (inventor only, no open challenge) ──► EXPIRED, funders refunded
```

| Entity | States |
|---|---|
| `PatentDossier.status` | `ACTIVE`, `INVALIDATED`, `EXPIRED` |
| `PriorArtChallenge.status` | `PENDING`, `UPHELD`, `REJECTED`, `VOIDED` |
| `InvalidationVerdict.outcome` | `INVALIDATED`, `VALID`, `AMBIGUOUS_VOID` |

Note on the brief's `DISPUTED` state: evaluation is a single atomic transaction, so a challenge is
never *persistently* disputed: it is `PENDING` until the evaluation settles it (`disputed_at` records
when). A transaction that fails (for example on a model fault) rolls back and leaves it `PENDING`
and retryable, which is why stale-voiding only needs to cover `PENDING`.

A patent's challenges are adjudicated **strictly first-in-first-out**: `evaluate_prior_art(id)` reverts
`ERR_FIFO_ORDER` unless `id` is the oldest still-pending challenge for that patent. When the head
`INVALIDATES` the patent, every challenge queued behind it is **voided and refunded in full in the same
transaction**; a rejection or void simply hands the queue to the next. Anyone can stale-void a stuck head
after 7 days to unblock the queue. At most 16 challenges can be pending per patent.

## 3. The tribunal (evaluation)

`evaluate_prior_art` runs inside `gl.vm.run_nondet`. The **leader** fetches the cited page, at its canonical URL
(`gl.nondet.web.get`, 2xx only, tags stripped, capped at 14,000 chars) and prompts an LLM for
structured JSON. Each **validator** repeats the same work independently and votes to agree only if:

* the `outcome` is identical, and
* for non-void outcomes, the extracted `publication_date` and the temporal result are identical.

Any validator fault returns "disagree", which forces consensus to rotate rather than lock in broken output.

**The model never decides the date test.** It only *extracts* the publication date; the contract
compares that date with the stored priority date as calendar days (§5). A model that claims full
anticipation of a post-dated source is overruled (covered by `test_model_cannot_override_the_date_test`).

Prompt-injection hardening: patent title, claim and source text are each wrapped in their own tag,
sanitised so they cannot close a tag or forge a `=== N. ===` section header, and declared untrusted data.

| Condition | Result |
|---|---|
| Source unreachable, non-2xx, redirect, or empty body | `AMBIGUOUS_VOID` |
| Source states conflicting publication dates | `AMBIGUOUS_VOID` |
| No usable publication date in the source | `AMBIGUOUS_VOID` |
| Model confidence < 60 | `AMBIGUOUS_VOID` |
| Malformed model output (not JSON, non-numeric confidence) | transaction reverts `[LLM_ERROR]`, challenge stays `PENDING` |
| All three criteria met | `INVALIDATED` |
| Otherwise | `VALID` |

## 4. Mathematical formalisation

Let `B₀ = 10¹⁷` wei (0.1 GEN) be the base bond, `P` a patent's bounty pool, `P = Σᵢ cᵢ` over funders,
and `B = max(B₀, ⌊2P/100⌋)` the bond required at submission (the challenger may overpay; the whole amount
is locked and `B` below denotes the amount actually locked).

**Settlement of one challenge** (`c` = challenger, `d` = inventor/defender, `V` = vault):

| Outcome | Credit to `c` | Credit to `d` | Vault `V` | Pool `P` |
|---|---|---|---|---|
| `INVALIDATED` | `B + P` | 0 | 0 | `P → 0` |
| `VALID` | 0 | `⌊B/2⌋` | `B − ⌊B/2⌋` | unchanged |
| `AMBIGUOUS_VOID` / stale | `B` | 0 | 0 | unchanged |

In every row the credits sum to `B` plus whatever leaves the pool, so a settlement moves value between
buckets and never creates or destroys it. For `B₀` (even) the slash splits exactly 50/50; for an odd `B`
the `B − ⌊B/2⌋` form routes the odd wei to the vault. When a challenge `INVALIDATES`, each follower `f`
in the queue is additionally refunded `B_f` (`L → C`).

**Solvency invariant.** Four liability buckets are tracked:

```
A = Σ active bounties      L = Σ locked bonds
C = Σ claimable credits    V = protocol vault
```

and two flow counters `D` (total deposited) and `W` (total withdrawn). The contract asserts, at the
end of **every** state-changing method,

```
D − W  =  A + L + C + V                                   (ledger identity)
```

and the intended on-chain balance relation is

```
contract_balance  =  A + L + C + V   (+ value of emitted transfers not yet settled)
```

`rescue_excess` operates on the difference `E = contract_balance − (A + L + C + V)`; it moves only `E`
(when `E > 0` and no payout was emitted in the last 24 h) and writes none of `A, L, C, V, D, W`, so the
ledger identity is unaffected by it.

Each method is a transfer between buckets (`A→C`, `L→C`, `L→V`, `V→C`, `A→C`) or an external flow that
moves exactly one of `D`, `W` together with exactly one bucket by the same amount:

| Method | Effect |
|---|---|
| `register_patent`, `fund_bounty` | `D += v`, `A += v` |
| `submit_prior_art` | `D += B`, `L += B` |
| settlement / stale void | `L −= B` ; `C` or `V` or `A` change by the same total |
| `expire_patent` | `A −= P`, `C += P` |
| `sweep_vault` | `V −= x`, `C += x` |
| `rescue_excess` | none of `A, L, C, V, D, W`; `contract_balance −= E` |
| `pull_withdraw` | `C −= x`, `W += x` |

so `D − W − (A+L+C+V)` is invariant. This is enforced in code and re-derived independently by a shadow
ledger in `tests/test_invariants.py`, which replays 90 randomised operations and compares every bucket
after every step.

## 5. Alignment with 35 U.S.C. § 102 and WIPO practice

| Legal requirement | Where it lives |
|---|---|
| § 102(a)(1): "published … **before** the effective filing date" | Contract compares `publication_date < priority_date` strictly, at day granularity. Same-day is **not** prior art. Time-of-day in the priority timestamp is ignored. |
| Imprecise dates | A month-only (`2020-05`) or year-only publication resolves to the **last** day of the period, so imprecision can never help the challenger. |
| Anticipation: the reference discloses **every element** as claimed | Model criterion `full_anticipation`. Partial disclosure fails. |
| Enablement: a skilled person could make and use it | Model criterion `enabling_detail`. A non-enabling disclosure fails. |
| Burden on the challenger | The challenger posts the bond; a failed challenge is slashed. |

This is an *economic and procedural analogue* of anticipation review, not legal advice. See §9.

## 6. Interface

| Method | Kind | Notes |
|---|---|---|
| `register_patent(title, priority_date, claim_text)` | payable | attached value is the initial bounty (0, or ≥ 0.05 GEN). `priority_date` is `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SSZ`, ≥ 1790, not in the future |
| `fund_bounty(patent_id)` | payable | ≥ **0.05 GEN** (dust cannot exhaust the 32 funder slots); at most 32 distinct funders per patent |
| `submit_prior_art(patent_id, url, claimed_pub_date)` | payable | value must be **≥ `max(0.1 GEN, 2% of the pool)`** (`required_bond(patent_id)`); inventor cannot challenge own patent; duplicate canonical (patent, source) refused; ≤ 16 pending per patent |
| `evaluate_prior_art(challenge_id)` | write | permissionless, once, **FIFO per patent** (`next_evaluable(patent_id)`) |
| `void_stale_challenge(challenge_id)` | write | permissionless, `PENDING` for ≥ 7 days |
| `expire_patent(patent_id)` | write | inventor only, no open challenge; refunds each funder what they put in |
| `pull_withdraw()` | write | pays out the caller's credits |
| `sweep_vault(to, amount)`, `rescue_excess(recipient)`, `transfer_governor(addr)` | write | governor only (the deployer) |
| views | | `get_patent`, `get_challenge`, `get_verdict`, `get_ledger`, `claimable_of`, `contribution_of`, `check_url`, `get_constants`, `get_governor`, `required_bond`, `next_evaluable`, `get_patent_count`, `get_challenge_count`, `whoami` |

**URL policy** (`_normalize_url` then `_canonicalize_url`): `https` only; no userinfo; port absent or `443`;
ASCII only, no whitespace/control/backslash; no IP literals or numeric hosts; well-formed host labels; the host
must equal, or be a whole-label subdomain of, a whitelisted domain. The whitelist holds **authoritative,
immutable publishers only**: `arxiv.org`, `patents.google.com`, `patentscope.wipo.int`,
`worldwide.espacenet.com`, `ieeexplore.ieee.org`, `dl.acm.org`, `doi.org`, `datatracker.ietf.org`,
`rfc-editor.org`, `w3.org`, `nature.com`, `sciencedirect.com`, `springer.com`, `biorxiv.org`,
`eprint.iacr.org`. Removed in v2: `web.archive.org`, `openreview.net`, `hal.science` (anyone can publish or
replace content there) and `semanticscholar.org` (an aggregator). `arxiv.org.evil.com`, `evilarxiv.org`,
`evil.com@arxiv.org`, punycode and Cyrillic look-alikes are refused.

`_canonicalize_url` then yields one spelling per source: lower-case host, **no query, no fragment, no
trailing slash**, duplicate slashes collapsed, unreserved percent-escapes decoded (others upper-cased), dot
segments rejected, and for arXiv the version suffix, `/pdf/` and `/html/` spellings and the `www`/`export`
mirrors all fold into `https://arxiv.org/abs/<id>`. That canonical string is what is stored, hashed
(`sha256(patent_id|canonical_url)`) for per-patent duplicate detection, and fetched.

## 7. Tests

```bash
uv venv --python 3.12
uv pip install --prerelease=allow genlayer-test==0.30.0rc2 genlayer-py==0.19.0rc2 genvm-linter==0.11.1rc2 pytest==9.1.1 web3==8.0.0 eth-account==0.14.0
.venv/bin/python -m pytest tests -q         # 176 passed; prints the executed-assertion count
.venv/bin/genvm-lint check contracts/patent_prior.py
```

| File | Covers |
|---|---|
| `test_urls.py` | 105 hostile and valid URLs (20 accepted, 85 refused): schemes, userinfo, ports, spoofed domains, purged hosts, private/IP hosts, dot segments, unicode, control characters, length boundary |
| `test_registration.py` | input validation, dates, bounty floor, funder cap, expiry refunds, bond exactness, duplicate rules, self-challenge |
| `test_adjudication.py` | invalidation, post-dated prior art, partial disclosure, non-enabling disclosure, 18-case date boundary table, 3-criteria truth table, 11 unreachable-HTTP statuses, ambiguous findings, sloppy/garbage model output, hostile source text, follower voiding after an invalidation |
| `test_economics.py` | 50/50 slashing arithmetic, stale-void boundary (`7d − 1s` refused, `7d` accepted), pull-withdraw, governor |
| `test_invariants.py` | scripted multi-patent/multi-challenger scenario and a seeded 120-step random walk against an independent shadow ledger that re-implements the FIFO queue and the proportional bond |
| `test_patent_prior.py` | the audit regressions: purged hosts, proportional bond, arXiv version/spelling canonicalisation, FIFO ordering and eager follower refunds, `rescue_excess` accounting and in-flight guard, dust backers |

The run reports **1,470 `assert` statements plus 302 reverts verified by `expect_revert` = 1,772**
executed assertions (counted at runtime by a pytest hook in `tests/conftest.py`). The count is lower than the
v1 figure (2,047) even though there are more tests (176 vs 138); I rewrote the invariants test and did not
investigate the difference further, so treat the two figures as measured, not as a like-for-like comparison.

Mutation checks: the 7 v1 mutants (wrong slash fraction, `<=` in the date test, bounty forgone, stale window
off by one, self-challenge allowed, void slashing the bond, withdraw skipping the ledger) and **8 new ones**
for the v2 defenses (FIFO check removed, followers not voided, bond not scaled, arXiv version not stripped,
dust floor lowered, rescue ignoring credits, rescue ignoring the in-flight window, web archive
re-whitelisted) were each caught by the suite. The 8 v2 mutants were run against `test_patent_prior.py`
alone; the v1 mutants were run against the then-full suite and were not re-run against v2.

**What direct mode cannot show.** It runs the leader only, so validator agreement is exercised only by the
live run in §8. It also does not credit `msg.value` to the contract balance, so the *balance* relation
cannot be asserted in direct mode; the *ledger identity* is.

## 8. Live deployment on Studio Next (chain 61997)

Reproduce with `python scripts/deploy.py` then `python scripts/interact_live.py` (resumable). Metadata is in
[`deployments/studio-next.json`](deployments/studio-next.json), including every transaction's validator
votes, per-validator `contract_state_hash`, `validator_votes_hash` list and a consensus digest. The superseded
v1 deployment record (`0xf5a0b81D…1796`, which has none of the §9.1 defenses) is kept in
`deployments/studio-next.v1.json`.

| | |
|---|---|
| Contract (v2) | `0xC94c5774C393f9e068Dc651c4251484DC85bD3e3` |
| Deploy tx | [`0x274f08da…9b96`](https://explorer-studio-next.genlayer.com/tx/0x274f08da9b927fe5e9b85bc3c8f970a1f5e394426ae68f829a811c46c6ec9b96) |
| Source SHA-256 | `58818780b9b900b369ef991d6cc0c473347015c9f2bfd06c8a9fb9c1d1d8e002` |
| Runner | `py-genlayer:5jycge4q8k23462jtb0b9fyey1s9qz928sz2nbrd9mg4sxqg2qng` |
| Deployed | 2026-10-02T17:49:23Z by `0x0E54CdFAc8F3586C27650d8f6F55d25e0248E446` (a fresh key funded by `sim_fundAccount`) |

| Case | Outcome observed | Register | Submit | Evaluate | What happened |
|---|---|---|---|---|---|
| **1** software, priority 2020-03-01 | **INVALIDATED** (`UPHELD`) | [`0xeeaeec8e…6906`](https://explorer-studio-next.genlayer.com/tx/0xeeaeec8e29309637427b2bbce714601f31b500c6e549efc34f9f09be4dc76906) | [`0xa9b845f9…ac02`](https://explorer-studio-next.genlayer.com/tx/0xa9b845f9bd1c09abea4e049d8cf47891e7f37cc77f7d30c322de6fa8a492ac02) | [`0xaf6bc714…5a2f`](https://explorer-studio-next.genlayer.com/tx/0xaf6bc714391b7e6165492c736e69b8dc3541ecaa7dfeac8b31d66fd275815a2f) | BERT (arXiv:1810.04805), published 2018-10-11, confidence 99, all 3 criteria met. Challenger credited 0.6 GEN (0.1 bond + 0.5 bounty). |
| **1b** FIFO follower on patent 1 | **VOIDED**, refunded | n/a | [`0xf39086ec…bb56`](https://explorer-studio-next.genlayer.com/tx/0xf39086ecc3a5b911ab3f7b7328251c1bf6047c8e41e5ca2fbfbb25b6a28ebb56) | n/a (no evaluation needed) | A second challenger cited `arxiv.org/abs/1706.03762v7`; it was stored as `https://arxiv.org/abs/1706.03762` and queued behind #1. When #1 invalidated the patent, #2 was voided and 0.1 GEN credited back **in the same transaction**. |
| **2** hardware, priority 2015-01-15 | **VALID** (`REJECTED`) | [`0xea57b4b7…ccca`](https://explorer-studio-next.genlayer.com/tx/0xea57b4b7dbdcd4f8047f500052223a44746fc833c507352821176f1619bcccca) | [`0x421176fe…b353`](https://explorer-studio-next.genlayer.com/tx/0x421176feaeed325559fd998e3f8831ef0861c88aadbc1d65e80e597d9b13b353) | [`0x9ad9898d…7a4f`](https://explorer-studio-next.genlayer.com/tx/0x9ad9898d946e4c2543e823b8ab90b4c0592c315f9b1ea9c74ca532b0f4547a4f) | TPU paper (arXiv:1704.04760), published 2017-04-16, **after** priority: temporal criterion false (confidence 78). The model also found the abstract lacks several claim elements. Bond slashed 0.05 / 0.05. |
| **3** dead link | **AMBIGUOUS_VOID** (`VOIDED`) | [`0x92e13e0a…815a`](https://explorer-studio-next.genlayer.com/tx/0x92e13e0a152fcc7ad6878a46e9556df3961fa735d742c6281d8d2fc0ce7f815a) | [`0x59355666…e9c9`](https://explorer-studio-next.genlayer.com/tx/0x59355666fc688c6006c54fb9ca9d180df5b1868502863afa10b3e1ff94fae9c9) | [`0x1cb763df…a4d0`](https://explorer-studio-next.genlayer.com/tx/0x1cb763df964ef127bcf74f306ded88f5d3b4186897cd113581639dd94eafa4d0) | arXiv returned 404; no LLM call was needed. Full 0.1 GEN refund credited, no slash. |
| **4** open for challenges | **ACTIVE** | [`0x37f9ab2a…2bd1`](https://explorer-studio-next.genlayer.com/tx/0x37f9ab2a833354e2edb623b7ca5c8a653c77bcbe8ccf7bbfa65451ac311b2bd1) | n/a | `fund_bounty` [`0x732a70fd…fda1`](https://explorer-studio-next.genlayer.com/tx/0x732a70fd768bc1817adf16ffb896ec70d9b764993a2b2037f19752d4e7f7fda1) | Bounty 0.5 + 0.25 GEN community-funded, no challenge yet. |

`rescue_excess` live: the simulator's `sim_fundAccount` was used to credit the contract 0.3 GEN of stray
funds (balance 1.95 → 2.25 GEN against 1.95 GEN tracked); the governor called `rescue_excess`
[`0x78b41acc…a161`](https://explorer-studio-next.genlayer.com/tx/0x78b41accd48d81b91da855044991be89b0134a9544b0de881556e00bc1f5a161), which returned `0.3 GEN` and left every tracked bucket **unchanged**
(`tracked_untouched = True`). The balance stayed at 2.25 GEN afterwards, consistent with
the network skipping the outbound transfer (§9.2).

Settlement transactions (all `MAJORITY_AGREE`): `pull_withdraw` by challenger 1 [`0xf97f2c84…596c`](https://explorer-studio-next.genlayer.com/tx/0xf97f2c84dd7b8ae9fa7bee2ad964d3e923e1fe7d2fcb7c53fdcb434e597f596c),
inventor 2 [`0x84221d71…5a7f`](https://explorer-studio-next.genlayer.com/tx/0x84221d714a26c2b4f7390ef652cedc5d5808df766bf0539e8c4b09b9743b5a7f), challenger 3 [`0x739690ca…3423`](https://explorer-studio-next.genlayer.com/tx/0x739690ca45baa39517a26be78597bfd4fb68629b4aae547328965c2149923423);
`sweep_vault` [`0xb7763bd2…ff0d`](https://explorer-studio-next.genlayer.com/tx/0xb7763bd2c9b2aceaf6230817dad9afb9bc331a6689d95e862d187cd7f3faff0d) and governor `pull_withdraw` [`0x3c981857…30ae`](https://explorer-studio-next.genlayer.com/tx/0x3c9818571b4e06a999a3b9e2a6e9b4f80ff911c32cf10d90bacd1f53111b30ae).

Every recorded transaction was decided `MAJORITY_AGREE` with five validators voting three `agree` and two
`idle`. I did not investigate why two validators vote `idle` in every round; I am reporting it as observed.
Final live ledger: `D = 1.95 GEN`, `W = 0.80 GEN`, `A + L + C + V = 1.15 GEN`, so **the ledger identity
holds on-chain**. The contract balance is 2.25 GEN = 1.15 tracked + 0.80 skipped payouts + 0.30 rescued-but-
undelivered stray funds (§9.2).

## 9. Security invariants and known limitations

### 9.1 v2 hardening: the three audited issues, and what each one does and does not guarantee

| # | Invariant | Mechanism | Limit |
|---|---|---|---|
| I1 | A challenger cannot plant its own "prior art" on a host it controls | Whitelist holds authoritative, immutable publishers only; `web.archive.org`, `openreview.net`, `hal.science` (and `semanticscholar.org`) purged | A whitelisted publisher is still trusted to serve what it published. `doi.org` redirects to the publisher. |
| I2 | Attacking a rich pool costs more than a poor one | `bond ≥ max(0.1 GEN, 2% of the pool)`, re-read from the contract at submission | The bond is fixed when submitted; if the pool later grows, earlier bonds are not re-priced. It is a deterrent, not a substitute for I1. |
| I3 | One paper, one citation per patent, however it is spelled | `_canonicalize_url` (§6), `sha256(patent|canonical)`; the canonical URL is also what is fetched | **Query-identified sources are unsupported**: queries are stripped, so a document addressed only by `?id=` cannot be cited (the challenge would void, never slash). |
| I4 | Challenges settle in submission order; a successful invalidation refunds everyone queued behind it, immediately | Per-patent FIFO queue, `ERR_FIFO_ORDER`, eager follower voiding, 16-pending cap | A head that cannot be settled (repeated `[LLM_ERROR]`) blocks its patent's queue for up to 7 days until anyone stale-voids it. |
| I5 | Surplus balance can be rescued without touching a single wei of tracked liability | `rescue_excess`: governor only; `excess = balance − (A+L+C+V)`; reverts `ERR_NO_EXCESS_BALANCE` when `excess ≤ 0`; sends only `excess`; writes no accounting bucket | See the caveat below. |
| I6 | Dust cannot exhaust the 32 backer slots | `fund_bounty` and the initial bounty both require ≥ 0.05 GEN | A determined attacker can still pay 32 × 0.05 GEN to occupy the slots (that money becomes bounty, refunded to them only on expiry). |
| I7 | `D − W = A + L + C + V` after every method | `_assert_ledger()` on every write; shadow ledger in the tests | Not a statement about `contract_balance` (§9.2). |

**Caveat on I5 (read this before trusting the governor).** `excess` cannot tell a stray donation from a payout
that has been emitted and not yet settled, nor from a payout the network *skipped* (§9.2). v2 therefore refuses
`rescue_excess` for 24 h after any `pull_withdraw` (`ERR_PAYOUTS_IN_FLIGHT`). After the window, a skipped payout
is indistinguishable from surplus and the governor **could** sweep it to an address of its choosing. That is a
governor-trust assumption: the governor is expected to return such funds to the credited users, and nothing in
the contract enforces that.

Deviation from the brief: the formula `gl.get_balance()` does not exist in the SDK; the contract uses
`self.balance`. The in-flight grace window is an addition the brief did not specify, made because the plain
formula lets the governor take users' withdrawals during the moments they are in transit.

### 9.2 Payout transfers are skipped on Studio Next (observed, unresolved)

All payout transactions finalized and the contract debited its ledger, but **no funds reached the recipients**:
the contract keeps the value. The transaction's `message_value_effects` shows each transfer as
`skipped: true, skippedRefunded: true, declaredBudget: 0`, with the value returned to the contract. I reproduced
it with a minimal throwaway contract that does nothing but `gl.chain.Account(x).emit_transfer(v, on="finalized")`,
so it is not specific to PatentPrior. `on="accepted"` errors outright, and the balance-funded variant
(`use_balance=True`) also errors (it needs a permission I could not find how to grant). The `rescue_excess`
transfer in §8 also did not reduce the balance.

* On this network the pull-pattern **credits are correct but not redeemable**: about 0.8 GEN (v1) and 0.8 GEN
  (v2) of testnet funds are stranded in the two deployed contracts.
* The contract cannot detect a skipped transfer, so `pull_withdraw` still decrements the ledger.
* `contract_balance = A + L + C + V` is not demonstrated live. The ledger identity (I7) is.

### 9.3 Other trust assumptions

* **LLM judgement.** The model extracts the date and judges anticipation/enablement. Equivalence consensus
  checks that validators reach the *same outcome and date*, not that the reasoning is legally correct.
* **Sources.** Only the first 14,000 characters of a page's visible text are read. An arXiv `abs` page carries
  the abstract only, so full-text anticipation from the abstract alone is a high bar (v2 folds `/pdf/` links
  into the `abs` page rather than fetching binary PDFs). JavaScript-rendered pages are not read.
* **Date integrity.** The publication date comes from the page's own text; the date test is only as good as
  the cited source.
* **Self-reported priority.** The contract does not verify `priority_date` against any patent office record,
  nor that the registrant owns the claim.
* **Sybil and spoiling.** A defender can fund their own bounty. The inventor cannot challenge their own patent,
  but a Sybil identity can; I2 makes that cost 2% of the pool per attempt.
* **Not legal advice.** Outcomes are on-chain economic events with no legal effect.
* **Governor.** Can sweep the protocol vault (slashed shares), rescue excess (I5 caveat) and rotate the key.
  It cannot move bounties, locked bonds or credited balances.
* **Funder and queue caps.** 32 distinct funders and 16 pending challenges per patent bound the loops.
* **Lint.** `genvm-lint` reports 0 errors; the Pyright "unreachable code" hints in the IDE are not lint findings.
