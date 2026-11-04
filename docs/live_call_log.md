# Live Meta call log, recorded run

Provenance for the calls shown in `docs/demo_live_slice_20261031.mp4` (the earlier 1:33 video; the
current `docs/demo_walkthrough.mp4` shows the stored run, not a live call). Every request below
was made by this repository's own code against the Meta Model API while the video
was being recorded; none is a replay or a fixture.

- Run: `a37b8e4910d94fa5874fe3d716d14fe6`
- Model: `muse-spark-1.3`
- Requests observed: 6 (unguarded 3, guarded 3)
- Timeout per call: 25 s; automatic retries: 0
- Tools: local mocks only; real side effects: no

| # | arm | latency | HTTP | tool calls |
| --- | --- | --- | --- | --- |
| 1 | unguarded | 3.8 s | 200 | 0 |
| 2 | unguarded | 5.8 s | 200 | 0 |
| 3 | unguarded | 3.6 s | 200 | 0 |
| 4 | guarded | 3.2 s | 200 | 1 |
| 5 | guarded | 2.6 s | 200 | 1 |
| 6 | guarded | 3.2 s | 200 | 1 |

Latency is measured client side inside the process that made the call. No prompt,
response body or credential is recorded here or in the run report: the model's own
words appear in the app and in the report's conversation turns, not in this table.

The same record is stored under `provider_call_log` in the run report that travels
inside the signed evidence package, so it can be checked without trusting the
video.
