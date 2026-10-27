# Meta Global AI Developer Hackathon — submission repository

Signed, verifiable behavioural assurance for agents built on Meta models.

You give it an agent, it runs a documented battery of situational-pressure
scenarios, a deterministic contract emits the verdicts, and the run is exported as
a signed evidence package that a third party can re-verify without trusting us.

**Status:** pre-event baseline. The submission work (live path, generated
scenarios, signed package, hosted demo) is built during the event and lands in the
commits after the baseline.

---

## Provenance: what existed before, what was built during the event

The rules require that judges can identify what was newly created during the
hackathon. This repository is built so that the answer is in the git history
rather than in a claim.

| Component | Origin | Commit |
| --- | --- | --- |
| Behavioural engine (`src/autopilot/`): contracts, deterministic evaluator, declarative sandbox | **Pre-existing**, built before the event | `baseline:` import |
| Pressure harness (`hackatons/meta/`): scenarios, contracts, runner, offline report | **Pre-existing**, built before the event | `baseline:` import |
| Live Meta Model API path wired as the demo default | Built during the event | see log below |
| Muse Spark as scenario *generator*, deterministic contract as judge | Built during the event | see log below |
| Projection to signed, citable evidence package + tamper detection | Built during the event | see log below |
| Hosted demo instance + three-minute video | Built during the event | see log below |

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

Live, against Meta Model API, with the key in the environment (never in a file):

```bash
export META_MODEL_API_KEY="…"
export META_MODEL_NAME="<model id enabled for your account>"
python -X utf8 hackatons/meta/run_meta_harness.py --live-agent
```

`--live-agent` without a key exits with an error instead of silently falling back
to fixtures. That is deliberate: a report labelled "live" must never contain
model-free results.

---

## What these numbers are not

The offline battery runs on fixed synthetic variants. Its counts describe the
plumbing and the contract rules, not the behaviour of any model. They are not
failure rates, not causal estimates, and must not be presented as model results.
The verdicts come from the deterministic contract evaluator, never from an LLM:
the model under test is the subject, not the judge.

The harness executes no real side effects. Tool calls are local mocks checked
against a closed allowlist.
