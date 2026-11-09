# ads-measurement-integration-kit

**The upstream half of ad measurement: a browser pixel stream and a server conversion-API stream describe the *same* conversions — this kit reconciles them into one true, consent-respecting count, and can explain every number in between.**

Explaining to an advertiser why their numbers don't match is the Solutions Engineer job — and this is the GA4/GTM problem the author works on in his day job. The advertiser's dashboard says one number, the ad platform says another, a raw sum of both event streams says a third, and all three are wrong for different, specific reasons: both-stream conversions counted twice, retried deliveries counted again, server events that lost their shared id, and conversions from people who never consented being counted at all. This project rebuilds that reconciliation layer as code a reviewer can run, test, and argue with: a deterministic synthetic dataset for a fictional advertiser (**Helios Home**) with a known ground truth, a four-stage dedupe/merge pipeline where every raw event gets a reason code, deterministic-only identity stitching, a fail-closed consent gate, and one headline table whose every gap is explained by counts — graded against the truth, not against the streams' own claims.

> **Synthetic data only.** Every event, conversion, and consent record is fabricated by a seeded generator. No real analytics, customer, or revenue data is used anywhere in this project.

Stdlib-only Python. Zero network, zero API keys, zero external dependencies.

**Not attribution.** The sibling project `attribution-pipeline` starts *after* events arrive cleanly and argues about which channel deserves credit. This kit is the layer before that: whether an event should exist in the count at all. Different problem, different failure modes, different conversation with the advertiser.

## The headline: one reconciliation table (seed 42, 240 true conversions)

| Count | Value | What it means |
|---|---|---|
| Naive (pixel rows + server rows, summed raw) | **446** (223 + 223) | The panic number: both-stream conversions counted twice, retried deliveries counted again |
| Deduped | **240** | Distinct conversions actually observed, after duplicates drop and cross-stream pairs merge |
| Consent-gated | **213** | What may legally/ethically be counted, after non-consented conversions drop |
| Ground truth | **240** (213 consented) | What actually happened — the generator's answer key, used only to grade |

The gaps are the deliverable, and every one is explained by reason-code counts:

- **446 → 240 (gap 206):** 34 duplicate deliveries dropped + 152 exact-id merges + 20 fallback merges. Nothing unexplained: `34 + 152 + 20 = 206`.
- **240 → 213 (gap 27):** exactly the 27 non-consented conversions in ground truth, each dropped with reason `DROPPED_NO_CONSENT` and tallied — never silently removed, never counted.
- **Deduped vs truth:** 0 missed, 0 phantom. **Consent-gated vs consented truth:** error 0. Total true revenue: **$50,325.00**.

Diagnostics (in `report`, per-channel coverage is just one column — see below): **match rate 1.0** and **duplicate rate 0.8583**, both measured *against ground truth* (206 excess raw rows per 240 true conversions), never self-reported by the streams.

## Quickstart

```bash
# 1. Generate the synthetic Helios Home dataset (240 conversions, seed 42)
#    -> dataset/ holding ground_truth / pixel / server / identifies / consent
python3 cli.py generate --count 240 --seed 42 --out-dir dataset

# 2. Reconcile the two streams (unified conversions + per-event audit)
python3 cli.py reconcile --input dataset --limit 5

# 3. Headline reconciliation table + diagnostics
python3 cli.py report --input dataset

# 4. CI gate: exit 0 = pass, 1 = reconciliation-error / match-rate breach,
#    2 = data-quality / IO error
python3 cli.py check --input dataset --max-reconciliation-error 0 --min-match-rate 1.0

# 5. One-command end-to-end demo (generate -> reconcile -> gate -> report -> serve)
python3 demo.py

# 6. Tiny localhost API: POST /reconcile, GET /report, GET /health on 127.0.0.1
python3 cli.py serve --port 8765 --input dataset
```

## How reconciliation works

Four stages, in order. Every raw event ends with exactly one audit record (`disposition` + `reason_code`); every unified conversion records how it was formed.

| Stage | Rule | Reason code |
|---|---|---|
| 1. Duplicate deliveries | Same stream, same `event_id` again = a retried delivery: kept once. An id-less event's duplicate is recognised as a byte-identical row (same identity, timestamp, channel, revenue) | `DUPLICATE_DELIVERY` |
| 2. Exact-id merge | A pixel event and a server event sharing a non-null `event_id` are the same conversion — the event_id dedupe pattern pixel + conversion-API integrations use. The shared id *is* the link; identity is not needed, and timestamp skew is irrelevant | `MERGED_EXACT_ID` |
| 3. Fallback merge | A server event with **no** `event_id` merges with an unmatched pixel event only when *all three* agree: same resolved identity key, timestamp within **900 s** (the window exists because the server stream runs 2–8 minutes behind the pixel stream), same revenue and channel. Greedy by smallest gap, one-to-one. Any condition fails → no merge; a near miss stays two conversions rather than a guessed one | `MERGED_FALLBACK_WINDOW` |
| 4. Singletons | Seen by one stream only — the other stream's event was lost or blocked. Real conversions; they count, once | `KEPT_PIXEL_ONLY` / `KEPT_SERVER_ONLY` |
| Consent gate (separate stage, on purpose) | Dedupe answers "how many distinct conversions were observed?"; gating answers "how many may we count?". No consent — or *no consent record at all* (fail-closed) — drops the conversion, tallied | `DROPPED_NO_CONSENT` |

**Identity stitching is deterministic only — a deliberate privacy choice.** An anonymous id resolves to a user id *only* when an explicit login/identify event links them. There is no probabilistic matching anywhere in this kit: no "probably the same person" from device, timing, or behaviour. The cost is real and measured — an id-less server event for a person who never identified *cannot* fallback-merge (eval E8 pins this), and stays a singleton. The kit takes that cost openly instead of guessing at identity.

**The generator plants the defects the reconciler must survive:** both-stream conversions (70%), pixel-only (server event lost, 15%), server-only (pixel blocked, 15%), duplicate deliveries (~12% of emitted events), server timestamp skew (+2–8 min), missing server ids (20% of both-stream conversions, planted only on identified people — identity-less fallback is impossible by construction), and non-consented users' events present in the raw streams (the raw feed does not gate; gating is the reconciler's job). Per-person conversions are spaced ≥ 2 h so the fallback window can never confuse two conversions of one person.

**Diagnostics, honestly framed.** Per-channel rows carry ground truth, pixel rows, server rows, deduped, consent-gated — and coverage as *one column among them*: dual-stream coverage, the share of the channel's true conversions both streams observed (paid_search lowest at 0.6176, paid_social highest at 0.8049 on the seeded book). The rest of each channel's truth arrived via a single stream — exactly what a one-stream outage would lose. Coverage taken from raw rows would be gameable by duplicates, so it is computed from the unified conversions' source lists.

## Architecture

| Module | Job |
|---|---|
| `generator.py` | Deterministic synthetic Helios Home dataset: ground truth + pixel/server streams derived from it with planted defects, identify events, consent records; JSONL / CSV serialisers + loaders with explicit numeric / boolean / null coercion |
| `reconcile.py` | The four-stage pipeline (duplicate deliveries → exact-id merge → time-window + identity fallback → singletons), deterministic identity map, fail-closed consent gate; one audit record per raw event |
| `report.py` | Headline reconciliation table with gap arithmetic, reason-code tallies, diagnostics (match / duplicate rates vs ground truth, per-channel table with coverage as one column). The reconciler itself never reads ground truth — truth enters only here, to grade |
| `validation.py` | Data-quality gate: required fields, channel vocabulary, timestamp parsing, revenue ≥ 0, duplicate truth/consent ids, source-matches-stream, events carrying no identity — reports issues, never raises on bad data |
| `cli.py` | `generate` / `reconcile` / `report` / `check` (CI exit 0/1/2) / `demo` / `serve` (stdlib `http.server` on 127.0.0.1: `POST /reconcile`, `GET /report`, `GET /health`); datasets are directories of JSONL or CSV tables |
| `demo.py` | End-to-end narrative with asserts: determinism, gap arithmetic, consent-gate exactness vs truth, CLI exit codes, live localhost serve round-trip where the served report matches the local report exactly |
| `fixtures/` | 60-conversion sample dataset (seed 42) in both JSONL and CSV: 60 true conversions, 55 pixel rows, 56 server rows, naive 111 → gated 54, $13,767.00 revenue |

## Evals

`python3 evals/run_evals.py` → **13/13 golden evals**, report committed at `evals/eval_report.json` (md5 `69b0e69e74f8b3bfeedb35acdf807dc0`, byte-identical across 3 verified runs):

- **E1** generator determinism — seed 42 ground-truth JSONL bytes pinned by md5
- **E2** headline pinned (naive 446, deduped 240, gated 213, truth 240/213, $50,325.00)
- **E3** gaps explained exactly (206 = 34 + 152 + 20; 27 no-consent; 0 missed, 0 phantom)
- **E4** per-event reason counts pinned (304 exact-merge events, 40 fallback, 33 pixel-only, 35 server-only, 34 duplicates, 27 no-consent)
- **E5** match rate 1.0 / duplicate rate 0.8583 vs truth; dual-stream coverage pinned per channel
- **E6** fallback window pinned at the edge (900 s merges, 901 s does not)
- **E7** consent gate exact (27 dropped == non-consented truth, zero leaks into counted)
- **E8** deterministic stitching pinned (no identify event → no fallback merge; exact id still merges)
- **E9** validation flags planted defects (bad channel, negative revenue, duplicate conversion id, non-boolean consent); generated dataset validates clean
- **E10** CLI `check` exit codes 0 / 1 / 2
- **E11** serve round-trip matches local reconcile and report exactly
- **E12** bundled fixture pinned (60 conversions, naive 111, gated 54, validates clean, CSV loads == JSONL loads)
- **E13** audit completeness (one reasoned record per raw event) + report determinism

Unit tests: `python3 -m unittest discover -s tests` → **84 hermetic tests**, including a no-network enforcement test (sockets stubbed), generator defect checks (skew direction, fallback only on identified people, non-consented events present in raw streams), every reconcile stage and its negative cases (window edge, revenue/channel mismatch, one-to-one greedy pairing), consent fail-closed behaviour, report arithmetic, data-quality validation, CLI exit codes, and serve round-trips.

## Development loop — what the checks actually caught

1. **Id-less duplicates became phantom conversions.** The first dedupe keyed only on `event_id` — so when a *duplicate delivery of an id-less server event* arrived, there was no id to dedupe on, both copies survived, one fallback-merged and the other stayed as a phantom singleton: deduped came out 241 against a truth of 240 on seed 42 (and off by 3 on another seed). The reconciliation-vs-truth check caught what per-stage tests had missed. Fix: id-less duplicates are recognised as byte-identical rows (identity + timestamp + channel + revenue), which two genuinely distinct conversions in this dataset can never be. Pinned by a unit test and by exact reconciliation across multiple seeds.
2. **The first coverage definition was a metric that could not fail.** Defined as "truth ids seen in either raw stream", coverage was 1.0 for every channel by construction — the generator guarantees at least one observation per conversion, so the column carried no information. Redefined as dual-stream coverage computed from unified source lists (range 0.6176–0.8049 across channels), which measures something an engineer would actually act on. A diagnostics number that cannot move is decoration, not diagnostics.
3. **Fallback has a precondition the generator must respect.** A missing-id server event can only fallback-merge if the person is identified — otherwise the pixel's anonymous key and the server's user key can never agree under deterministic stitching. The generator therefore plants missing ids only on identified people, and E8 pins the unidentified case as *unmergeable by design*. Documenting that constraint beat quietly letting the generator produce impossible-to-recover cases and grading the reconciler down for them.

## Scope and honest limits

- **Recovery is exact on the seeded book — because the seeded book is kind.** Every true conversion is observed by at least one stream, and per-person spacing makes fallback pairing unambiguous. Real feeds have conversions *neither* stream sees (deduped would undercount truth, measurably only with a truth source) and denser timing, where a fallback window can pair the wrong events. The kit's defences are its rules (all three fallback conditions, one-to-one greedy pairing) and its honesty: missed/phantom are reported whenever a truth table is supplied.
- **In production there is no ground-truth column.** The truth table is the eval harness's answer key. The table's first three rows — naive, deduped, consent-gated, with explained gaps — are what the kit produces from the streams alone; the truth row exists so the demo can *prove* the pipeline works instead of asserting it.
- **No parity with any real platform is claimed.** This is an independent implementation of the event_id dedupe pattern pixel + conversion-API integrations use, on synthetic data. No vendor API is called, mimicked, or required.
- **Consent is a per-person boolean here.** Real consent is per-purpose, timestamped, and revocable; the fail-closed rule (no record → not counted) is the part that generalises.
- **The API is a demo surface.** It binds 127.0.0.1 only, holds its dataset in memory, and resets on restart.

## Why this project

It takes the author's day-job craft — GA4/GTM measurement, the pixel + server-side event plumbing underneath every advertiser number — and rebuilds it as a solutions-engineering artifact: a deterministic generator with planted defects and a known truth, a reconciliation pipeline whose every decision carries a reason code, measured quality against that truth, a CI check command, and a small API. This build is JD-backed (a 2026-11-09 Solutions Engineer, Ads Solutions posting asking for exactly this measurement-integration work). The discipline is the point: make the logic inspectable, make the outputs reproducible, and make the failure modes exit codes — because the job is standing in front of an advertiser and explaining, number by number, why the counts disagreed and why the final one is right.
