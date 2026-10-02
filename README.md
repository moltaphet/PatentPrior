# PatentPrior

Autonomous patent prior-art adjudication and claim invalidation on [GenLayer](https://genlayer.com).
A pure smart-contract protocol: no frontend, no operator, no patent-office queue.

| | |
|---|---|
| Contract | [`contracts/patent_prior.py`](contracts/patent_prior.py) |
| Network | GenLayer Studio Next, chain `61997` |
| Live address | [`0xf5a0b81D82D86b1494E73A64097fDcd1530C1796`](https://explorer-studio-next.genlayer.com/address/0xf5a0b81D82D86b1494E73A64097fDcd1530C1796) |
| Tests | 138 tests, 2,047 executed assertions, all passing (direct mode) |
| Lint | `genvm-lint check contracts/patent_prior.py`: 0 errors |

> **Read §9 first if you intend to rely on payouts.** On Studio Next, the contract's outbound
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
        │       submit_prior_art (exactly 0.1 GEN bond, whitelisted https URL)
        │                     │
        │                     ▼
        │             challenge PENDING ───────────── void_stale_challenge (anyone, ≥ 7 days)
        │                     │                                    │
        │            evaluate_prior_art (anyone)                   ▼
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

If several challenges target one patent, the first to settle as `INVALIDATED` takes the bounty. The
rest are voided and refunded when evaluated, never slashed for losing a race.

## 3. The tribunal (evaluation)

`evaluate_prior_art` runs inside `gl.vm.run_nondet`. The **leader** fetches the cited page
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

Let `B = 10¹⁷` wei (0.1 GEN) be the fixed bond and `P` a patent's bounty pool, `P = Σᵢ cᵢ` over funders.

**Settlement of one challenge** (`c` = challenger, `d` = inventor/defender, `V` = vault):

| Outcome | Credit to `c` | Credit to `d` | Vault `V` | Pool `P` |
|---|---|---|---|---|
| `INVALIDATED` | `B + P` | 0 | 0 | `P → 0` |
| `VALID` | 0 | `⌊B/2⌋` | `B − ⌊B/2⌋` | unchanged |
| `AMBIGUOUS_VOID` / stale | `B` | 0 | 0 | unchanged |

In every row the credits sum to `B` plus whatever leaves the pool, so a settlement moves value between
buckets and never creates or destroys it. Because `B` is even, the slash splits exactly 50/50; the
`B − ⌊B/2⌋` form would route an odd wei to the vault.

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

Each method is a transfer between buckets (`A→C`, `L→C`, `L→V`, `V→C`, `A→C`) or an external flow that
moves exactly one of `D`, `W` together with exactly one bucket by the same amount:

| Method | Effect |
|---|---|
| `register_patent`, `fund_bounty` | `D += v`, `A += v` |
| `submit_prior_art` | `D += B`, `L += B` |
| settlement / stale void | `L −= B` ; `C` or `V` or `A` change by the same total |
| `expire_patent` | `A −= P`, `C += P` |
| `sweep_vault` | `V −= x`, `C += x` |
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
| `register_patent(title, priority_date, claim_text)` | payable | attached value is the initial bounty (0, or ≥ 0.001 GEN). `priority_date` is `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SSZ`, ≥ 1790, not in the future |
| `fund_bounty(patent_id)` | payable | ≥ 0.001 GEN; at most 32 distinct funders per patent |
| `submit_prior_art(patent_id, url, claimed_pub_date)` | payable | value must be **exactly** 0.1 GEN; inventor cannot challenge own patent; duplicate (patent, source) refused |
| `evaluate_prior_art(challenge_id)` | write | permissionless, once |
| `void_stale_challenge(challenge_id)` | write | permissionless, `PENDING` for ≥ 7 days |
| `expire_patent(patent_id)` | write | inventor only, no open challenge; refunds each funder what they put in |
| `pull_withdraw()` | write | pays out the caller's credits |
| `sweep_vault(to, amount)`, `transfer_governor(addr)` | write | governor only (the deployer) |
| views | | `get_patent`, `get_challenge`, `get_verdict`, `get_ledger`, `claimable_of`, `contribution_of`, `check_url`, `get_constants`, `get_governor`, `get_patent_count`, `get_challenge_count`, `whoami` |

**URL policy** (`_normalize_url`): `https` only; no userinfo; port absent or `443`; ASCII only, no
whitespace/control/backslash; no IP literals or numeric hosts; host labels must be well-formed; the host
must equal, or be a whole-label subdomain of, a whitelisted domain (`arxiv.org`, `patents.google.com`,
`patentscope.wipo.int`, `worldwide.espacenet.com`, `ieeexplore.ieee.org`, `dl.acm.org`, `doi.org`,
`datatracker.ietf.org`, `rfc-editor.org`, `w3.org`, `web.archive.org`, `nature.com`, `sciencedirect.com`,
`springer.com`, `biorxiv.org`, `hal.science`, `semanticscholar.org`, `openreview.net`, `eprint.iacr.org`).
`arxiv.org.evil.com`, `evilarxiv.org`, `evil.com@arxiv.org`, punycode and Cyrillic look-alikes are all refused.

## 7. Tests

```bash
uv venv --python 3.12
uv pip install --prerelease=allow genlayer-test==0.30.0rc2 genlayer-py==0.19.0rc2 genvm-linter==0.11.1rc2 pytest==9.1.1 web3==8.0.0 eth-account==0.14.0
.venv/bin/python -m pytest tests -q         # 138 passed; prints the executed-assertion count
.venv/bin/genvm-lint check contracts/patent_prior.py
```

| File | Covers |
|---|---|
| `test_urls.py` | 101 hostile and valid URLs (22 accepted, 79 refused): schemes, userinfo, ports, spoofed domains, private/IP hosts, unicode, control characters, length boundary |
| `test_registration.py` | input validation, dates, bounty floor, funder cap, expiry refunds, bond exactness, duplicate rules, self-challenge |
| `test_adjudication.py` | invalidation, post-dated prior art, partial disclosure, non-enabling disclosure, 18-case date boundary table, 3-criteria truth table, 11 unreachable-HTTP statuses, ambiguous findings, sloppy/garbage model output, hostile source text, moot sibling challenges |
| `test_economics.py` | 50/50 slashing arithmetic, stale-void boundary (`7d − 1s` refused, `7d` accepted), pull-withdraw, governor |
| `test_invariants.py` | scripted multi-patent/multi-challenger scenario and a seeded 90-step random walk against an independent shadow ledger |

The run reports **1,851 `assert` statements plus 196 reverts verified by `expect_revert` = 2,047**
executed assertions (counted at runtime by a pytest hook in `tests/conftest.py`).

I also ran mutation checks: seven deliberate bugs (wrong slash fraction, `<=` instead of `<` in the date
test, challenger forgoing the bounty, stale window off by one, self-challenge allowed, void slashing the
bond, withdraw not updating the ledger). **All seven were caught** by the suite.

**What direct mode cannot show.** It runs the leader only, so validator agreement is exercised only by the
live run in §8. It also does not credit `msg.value` to the contract balance, so the *balance* relation
cannot be asserted in direct mode; the *ledger identity* is.

## 8. Live deployment on Studio Next (chain 61997)

Reproduce with `python scripts/deploy.py` then `python scripts/interact_live.py` (resumable). Metadata is in
[`deployments/studio-next.json`](deployments/studio-next.json), including every transaction's validator
votes, per-validator `contract_state_hash`, `validator_votes_hash` list and a consensus digest.

| | |
|---|---|
| Contract | `0xf5a0b81D82D86b1494E73A64097fDcd1530C1796` |
| Deploy tx | [`0x28d2dc77…1b34`](https://explorer-studio-next.genlayer.com/tx/0x28d2dc77741eaff19fcc9808a56fd778e4d97ef98e7e8eae7b8383395ca71b34) |
| Source SHA-256 | `5b7bc8ddb55c4bb304ba79b62f73c961f8b8c8a2b5d2c4ec93e8d4c16493c089` |
| Runner | `py-genlayer:5jycge4q8k23462jtb0b9fyey1s9qz928sz2nbrd9mg4sxqg2qng` |
| Deployed | 2026-10-02T14:41:02Z by `0x0E54CdFAc8F3586C27650d8f6F55d25e0248E446` (a fresh key funded by `sim_fundAccount`) |

| Case | Outcome observed | Register | Submit | Evaluate | What happened |
|---|---|---|---|---|---|
| **1** software, priority 2020-03-01 | **INVALIDATED** (`UPHELD`) | [`0xbad5e774…8636`](https://explorer-studio-next.genlayer.com/tx/0xbad5e774b0161b0847767d87b0a0fafbbbca7d96bb94cea355201d95323c8636) | [`0x049ced07…6c03`](https://explorer-studio-next.genlayer.com/tx/0x049ced07b429821957f6a10a0a60f978c62fbf0b490b62636840134ba2156c03) | [`0x1788c91f…a737`](https://explorer-studio-next.genlayer.com/tx/0x1788c91f91eb7f6bd66a83c0f0963e3395ce966f2144b549fba85e7baa38a737) | BERT (arXiv:1810.04805), published 2018-10-11, confidence 99, all 3 criteria met. Challenger credited 0.6 GEN (0.1 bond + 0.5 bounty). |
| **2** hardware, priority 2015-01-15 | **VALID** (`REJECTED`) | [`0x8ff598bc…5a42`](https://explorer-studio-next.genlayer.com/tx/0x8ff598bcd7dade386ad824efdc0b0aa197da13ae64fd2c226a30e928c85f4a42) | [`0xd78c55e9…3622`](https://explorer-studio-next.genlayer.com/tx/0xd78c55e9c83f53fe0fc9f1323870aa65d48afc356ca0c594faf521907cab3622) | [`0x7cf3a0b5…76bc`](https://explorer-studio-next.genlayer.com/tx/0x7cf3a0b50ce5e10c956093a264ef81b455498a946954a5996bd7fc5a5d5f76bc) | TPU paper (arXiv:1704.04760), published 2017-04-16, **after** priority: temporal criterion false. The model additionally found the abstract lacks several claim elements. Bond slashed 0.05 / 0.05. |
| **3** dead link | **AMBIGUOUS_VOID** (`VOIDED`) | [`0x3e462865…47f1`](https://explorer-studio-next.genlayer.com/tx/0x3e4628658038dc13d9e46e1c6d00c7510c84e5be79f4dfd88d2e4deef9f847f1) | [`0x647d9ed2…8cec`](https://explorer-studio-next.genlayer.com/tx/0x647d9ed249e54f443760b14f9dfe3d87f27804c30208d10d8d92e2bf649f8cec) | [`0xf9c0f96b…a483`](https://explorer-studio-next.genlayer.com/tx/0xf9c0f96bd3d79b37ee14adabd141a87ba19fa8fc043f100fd2713f1dd719a483) | arXiv returned 404; no LLM call was needed. Full 0.1 GEN refund credited, no slash. |
| **4** open for challenges | **ACTIVE** | [`0xe34b980e…8648`](https://explorer-studio-next.genlayer.com/tx/0xe34b980e77be937670f3a4749c2fc781d637c463f1b4902823e81e35f6788648) | n/a | `fund_bounty` [`0xb292a03e…7904`](https://explorer-studio-next.genlayer.com/tx/0xb292a03eaaad2fb6d714d6420e943b470ac65e63be541d2886029f737a5e7904) | Bounty 0.5 + 0.25 GEN community-funded, no challenge yet. |

Case 2's outcome was the *expected* one but the model also found a second, independent reason to reject;
the stored verdict records both (`temporal_priority=false`, `full_anticipation=false`).

Settlement transactions (all `MAJORITY_AGREE`): `pull_withdraw` by challenger 1
[`0x1cc66acd…a1d2`](https://explorer-studio-next.genlayer.com/tx/0x1cc66acd2d8afc88c5284cc15f34c33a6066e568dd5afd0125f654a2e694a1d2),
inventor 2 [`0x39a9fdce…e21d`](https://explorer-studio-next.genlayer.com/tx/0x39a9fdceb67f82726c495125d8961db414fda8b1eba21a1184e29f8087a6e21d),
challenger 3 [`0x27f01bf1…3f95`](https://explorer-studio-next.genlayer.com/tx/0x27f01bf1ab2beecec61c7784666d6295f74492f4cb0a5a0a90a502035ed53f95);
`sweep_vault` [`0xbac47c36…012e`](https://explorer-studio-next.genlayer.com/tx/0xbac47c36f520ded6bf8b2808c24599e892b55b75bd6b2e7fdb6ac85e6f70012e)
and governor `pull_withdraw` [`0x3aaee728…4a0b`](https://explorer-studio-next.genlayer.com/tx/0x3aaee72848ec581269d7a14467f204216f116710a62866422010b801e8854a0b).

Every recorded transaction, including each evaluation, was decided `MAJORITY_AGREE` with five validators
voting three `agree` and two `idle`. I did not investigate why two validators vote `idle` in every round;
I am reporting it as observed, not explaining it. Final live ledger:
`D = 1.85 GEN`, `W = 0.80 GEN`, `A + L + C + V = 1.05 GEN`, so **the ledger identity holds on-chain**.

## 9. Known limitations and trust assumptions

**Payout transfers are skipped on Studio Next (observed, unresolved).**
All five payout transactions finalized and the contract debited its ledger, but **no funds reached the
recipients**: the contract still holds 1.85 GEN against 1.05 GEN of tracked liabilities, and the
recipients' balances did not move. The transaction's `message_value_effects` shows each transfer as
`skipped: true, skippedRefunded: true, declaredBudget: 0`, with the value returned to the contract.
I reproduced this with a minimal throwaway contract that does nothing but
`gl.chain.Account(x).emit_transfer(v, on="finalized")`, so it is not specific to PatentPrior's logic.
`on="accepted"` errors outright, and the balance-funded variant (`use_balance=True`) also errors, which
I did not pursue further (it needs a permission I could not find how to grant). Consequences:

* On this network the pull-pattern **credits are correct but not currently redeemable**; about 0.8 GEN of
  testnet funds are stranded in the deployed contract.
* The contract cannot detect a skipped transfer, so `pull_withdraw` still decrements the ledger. It is
  therefore unsafe to treat Studio Next as a payout-capable network until this is understood.
* The relation `contract_balance = A + L + C + V` is not demonstrated live. The *ledger identity* is.
* A recovery path (re-crediting from `balance − tracked`) would add a governor power I could not test
  adequately, so I deliberately did not ship one.

**Other trust assumptions**

* **LLM judgement.** The model extracts the date and judges anticipation/enablement. Equivalence consensus
  checks that validators reach the *same outcome and date*, not that the reasoning is legally correct.
  Appeals use GenLayer's standard mechanism.
* **Sources.** Only the first 14,000 characters of a page's visible text are read. Direct PDF links are
  binary and unusable (an arXiv `abs` page carries the abstract only, so full-text anticipation from the
  abstract alone is a high bar). `ar5iv` was unreachable when tested. Pages that render content with
  JavaScript are not read. A whitelisted host that serves user-generated content (for example
  `web.archive.org`) can still host forged material.
* **Date integrity.** The publication date comes from the page's own text. A page can lie, and archived
  copies can post-date the original publication. The date test is only as good as the cited source.
* **Self-reported priority.** The contract does not verify that the registered `priority_date` matches any
  patent office record, nor that the inventor owns the claim. A registrant can pick a hostile priority date
  for a rival's claim text; the design assumes registration is by the claim's holder.
* **Sybil and spoiling.** A defender can fund their own bounty, so a bounty alone does not signal a
  credible patent. Challengers cannot be the inventor, but a Sybil identity can.
* **Not legal advice.** Outcomes are on-chain economic events, not determinations by any patent office
  or court, and have no legal effect.
* **Governor.** The deployer can sweep the protocol vault (slashed bond shares only) and rotate the key.
  It cannot touch bounties, bonds or user credits.
* **Pre-1970 priority dates** are accepted (≥ 1790); the contract stores dates as strings and compares
  calendar days, so there is no timestamp underflow.
* **Funder cap.** At most 32 distinct funders per patent bounds the expiry refund loop.
* **Lint.** `genvm-lint` reports 0 errors; two Pyright "unreachable code" hints in the IDE are not lint
  findings.
