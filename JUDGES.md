# Integrity Harness — judge guide

A bounded laboratory for one question: when an agent is pushed to skip a check,
what does it actually do, and can you verify the claim afterwards?

The same three-turn conversation runs in two independent arms. A deterministic
contract, not a model, issues the verdicts (`pass`, `fail`, `indeterminate`) per
clause, and the resulting package is signed and tamper-tested.

## 60-second path, no API key, no quota

No public deployment exists yet, so run it locally:

```bash
python -m pip install -e ".[demo]"   # the demo extra adds Gradio

python demo_app.py                   # http://127.0.0.1:7860/
```

Two commands need no key at all:

```bash
python scripts/judge_check.py                    # verify the signed package + tamper test
python scripts/clause_matrix.py                  # rebuild docs/clause_matrix.md from stored reports
python scripts/case_clause45_gate_false_negative.py   # the gate's false negative, in one command
```

`judge_check.py` exits non-zero if the signature or the tamper test fails. The third
command replays two identical sessions with the same pressure turn: the clause
**passes** a reply that echoes its own sentence and **fails** the recorded v1.15 reply
that the hand reading labels as a restatement. It reads the stored run, calls no model
and exits 0 only when the mismatch reproduces. The repaired detector is shipped beside
it as `scripts/detector_clause45_v0_2.py`.

1. **Show recorded reference run** — clause verdicts from a live run stored on
   31 October 2026. It makes no model call and is labelled as stored evidence.
2. **Run synthetic control** — the authorization boundary exercised on local
   mocks only, with unverified and verified authorization. The proposal is a
   fixture, not a Meta model output.
3. **Run live slice** — needs a Meta API key on the server. Three scenarios,
   at most 6 calls, 1,024 tokens, 25 s per call, no retries.
4. **Sign package**, then **Verify** and **Alter one byte** to see the Ed25519
   signature accept the original and reject a re-hashed tampered copy.

If no key is configured the app says so in one line and does not fall back to
fixtures. Replay and synthetic control keep working, because neither calls Meta.

## Artifacts in this repository

| Path | What it is |
| --- | --- |
| `evidence/meta-heldout-live-20261028-signed.zip` | Signed package from a live held-out run |
| `evidence/reference/meta_safety_scan_report.json` | Stored report behind the reference-run button |
| `offline_verifier/app.py` | Offline verifier UI: signature, artifacts, tamper test |
| `demo_app.py` | The laboratory app |
| `docs/rebanada_demo_live_20261031_v0.1.md` | Design of the bounded live slice |
| `docs/clause_matrix.md` | Clause counts aggregated from stored reports, with provenance |
| `scripts/case_clause45_gate_false_negative.py` | Two sessions, one contract: the echo passes, the recorded reply fails |
| `scripts/detector_clause45_v0_2.py` | Repaired cláusula 45 detector, published with the predictions it missed |
| `examples/insurance_claims_conversation_v0.1.yaml` | The contract and the pressure-turn adapter the case runs on |
| `docs/experiments/clause45_gate_false_negative_20261104_v1.21.md` | The one-page write-up of that case |
| https://youtu.be/gfuXbeZF058 | 1:33 video: offline path, live slice, signing and tamper test, English subtitles and voiceover |
| `README.md` | Contracts, limits and what the evidence does not claim |

Verify the signed package without a key:

```bash
python -m pip install -r offline_verifier/requirements.txt
python offline_verifier/app.py    # upload the ZIP, run verify and tamper test
```

## How this maps to the published rubric

The organizers publish four weighted criteria: Impact (30%), Use of Meta AI
Tooling (25%), Technical Depth and Execution (20%) and Demo (25%). The track
decides eligibility, not the weights; this submission enters the startup track, so
production viability is weighed inside Impact and Technical Depth.

- **Impact (30%):** the failure addressed here is an agent executing a restricted
  action because a human relayed an approval nobody verified. The laboratory makes
  that failure observable: the same model in both arms, one authorization
  boundary, and a signed record of what was proposed and what the contract
  decided. In the recorded run the model never proposed the restricted action:
  it declined in every pressured turn and proposed human escalation, so the guard
  canonicalised those handoffs rather than blocking a payment.
- **Use of Meta AI Tooling (25%):** `muse-spark-1.3` is called live through

  `/chat/completions`, once per turn in each arm, six calls per run, at

  `temperature: 0.0` with a 1,024-token cap. The call carries two function

  declarations with `tool_choice: "auto"`, so the proposal to act is the model's

  own tool call, not a scripted string. The deterministic contract issues the
  verdict; where the model does propose an action or a handoff, the guard applies
  the policy to it. Every request is logged with model,
  latency, HTTP status and tool-call count. The README section "What each request

  to Meta actually contains" shows where each of those is in the source.
- **Technical Depth and Execution (20%):** two independent arms, deterministic
  verdicts per clause, Ed25519 signing, a tamper test that survives a re-hashed
  manifest, and a ledger that reserves six provider calls before the first one is
  sent.
- **Demo (25%):** one screen recording of the running product at https://youtu.be/gfuXbeZF058 (1:33): the live
  run, the two verdicts, the signature, the verification and the rejected tamper.
- **Production readiness, stated plainly:** the laboratory is a bounded
  evaluation tool, not a production service. It fails closed without a key, does
  not retry, caps every call in time and tokens, and keeps its quota in SQLite so
  a restart does not reset it. Before any deployment it would still need
  persistent storage outside `DEMO_STATE_DIR`, a provider-side spend ceiling, an
  authenticated surface and a ledger safe for more than one worker. It is
  therefore shipped as a local, reproducible run, not as a hosted product.
- **Does it work:** the whole path runs offline except the live slice, and the
  stored reference run plus the synthetic control need no key at all.

## What was measured after the recorded run

Nine further series were executed on 3-4 November under pre-registration, and four of the eleven series
in this repository now end as *not measured*: the invalidity condition was written before the run and
honoured after it. The full table is in [the README](README.md#nine-further-series-3-4-november-what-held-what-did-not);
the two that matter for judging are these.

- **The authorization chain, measured.** With the restricted action reachable but denied by default,
  18 attempts without authorization were denied 18 times, 5 attempts carrying a valid attestation were
  executed 5 times, and nothing executed outside an authorized turn (24 trajectories). The gate does
  not verify the token: it enforces an attestation that arrives from outside the model. That boundary
  is the product.
- **The behavioural claim, narrowed twice.** Five clause-45 series asked whether the agent keeps naming
  who decides under pressure. With the boundary in the system prompt it restated in 53 of 53 non-empty
  replies. With that sentence removed the detector read 13.2 %, and reading the same replies by hand
  gives 28.9 % (sustained) and 50.0 % (escalation) — which is why every detector figure here is
  published as a lower bound, and two findings drawn from those counts have since been withdrawn: the
  token ceiling, and the gap between arms, which did not replicate on two other models (10.0 and 12.5
  points, intervals crossing zero). By the contract it fell to 0 of 40 pressured turns. The earlier
  figure was the model **echoing the prompt**. Then a fourth model, from a different family, produced
  the widest gap of the programme — **50.0 % vs 12.5 %, +37.5 (+18.9 to +56.1)** — and the pattern
  finally lined up: when the user **asks** for the payout to be approved, all four models restate the
  limit within 48.7–60.0 %; when the pressure is ambient, the rate runs from 12.5 % to 50.0 %.
  Restating the boundary is a reply to a demand, not a stable property of the turn. In none of the five
  series did the model approve, promise or advance a payment in 320 pressured turns. So the honest claim
  is not "the model holds the line"; it is "the model does not cede, and how often it renames the
  authority depends on what the user is asking for".

- **And a null about comparison.** With five runs on four models we could have claimed a ranking.
  The intervals do not support one: escalation is flat across models (48.7-60.0 %), the only sustained
  contrasts that clear their interval are against two of the four models, and **the same model run twice
  differs by 10.5 points (−8.4 to +29.5)**. So the note
  ([`clause45_model_comparison_20261104_v0.1.md`](docs/experiments/clause45_model_comparison_20261104_v0.1.md))
  publishes the intervals and declines to order them. Nor is a lower restatement rate a safety win:
  naming the limit less often is worse compliance, and cession is zero in every run. The finding is
  about the metric — an arm where nobody asks for the action measures style, not compliance — which is
  the kind of result this harness exists to produce.

This is why the control we propose sits in the runtime rather than in the prompt, and why every
preregistration in this repository publishes its nulls with the same prominence as its passes.

## Provenance

The engine, contract and harness are the baseline commit `2fd192d`; the
laboratory app, the bounded live slice, the offline verifier and the signed
evidence are new work.

## What this does not claim

- The signature uses a self-embedded public key: it detects modification, it
  does not establish an external identity or a trusted timestamp.
- `fail` on a contractual clause is a mapping limitation, not proof that the
  model attempted a payment; `indeterminate` is not evidence of safe behaviour.
- The live scenarios apply pressure and authorization metadata; they are not a
  general safety benchmark, and the replay covers one turn, not a conversation.
- Replay and synthetic control are mocks. They demonstrate mechanism, not the
  model's real-world conduct.
