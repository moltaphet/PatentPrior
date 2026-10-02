# PatentPrior

Autonomous patent prior-art adjudication and claim invalidation on [GenLayer](https://genlayer.com).
A pure smart-contract protocol: no frontend, no operator, no patent-office queue.

| | |
|---|---|
| Contract | [`contracts/patent_prior.py`](contracts/patent_prior.py) |
| Network | GenLayer Studio Next, chain `61997` |
| Live address (v2.1, hardened) | [`0xB1B8Db4679753Da25ae92f6fdEaE8fa231ACe8AD`](https://explorer-studio-next.genlayer.com/address/0xB1B8Db4679753Da25ae92f6fdEaE8fa231ACe8AD) |
| Tests | 193 tests, 1,966 executed assertions, all passing (direct mode) |
| Lint | `genvm-lint check contracts/patent_prior.py`: 0 errors |

> **Read §9 first if you intend to rely on payouts.** (v2 and v2.1 hardening are described in §9.1.) On Studio Next, the contract's outbound
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
        ┌────────────────  ACTIVE  ◄──── fund_bounty (anyone, ≥ 0.05 GEN, only while nothing is pending)
        │                     │
        │       submit_prior_art (bond ≥ max(0.1 GEN, 2% of pool), whitelisted https URL, canonicalised)
        │                     │
        │                     ▼
        │             challenge PENDING ───────────── void_stale_challenge (anyone, ≥ 7 d; ≥ 24 h if blocking)
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
transaction**; a rejection or void simply hands the queue to the next. A challenge that is the head of its queue **with others waiting behind it** can be stale-voided by anyone
after **24 hours** (7 days otherwise), so one unresolvable challenge cannot stall a patent's queue for long. At most 16 challenges can be pending per patent.

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

`rescue_excess` operates on the difference `E = contract_balance − (A + L + C + V)`; it moves only `E`,
to the governor, (when `E > 0` and no payout was emitted in the last 24 h) and writes none of `A, L, C, V, D, W`, so the
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
| `rescue_excess()` | none of `A, L, C, V, D, W`; `contract_balance −= E`, paid to the governor |
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
| `fund_bounty(patent_id)` | payable | ≥ **0.05 GEN** (dust cannot exhaust the 32 funder slots); at most 32 distinct funders per patent; **reverts `ERR_CHALLENGE_IN_PROGRESS` while any challenge is pending**, so a bond is always priced against the whole pool it risks |
| `submit_prior_art(patent_id, url, claimed_pub_date)` | payable | value must be **≥ `max(0.1 GEN, 2% of the pool)`** (`required_bond(patent_id)`); inventor cannot challenge own patent; duplicate canonical (patent, source) refused; ≤ 16 pending per patent |
| `evaluate_prior_art(challenge_id)` | write | permissionless, once, **FIFO per patent** (`next_evaluable(patent_id)`) |
| `void_stale_challenge(challenge_id)` | write | permissionless, `PENDING` for ≥ 7 days, or ≥ 24 h if it is the queue head with others behind it |
| `expire_patent(patent_id)` | write | inventor only, no open challenge; refunds each funder what they put in |
| `pull_withdraw()` | write | pays out the caller's credits |
| `sweep_vault(to, amount)`, `rescue_excess()`, `transfer_governor(addr)` | write | governor only (the deployer) |
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

`_canonicalize_url` then yields one spelling per source: lower-case host **with any leading `www.` removed**, **no query, no fragment, no
trailing slash**, duplicate slashes collapsed, unreserved percent-escapes decoded (others upper-cased), dot
segments rejected, and for arXiv the version suffix, `/pdf/` and `/html/` spellings and the `export`
mirror all fold into `https://arxiv.org/abs/<id>`. That canonical string is what is stored, hashed
(`sha256(patent_id|canonical_url)`) for per-patent duplicate detection, and fetched.

## 7. Tests

```bash
uv venv --python 3.12
uv pip install --prerelease=allow genlayer-test==0.30.0rc2 genlayer-py==0.19.0rc2 genvm-linter==0.11.1rc2 pytest==9.1.1 web3==8.0.0 eth-account==0.14.0
.venv/bin/python -m pytest tests -q         # 193 passed; prints the executed-assertion count
.venv/bin/genvm-lint check contracts/patent_prior.py
```

| File | Covers |
|---|---|
| `test_urls.py` | 105 hostile and valid URLs (20 accepted, 85 refused): schemes, userinfo, ports, spoofed domains, purged hosts, private/IP hosts, dot segments, unicode, control characters, length boundary |
| `test_registration.py` | input validation, dates, bounty floor, funder cap, expiry refunds, bond exactness, duplicate rules, self-challenge |
| `test_adjudication.py` | invalidation, post-dated prior art, partial disclosure, non-enabling disclosure, 18-case date boundary table, 3-criteria truth table, 11 unreachable-HTTP statuses, ambiguous findings, sloppy/garbage model output, hostile source text, follower voiding after an invalidation |
| `test_economics.py` | 50/50 slashing arithmetic, stale-void boundary (`7d − 1s` refused, `7d` accepted), pull-withdraw, governor |
| `test_invariants.py` | scripted multi-patent/multi-challenger scenario and a seeded 120-step random walk against an independent shadow ledger that re-implements the FIFO queue and the proportional bond |
| `test_patent_prior.py` | the audit regressions: purged hosts, proportional bond, arXiv version/spelling and global `www.` canonicalisation, FIFO ordering and eager follower refunds, bounty freeze while challenges are pending, `rescue_excess` accounting / destination / 24 h guard, 24 h blocking-head voiding, dust backers |

The run reports **1,586 `assert` statements plus 380 reverts verified by `expect_revert` = 1,966**
executed assertions (counted at runtime by a pytest hook in `tests/conftest.py`). The harness does not execute
emitted transfers, so `tests/conftest.py::capture_transfers` hooks the VM's `EmitInternalMessage` call to record
each transfer's destination, amount and stage; that is how the tests assert where `rescue_excess` pays.

Mutation checks: the 7 v1 mutants, the 8 v2 mutants and **8 new v2.1 mutants** (bounty freeze removed, `www.`
kept, `www.` stripped anywhere in the host, rescue paying a different address, rescue ignoring the 24 h window,
blocking-stale window never applied / applied to non-head challenges / applied to a lone challenge) were each
caught. The v2.1 mutants were run against `test_patent_prior.py` alone, the v2 ones likewise, and the v1 ones
against the then-full suite; the older two groups were not re-run against v2.1.

**What direct mode cannot show.** It runs the leader only, so validator agreement is exercised only by the
live run in §8. It also does not credit `msg.value` to the contract balance, so the *balance* relation
cannot be asserted in direct mode; the *ledger identity* is.

## 8. Live deployment on Studio Next (chain 61997)

Reproduce with `python scripts/deploy.py` then `python scripts/interact_live.py` (resumable). Metadata is in
[`deployments/studio-next.json`](deployments/studio-next.json), including every transaction's validator
votes, per-validator `contract_state_hash`, `validator_votes_hash` list and a consensus digest. Earlier
deployment records are kept as `studio-next.v1.json` (`0xf5a0b81D…1796`) and `studio-next.v2.json`
(`0xC94c5774…D3e3`); neither has the v2.1 changes, and v1 also lacks the §9.1 v2 defenses.

| | |
|---|---|
| Contract (v2.1) | `0xB1B8Db4679753Da25ae92f6fdEaE8fa231ACe8AD` |
| Deploy tx | [`0xb14f741a…7d7a`](https://explorer-studio-next.genlayer.com/tx/0xb14f741afb2546ebcd3d9d7a9a65ca6584df59f97979b3a0837e90d875627d7a) |
| Source SHA-256 | `9f05dd0197f14abccdcce505815d51a4b52fd3012a0141aba14fba17e66f6583` |
| Runner | `py-genlayer:5jycge4q8k23462jtb0b9fyey1s9qz928sz2nbrd9mg4sxqg2qng` |
| Deployed | 2026-10-02T19:19:43Z by `0x0E54CdFAc8F3586C27650d8f6F55d25e0248E446` (a fresh key funded by `sim_fundAccount`) |

| Case | Outcome observed | Register | Submit | Evaluate | What happened |
|---|---|---|---|---|---|
| **1** software, priority 2020-03-01 | **INVALIDATED** (`UPHELD`) | [`0xb31f3734…9711`](https://explorer-studio-next.genlayer.com/tx/0xb31f3734935f16a165c9cce33632e99d393af8b8adf36b8a47bfebcdda729711) | [`0xc177b47b…f391`](https://explorer-studio-next.genlayer.com/tx/0xc177b47b653403ee70133280b5fea6bcadd4ab760e035285a43b0cf33923f391) | [`0x29f4c34b…5ad8`](https://explorer-studio-next.genlayer.com/tx/0x29f4c34b00962dd33ba4783ef2efdda06b9089cd47e77f81d0c5982e0b9c5ad8) | BERT (arXiv:1810.04805), published 2018-10-11, confidence 99, all 3 criteria met. Challenger credited 0.6 GEN (0.1 bond + 0.5 bounty). |
| **1b** FIFO follower on patent 1 | **VOIDED**, refunded | n/a | [`0x18561b24…43e3`](https://explorer-studio-next.genlayer.com/tx/0x18561b24aad38ccce4377a30667c9718398ea518ddb11cd3de2b65bcd48d43e3) | n/a (no evaluation needed) | Cited `arxiv.org/abs/1706.03762v7`, stored as `https://arxiv.org/abs/1706.03762` and queued behind #1. When #1 invalidated the patent, #2 was voided and 0.1 GEN credited back **in the same transaction**. |
| **2** hardware, priority 2015-01-15 | **VALID** (`REJECTED`) | [`0x72153806…e9ec`](https://explorer-studio-next.genlayer.com/tx/0x7215380666277cd1550bec3d215652c7ff7f07c25a9ceb1ac817affbac6ee9ec) | [`0x84648b49…a5c2`](https://explorer-studio-next.genlayer.com/tx/0x84648b49ce5a7fff0a85229a54e9908159535c1586be6888d44c8f3fc90ca5c2) | [`0x4cf24484…7ba5`](https://explorer-studio-next.genlayer.com/tx/0x4cf2448412db8290990a5af1988489ac0b84c8fe894f9491945dfaabc8507ba5) | TPU paper (arXiv:1704.04760), published 2017-04-16, **after** priority: temporal criterion false (confidence 95). Bond slashed 0.05 / 0.05. |
| **3** dead link | **AMBIGUOUS_VOID** (`VOIDED`) | [`0x4600976f…363e`](https://explorer-studio-next.genlayer.com/tx/0x4600976f509086a30b92079ef8fcc3acc3fa86257dfce1a2d15915516d12363e) | [`0x9ccb00f4…fa06`](https://explorer-studio-next.genlayer.com/tx/0x9ccb00f49db841357026b914a43aa155a4810ee63c2c21c98ddf760a2a64fa06) | [`0xcad881a9…1c6e`](https://explorer-studio-next.genlayer.com/tx/0xcad881a965c6e6acf17414f2ac6d1b2325b0790a189f60fe6d6e6d4731621c6e) | arXiv returned 404; no LLM call needed. Full 0.1 GEN refund credited, no slash. |
| **4** open for challenges | **ACTIVE** | [`0xd28b4079…66f1`](https://explorer-studio-next.genlayer.com/tx/0xd28b407903496cb57c8e2ec7916ab432018a76f436815b87e4c7746c230066f1) | n/a | `fund_bounty` [`0x1674ad88…3c88`](https://explorer-studio-next.genlayer.com/tx/0x1674ad8897c7e598fcbff368c417b794b947ab174d8294eaec8dc0dc49243c88) | Bounty 0.5 + 0.25 GEN community-funded, no challenge yet. |
| **5** `www.` probe | **INVALIDATED** | [`0x1f68ca4c…1ffa`](https://explorer-studio-next.genlayer.com/tx/0x1f68ca4cb912758a1521d7a7d73b2fc3ceaea6ad9a4d256bb83ea69cf0021ffa) | [`0xfa88a193…80f7`](https://explorer-studio-next.genlayer.com/tx/0xfa88a193a92342556fb9f42d240a7326d0788eacb1be8cad9013b0a29ab580f7) | [`0xf6ea5c9e…d6f7`](https://explorer-studio-next.genlayer.com/tx/0xf6ea5c9ed9119252ff36ba25479f3442da1e563a38f932cffbf3586f1872d6f7) | Cited `https://www.rfc-editor.org/rfc/rfc9000`; stored and fetched as `https://rfc-editor.org/rfc/rfc9000`. That bare host answers `301` back to `www.`; the challenge was **not** voided, so **the redirect was followed** and the page was read (extracted date `2021-12-31`, confidence 98). |

Case 5 is a probe, not a legal claim: its purpose was to learn whether stripping `www.` from hosts that
redirect the bare name back to `www.` (`w3.org`, `rfc-editor.org`, `nature.com`, `springer.com`,
`sciencedirect.com`, `biorxiv.org`, `doi.org` all do) would make citations unfetchable. It does not on this
network. Note the extracted date `2021-12-31` is the contract's conservative resolution of a year-only
date (§5), not a claim about the day RFC 9000 was published. Several of these publishers also block automated
clients (HTTP 403/418/429 from my own `curl`); that is independent of this change and would settle as
`AMBIGUOUS_VOID`.

`rescue_excess` live: the simulator's `sim_fundAccount` credited the contract 0.3 GEN of stray funds
(balance 2.15 → 2.45 GEN against 2.15 GEN tracked, after cases 1-5 and before any withdrawal). The governor called
`rescue_excess()` [`0x23253198…60b3`](https://explorer-studio-next.genlayer.com/tx/0x23253198f96c0310b70759c6594d44a458e4570d59a037da96190594da2560b3). The transaction emitted **exactly one message, to the governor
`0x0E54CdFAc8F3586C27650d8f6F55d25e0248E446` for the full 0.3 GEN** (`message_recipient_is_governor = True`) and left every tracked
bucket unchanged (`tracked_untouched = True`). The balance did not fall afterwards, consistent with the network
skipping the outbound transfer (§9.2).

Settlement transactions (all `MAJORITY_AGREE`): `pull_withdraw` by challenger 1 [`0xe502a8b0…b539`](https://explorer-studio-next.genlayer.com/tx/0xe502a8b054f0896a1ffa8869af02064cfe5503fab0ff805e2fabe41ad0d9b539),
inventor 2 [`0xe249582e…3dd4`](https://explorer-studio-next.genlayer.com/tx/0xe249582ed791edfd0af36375d728811663a58bbb849d99f983e0447c7c4d3dd4), challenger 3 [`0x09b28f40…31ae`](https://explorer-studio-next.genlayer.com/tx/0x09b28f40b75b35fcf801c64668c0a4d95f5061f76cf4b0a17a90690e984331ae);
`sweep_vault` [`0x3ce04d6c…2d38`](https://explorer-studio-next.genlayer.com/tx/0x3ce04d6ca018b3b9f78a8aca5d446a50aba213672d9002b6ebcfa526c08c2d38) and governor `pull_withdraw` [`0x91a191d7…b0b2`](https://explorer-studio-next.genlayer.com/tx/0x91a191d76afcfd0e3c9252366516c5495b4ed99f8244582682783f0f1eedb0b2).

Every recorded transaction was decided `MAJORITY_AGREE` with five validators voting three `agree` and two
`idle`; I did not investigate why two vote `idle`. Final live ledger: `D = 2.15 GEN`, `W = 0.80 GEN`,
`A + L + C + V = 1.35 GEN`, so **the ledger identity holds on-chain**. The contract balance is 2.45 GEN =
1.35 tracked + 0.80 skipped payouts + 0.30 rescued-but-undelivered stray funds (§9.2).

## 9. Security invariants and known limitations

### 9.1 Hardening history and the invariants it enforces

| # | Invariant | Mechanism | Limit |
|---|---|---|---|
| I1 | A challenger cannot plant its own "prior art" on a host it controls | Whitelist holds authoritative, immutable publishers only; `web.archive.org`, `openreview.net`, `hal.science`, `semanticscholar.org` purged (v2) | A whitelisted publisher is still trusted to serve what it published. `doi.org` redirects to the publisher. |
| I2 | Attacking a rich pool costs more than a poor one, **and the bond always matches the pool being risked** | `bond ≥ max(0.1 GEN, 2% of the pool)` (v2); **`fund_bounty` reverts `ERR_CHALLENGE_IN_PROGRESS` while any challenge is pending** (v2.1) | A deterrent, not a substitute for I1. While a challenge is pending nobody, including the inventor, can add to the pool; funders must wait for it to settle. |
| I3 | One paper, one citation per patent, however it is spelled | `_canonicalize_url`: lower-case host **minus any leading `www.`** (v2.1, all domains), no query/fragment/trailing slash, arXiv version / `/pdf/` / `/html/` / `export` folded; the canonical URL is hashed, stored and fetched | **Query-identified sources are unsupported** (queries are stripped; such a citation would void, never slash). Only a *leading* `www.` is stripped; `a.www.x.com` is left alone. |
| I4 | Challenges settle in submission order; an invalidation refunds everyone queued behind it, immediately; no challenge can stall a queue for long | Per-patent FIFO queue, `ERR_FIFO_ORDER`, eager follower voiding, 16-pending cap (v2); **a blocking head is stale-voidable after 24 h, not 7 days** (v2.1) | See "Head-of-line" below. |
| I5 | Surplus balance can be rescued without touching a single wei of tracked liability, **and only ever to the governor** | `rescue_excess()` (v2.1: **no recipient argument**): governor-only; `excess = balance − (A+L+C+V)`; `ERR_NO_EXCESS_BALANCE` at ≤ 0; nothing within 24 h of **any** `pull_withdraw` (`ERR_PAYOUTS_IN_FLIGHT`) | See "Rescue" below. |
| I6 | Dust cannot exhaust the 32 backer slots | `fund_bounty` and the initial bounty both require ≥ 0.05 GEN | A determined attacker can still pay 32 × 0.05 GEN to occupy the slots. |
| I7 | `D − W = A + L + C + V` after every method | `_assert_ledger()` on every write; shadow ledger in the tests | Not a statement about `contract_balance` (§9.2). |

**Rescue.** The destination is now `self.governor`, the same key that must call the method, so a caller can no
longer route funds to a third address. Two things this does *not* do, and the brief's wording suggested
otherwise: (1) `governor` is **not immutable**, because `transfer_governor` still exists, so the destination
is "the current governor", not a fixed treasury; I kept key rotation so a lost key is recoverable and did not
silently remove a feature. (2) `excess` cannot tell a stray donation from a payout the network *skipped*
(§9.2). After the 24 h window a skipped payout is indistinguishable from surplus, so the governor **could**
rescue users' stranded funds *to itself*. That is a governor-trust assumption; nothing in the contract returns
those funds to the credited users.

**Head-of-line.** The brief asked to void a challenge "if consecutive transient errors persist". The contract
cannot count them: a failed evaluation reverts, and a revert discards any counter written in it. So v2.1 uses
the other option the brief offered, a shorter timeout: a challenge that is the head of its queue **and** has
others pending behind it can be stale-voided by anyone after 24 h. With *n* queued challenges a fully wedged
queue clears in at most *n − 1* daily steps plus 7 days for the last one. A lone challenge keeps the 7-day
window. The cost is that a legitimate head can be voided (and refunded, never slashed) 24 h after it was
submitted if nobody evaluated it, which is why evaluation is permissionless.

Deviations from the brief across v2 and v2.1: `gl.get_balance()` does not exist in the SDK, so the contract uses
`self.balance`; the 24 h in-flight window on rescue is an addition made because the plain formula lets the
governor take payouts in transit.

### 9.2 Payout transfers are skipped on Studio Next (observed, unresolved)

All payout transactions finalized and the contract debited its ledger, but **no funds reached the recipients**:
the contract keeps the value. The transaction's `message_value_effects` shows each transfer as
`skipped: true, skippedRefunded: true, declaredBudget: 0`, with the value returned to the contract. I reproduced
it with a minimal throwaway contract that does nothing but `gl.chain.Account(x).emit_transfer(v, on="finalized")`,
so it is not specific to PatentPrior. `on="accepted"` errors outright, and the balance-funded variant
(`use_balance=True`) also errors (it needs a permission I could not find how to grant). The `rescue_excess`
transfer in §8 also did not reduce the balance, even though its message was addressed correctly.

* On this network the pull-pattern **credits are correct but not redeemable**: about 0.8 GEN of testnet
  funds are stranded in each of the three deployed contracts.
* The contract cannot detect a skipped transfer, so `pull_withdraw` still decrements the ledger.
* `contract_balance = A + L + C + V` is not demonstrated live. The ledger identity (I7) is.

### 9.3 Other trust assumptions

* **LLM judgement.** The model extracts the date and judges anticipation/enablement. Equivalence consensus
  checks that validators reach the *same outcome and date*, not that the reasoning is legally correct.
* **Sources.** Only the first 14,000 characters of a page's visible text are read. An arXiv `abs` page carries
  the abstract only. JavaScript-rendered pages are not read, and some publishers block automated clients.
* **Date integrity.** The publication date comes from the page's own text; the date test is only as good as
  the cited source. Year-only and month-only dates resolve to the last day of the period.
* **Self-reported priority.** The contract does not verify `priority_date` against any patent office record,
  nor that the registrant owns the claim.
* **Sybil and spoiling.** A defender can fund their own bounty (only while nothing is pending). The inventor
  cannot challenge their own patent, but a Sybil identity can; I2 makes that cost 2% of the pool per attempt.
* **Not legal advice.** Outcomes are on-chain economic events with no legal effect.
* **Governor.** Can sweep the protocol vault (slashed shares), rescue excess to itself (see Rescue) and rotate
  the key. It cannot move bounties, locked bonds or credited balances.
* **Funder and queue caps.** 32 distinct funders and 16 pending challenges per patent bound the loops.
* **Lint.** `genvm-lint` reports 0 errors; the Pyright "unreachable code" hints in the IDE are not lint findings.
