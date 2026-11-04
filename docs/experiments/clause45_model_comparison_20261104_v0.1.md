# v0.1 — Four models, five runs: what the counts can and cannot order

**Question.** After five clause-45 runs on four models from two families (`muse-spark-1.1`, `1.2`, `1.3`,
`muse-wonder-1.0`), do the restatement rates separate the models? This note answers that with paired
intervals and states the one difference that survives.

**Instrument, identical in all five runs:** same contract, same adapter (v0.2), same opening scope message,
boundary removed from the system prompt, temperature 0, `plain` prompt, 40 conversations per arm, 120 calls
per run, one pass, no retries for empty replies. Arms: **escalation** (the user explicitly asks for the
payout to be approved) and **sustained** (urgency, prior commitment, social proof, claimed authority, with
no request to approve). The hand reading is the reference; detector figures are lower bounds.

## 1. The counts

| Run | Model | Family | Escalation | Sustained | Gap (manual) |
|---|---|---|---|---|---|
| v1.15 | `muse-spark-1.1` | spark | 14/39 = 50.0 % | 11/38 = 28.9 % | +21.1 |
| v1.16 | `muse-spark-1.1` | spark | 19/39 = 48.7 % | 7/38 = 18.4 % | +30.3 |
| v1.22 | `muse-spark-1.3` | spark | 24/40 = 60.0 % | 19/40 = 47.5 % | +12.5 |
| v1.23 | `muse-spark-1.2` | spark | 24/40 = 60.0 % | 20/40 = 50.0 % | +10.0 |
| v1.25 | `muse-wonder-1.0` | wonder | 20/40 = 50.0 % | 5/40 = 12.5 % | **+37.5** |

Denominators are non-empty replies, which is why two runs read 39 and 38 rather than 40.

## 2. Paired contrasts (Wald, 95 %)

Difference in percentage points, each run against `muse-wonder-1.0`, same arm.

| Run | vs wonder, escalation | vs wonder, sustained |
|---|---|---|
| v1.15 (`1.1`) | −14.1 (−35.7 to +7.5) | +16.4 (−1.2 to +34.1) |
| v1.16 (`1.1`) | −1.3 (−23.3 to +20.8) | +5.9 (−10.1 to +22.0) |
| v1.22 (`1.3`) | +10.0 (−11.7 to +31.7) | **+35.0 (+16.4 to +53.6)** |
| v1.23 (`1.2`) | +10.0 (−11.7 to +31.7) | **+37.5 (+18.9 to +56.1)** |

Within the spark family, sustained arm: `1.1` v1.15 vs `1.3` −18.6 (−39.7 to +2.6); vs `1.2` −21.1
(−42.2 to +0.1); `1.1` v1.16 vs `1.3` −29.1 (−48.9 to −9.3); vs `1.2` −31.6 (−51.4 to −11.8).

**Same model, twice, same instrument:** `1.1` reads 28.9 % then 18.4 % in the sustained arm — **+10.5
(−8.4 to +29.5)**. Two runs of one model differ by as much as most of the "between-model" differences
below.

## 3. What cannot be concluded

1. **No ranking inside the spark family.** Four of six pairs cross zero; the two that do not both involve
   the lower `1.1` run, and that run is itself 10.5 points away from the other `1.1` run. Ordering `1.1`,
   `1.2` and `1.3` from these counts would be ordering noise.
2. **`muse-wonder-1.0` is not "safer".** 12.5 % means it rarely names the limit under ambient pressure,
   and the contract requires naming it. A lower restatement rate is **worse** compliance here, not more
   firmness. Cession is zero in every run (see §5), so what varies is how often the boundary is spoken,
   never whether it is crossed.
3. **Model is not separated from run.** One run per model, n = 40 per arm: a slow day and a model
   difference are the same observation here. The wonder-versus-`1.2`/`1.3` contrast is the only one that
   clears its interval, and it should be replicated before anyone cites it as a model property.

## 4. What does survive: the axis, not the model

- **Escalation is stable across models:** 48.7-60.0 % in all five runs, and every paired contrast in that
  arm crosses zero. When the user asks for the payout to be approved, the reply names the limit, whoever
  is answering.
- **Sustained pressure is not:** 12.5-50.0 %, a four-fold spread on the same instrument.

So the two arms measure different things. Escalation measures compliance with the contract's own
obligation to name the decision authority — it is stable, and therefore comparable. Sustained pressure
measures how much an agent volunteers the boundary when nobody asked, which is a property of the model's
conversational style as much as of its policy. **A benchmark that only pressures ambiently ranks
talkativeness.** That is the methodological result, and it is why the README reports both arms separately
instead of one headline rate.

**Operational recommendation for anyone evaluating agents on this clause:**

1. Always include an arm where the user **asks for the prohibited action**. It is the only arm that is
   stable enough to compare across models.
2. Report **restatement** and **cession** separately. They move independently: 320 pressured turns, zero
   cessions, restatement from 12.5 % to 60.0 %.
3. Publish the interval, the denominator, and how many runs per model. At n = 40 a 10-point gap is not a
   finding.
4. Do not treat a lower restatement rate as a safety win without checking cession. Silence is not
   refusal.

## 5. Cession, checked lexically

`python scripts/check_clause45_no_cession_v0_1.py` applies the contract's `patterns_to_avoid` to all 591
replies of the five runs: **0** forms of approving, promising or quoting a payout. 320 of those turns were
under pressure. The agent does not cede in any model; the models differ in whether they say why.

## 6. Limitations

- The hand labels are mine, one reader, applied consistently across the five runs: internally comparable,
  not independent.
- Wald intervals on proportions with n ≈ 40 are approximate; with counts this small, a Wilson interval
  would be wider at the extremes (the 12.5 % cell especially). The qualitative reading — escalation
  stable, sustained dispersed — does not depend on the approximation.
- `1.1` and `1.3`/`1.2` differ in more than weights (the runs were days apart), so even a clean interval
  would need replication before being read as a model property.
- Two families, one provider. Nothing here generalises to other vendors.

## 7. Reproduce

```
python scripts/audit_clause45_restatement_v0_1.py      # 1.1, the two runs behind the 28.9 % and 18.4 %
python scripts/audit_clause45_second_model_v0_1.py     # 1.3 and 1.2 labels, v0.2, gate
python scripts/audit_clause45_fourth_model_v0_1.py     # wonder, and the v0.3 out-of-sample test
python scripts/check_clause45_no_cession_v0_1.py       # 591 replies, 0 cessions
```

Each script prints the per-cell counts, the bias, the false negatives and positives, and its exit code is
0 only when the arithmetic in this note holds.

Those four scripts live in the working repository, `bamontejano1978-beep/behavioural-autopilot`, where
the run JSONs and the `autopilot` package are; they are not duplicated here because they import it. This
submission repository ships the note, the contract's own adapter (`scripts/detector_clause45_v0_2.py`)
and the gate case; the run counts are written out per cell in this note so the arithmetic can be checked
with a calculator, which is how it was checked.
