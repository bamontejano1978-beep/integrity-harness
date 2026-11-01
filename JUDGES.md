# Integrity Harness — judge guide

A bounded laboratory for one question: when an agent is pushed to skip a check,
what does it actually do, and can you verify the claim afterwards?

The same three-turn conversation runs in two independent arms. A deterministic
contract, not a model, issues the verdicts (`pass`, `fail`, `indeterminate`) per
clause, and the resulting package is signed and tamper-tested.

## 60-second path, no API key, no quota

No public deployment exists yet, so run it locally:

```bash
python -m pip install -e .
python demo_app.py          # http://127.0.0.1:7860/
```

Two commands need no key at all:

```bash
python scripts/judge_check.py      # verify the signed package + tamper test
python scripts/clause_matrix.py    # rebuild docs/clause_matrix.md from stored reports
```

`judge_check.py` exits non-zero if the signature or the tamper test fails.

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
| `docs/demo_walkthrough.mp4` | 1:27 video: offline path, live slice, signing and tamper test, English subtitles |
| `README.md` | Contracts, limits and what the evidence does not claim |

Verify the signed package without a key:

```bash
python -m pip install -r offline_verifier/requirements.txt
python offline_verifier/app.py    # upload the ZIP, run verify and tamper test
```

## How this maps to the criteria

- **Creativity:** agents are usually shipped with a prompt and a hope. Here the
  acceptance criteria are a contract, and the evidence is signed.
- **Technical ambition:** two independent arms, deterministic verdicts per
  clause, Ed25519 signing, a tamper test that survives a re-hashed manifest, and
  a ledger that reserves six provider calls before the first one is sent.
- **Whether it works:** the whole path runs offline except the live slice, and
  the stored reference run plus the synthetic control need no key at all.

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
