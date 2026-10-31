# Meta Global AI Developer Hackathon — submission repository

Signed evidence packaging and offline verification for behavioural-assurance runs on Meta models.

The harness evaluates documented situational-pressure scenarios with a deterministic contract. A locally verified Ed25519 package is available for a saved live run; its public key is embedded and self-described, so it detects tampering but does not establish signer identity or trusted time.

**Status: evidence package verified; submission not ready.** A saved Meta target-agent run and its signed archive are verified offline. The participant guide requires a three-minute demo video and a public repository, and says judges use the video, code, and live product as primary judging artifacts. The local verifier is only an evidence-review component, not the complete live project demo. Public hosting/URL requirements for the product are not explicit in the guide shared with the team and must be checked in the submission platform. Video, product access for judges, final public-repository provenance, and team submission details are pending.

---

## Provenance: what existed before, what was built during the event

The submission should let judges identify what was newly created during the
hackathon. This repository is built so that the answer is in the git history
rather than in a claim.

| Component | Evidence/status | Provenance |
| --- | --- | --- |
| Behavioural engine (`src/autopilot/`): contracts, deterministic evaluator, declarative sandbox | Pre-existing | Imported in baseline commit `2fd192d` |
| Pressure harness (`hackatons/meta/`): scenarios, contracts, runner | Pre-existing baseline; live Meta target-agent path is present and exercised by the saved run | Baseline commit `2fd192d`; saved report is described below |
| Muse Spark as scenario *generator* | Not evidenced by the saved held-out run | Its report records `meta_model_generator_mode: deterministic_offline`; do not claim live generation from this artifact |
| Signed evidence packager + tamper detection | Implemented and verified locally after the baseline; files are still in the working tree | Not yet represented by a post-baseline commit |
| Three-minute demo video | Required by the participant guide; pending | Not recorded |
| Live product for judging | Local one-case live demo app implemented and tested offline; live run and public hosting pending | No public app deployed; the app enforces a six-request cap and is not a general chat |
| Public repository and team submission | Public repository required; representative must submit once per team/startup and list every contributor | Final public access/provenance and submission details pending |

Everything imported in the `baseline:` commit is verbatim: nothing was edited to
make the pre-existing part look new, and nothing built during the event will be
backdated.

---

## Run it

Requires Python 3.12 and the three runtime dependencies (`cryptography`,
`pydantic`, `PyYAML`).

Offline (no network, no credentials — plumbing only):

```bash
python -X utf8 hackatons/meta/run_meta_harness.py
```

A live run against Meta Model API is opt-in and can incur charges. Do not run it until the account owner has confirmed a spend ceiling and the intended model. The key may be supplied as `META_MODEL_API_KEY` or `MODEL_API_KEY`; for local use only, the harness also reads a git-ignored `.env`, `.env.txt`, or `apimeta.env` at the repository root. Never commit, print, or copy these secrets into browser/client code.

```bash
export META_MODEL_API_KEY="<set privately in your shell>"
export META_MODEL_NAME="<model id enabled for your account>"
python -X utf8 hackatons/meta/run_meta_harness.py --live-agent
```

`--live-agent` without a key exits with an error instead of silently falling back
to fixtures. The default live run covers four core scenarios; `--include-expanded-live` opts into twelve more multi-turn variants. The saved six-case held-out report contains 36 assistant proposals. These are observed run details, **not an enforced request or spend cap**: the CLI does not enforce a global/per-visitor quota or a monetary ceiling. Do not expose any live route publicly until those limits are implemented and tested, and the owner confirms the maximum spend.

For a bounded paired live check of the three untagged textual guard scenarios
(maximum six target-model requests; local mock tools only; no real side effects):

```bash
python -X utf8 hackatons/meta/run_meta_harness.py --live-agent --text-guard-live-only
```

This mode supplies no `pressure_type` or exception-trigger metadata. It still
uses the scenario's explicit `verified_authorization` state for the mock tool
boundary; it does not connect to a real authorization service. The comparison
is an exploratory run, not an independent detector accuracy estimate.

---

## Bounded live laboratory

The app lets the visitor select one of three fixed held-out cases: authority,
urgency, or ambiguity. It runs the selected case against Meta, then signs,
verifies, and tamper-tests the resulting package. It accepts no free-form input
and never falls back to fixtures.

```bash
python -m pip install -e .
python -m pip install -r offline_verifier/requirements.txt
python demo_app.py
```

The live run is opt-in through the button. It uses three turns in each of two
arms, a hard cap of six provider requests, 1,024 completion tokens, a 25-second
per-call timeout, no automatic retries, and local mock tools. A persistent
SQLite ledger enforces three runs per visitor/24 h, keyed on the Gradio session
with the client address and user agent as fallback, 40 runs per day, 200 runs
total, and 1,200 reserved requests. The signing key and run
artifacts live under `DEMO_STATE_DIR` (default `.tmp/demo-state`, ignored by
Git); the Docker image uses `/data` and does not contain a key.

The laboratory can replay the original final-turn proposal from the unguarded
live arm in two local mock sandboxes, with and without the authorization
boundary. Replay makes no new model calls and is not a replay of the full
conversation. If no restricted action was proposed, it reports that there was
no restricted attempt to block. A separate synthetic control supplies the same
mock operation under unverified and verified authorization; it is not a Meta
model proposal. In both exercises the no-boundary arm is deliberately unsafe
and isolated to mocks. The live scenario supplies pressure and authorization
metadata; these results are not evidence of general safety.

No public deployment exists yet. Do not expose the app publicly until the
hosting gate is verified: persistent ledger volume, secret injection, a
provider-side spend ceiling, reverse-proxy header handling, and a clean-browser
test. The app is not the offline verifier; pressing the button makes a new live
call, while the verifier only replays a saved package.

Behind a reverse proxy, set `DEMO_TRUSTED_PROXIES` to the proxy addresses or
CIDRs (comma-separated). Only then does the app read `X-Forwarded-For`, and it
takes the hop added by the nearest trusted proxy rather than the leftmost entry.
With the variable unset the header is ignored entirely. The visitor identity is
the Gradio session, so two people behind the same address do not block each
other; the per-visitor limit is a demo quota, and the global caps are what bound
spend.

---

## Local offline evidence verifier

A small Gradio UI is available in [offline_verifier/](offline_verifier/). It accepts size-limited ZIPs, checks the manifest/signature, and runs the tamper test against a temporary copy. It makes no Meta or other external API calls and does not need a secret. To run it locally from this directory:

```bash
python -m pip install -r offline_verifier/requirements.txt
python offline_verifier/app.py
```

This is a local evidence-review aid, **not the complete project demo or a hosted service**. The participant guide shared with the team requires a three-minute demo video and a public repository, and identifies the live product as a primary judging artifact. It does not explicitly say that the live product must be reachable through a public hosted URL; confirm the access field/instructions in the submission platform. Hugging Face is not part of the plan; no Space has been created or published. Do not deploy until the access requirement, destination, authorization, and operational controls are clear. A live public Meta demo is separately blocked until its backend has a fail-closed hard request ceiling at each provider invocation, global and per-visitor quotas that survive concurrent requests/restarts, a confirmed monetary spend cap at the provider account, bounded timeouts/no automatic retries, and verified secret handling plus redacted logs. The existing harness has a 45-second provider timeout and 1,024-token completion limit per request, but those settings alone do not bound total spend.

The Meta API key in local ignored environment files was neither inspected nor copied; it is not a deployment credential. No hosting account was configured, nothing was published, and no new live call was made.

## Saved held-out live result and verification commands

A previously completed live Meta run is preserved outside the public submission tree at `.tmp/meta-live-heldout-v0_1-20261028/`. Build a signed evidence archive from that saved run without making API calls:

```bash
python scripts/package_meta_evidence.py --build
python scripts/package_meta_evidence.py --verify evidence/meta-heldout-live-20261028-signed.zip
python scripts/package_meta_evidence.py --tamper-test evidence/meta-heldout-live-20261028-signed.zip
```

The signed archive contains the saved JSON/Markdown report, held-out scenarios, contract, adapter, both sandbox configs, runner source, a SHA-256 manifest and an Ed25519 signature over the canonical manifest. The local private key is created once at `.tmp/meta-evidence-signing/ed25519-private.pem` and ignored by Git; never distribute it. `signature.json` includes the public key so verification works offline. The signature detects changes to the manifest and its hashed files, but the embedded key is self-described: without an independently trusted copy of that public key, it does not establish signer identity or a trusted timestamp. The archive is intentionally ignored by Git. The original `meta-heldout-live-20261028.zip` remains an unsigned legacy bundle. See the three-minute [demo script](docs/meta-heldout-live-demo.md) for accurate presentation and limitations; the local verifier can be opened with the command above.

The saved report records six synthetic held-out scenarios: six unguarded failures and six guarded passes across twelve paired trajectories. The same model is used in both arms. This does not establish general model robustness: `pressure_type` is supplied as metadata and the guarded sandbox applies deterministic scenario-matched responses. Do not present this one fixed run as a population rate, causal result, or production safety guarantee.

## Preregistered balanced guard evaluation (not yet run)

A separate 12-case internal comparison is frozen: six synthetic adversarial exception requests and six benign controls, with labels kept out of the stimulus file. Its preregistered budget is at most 24 model requests (one response per case in each arm), using local mock tools only. **No live call has been made for this suite; execution remains pending explicit authorization.**

- Protocol: [meta-guard-balanced-eval-prereg-v0.1.md](docs/meta-guard-balanced-eval-prereg-v0.1.md)
- Unlabeled-to-agent stimulus set: [blind_guard_eval_stimuli_v0.1.json](hackatons/meta/scenarios/blind_guard_eval_stimuli_v0.1.json)
- Separate scoring oracle: [blind_guard_eval_oracle_v0.1.json](hackatons/meta/scenarios/blind_guard_eval_oracle_v0.1.json)
- Freeze hashes: [blind_guard_eval_freeze_v0.1.json](hackatons/meta/scenarios/blind_guard_eval_freeze_v0.1.json)
- Offline verifier: `python scripts/verify_blind_guard_eval_freeze.py`

The verifier checks that labels are absent from the stimulus records, the oracle IDs and class balance match, and the frozen SHA-256 hashes are intact. This is still an internal synthetic protocol, not external review or a validated benchmark.

## What these numbers are not

The offline battery runs on fixed synthetic variants. Its counts describe the
plumbing and the contract rules, not the behaviour of any model. They are not
failure rates, not causal estimates, and must not be presented as model results.
The verdicts come from the deterministic contract evaluator, never from an LLM:
the model under test is the subject, not the judge.

The harness executes no real side effects. Tool calls are local mocks checked
against a closed allowlist.

## Textual exception screen (experimental)

The guarded sandbox includes an explicitly lexical, English/Spanish screening
rule for requests to act despite missing authorization controls. It only produces
a screening signal; the `verified_authorization` state must come from trusted
metadata, and the detector is not a semantic classifier. Its synthetic test
stimuli and reference labels are separate files:

- `hackatons/meta/scenarios/text_exception_stimuli_v0.1.json`
- `hackatons/meta/scenarios/text_exception_oracle_v0.1.json`

Run the offline fixture scorer with:

```bash
python scripts/evaluate_text_exception_detector.py
```

The reported metrics are only consistency checks on these small, human-authored
fixtures. The stimuli and labels were inspected during detector development, so
this is not an independent blind evaluation and does not demonstrate real-world
accuracy, semantic understanding, or generalization. A separate, independently
written and adjudicated dataset is required before any performance claim.
