# Wonder pipeline validation

Owner: Codex owns local builds, test completion, results and repairs on `wonder-fix`.
Coordinator owns pushing. No push, deployment or release is part of this build.

Before each changed/scoped run, collect the exact node IDs with the same Python
interpreter, paths and selection. Confirm expected parameter cases are present.
Then execute that scope and verify nonzero executed counts; collection alone is
not execution. Save command, source identity and result under
`~/wonder-audit/gpt-evidence/`. On failure preserve the log, diagnose the actual
stage, and rerun the demonstrated gap. On pass advance to the next phase and run
the full suite before its checkpoint. Never count skipped cases as passes.

Order: evidence/grading and app logic unit tests, mocked tool/store/billing
integration checks, fixture image checks, then full regression. This is a Python
MCP service with no native UI or deployed backend in this scope; native device,
accessibility and minimum mobile OS tests do not apply. Fake API tests prove
local orchestration, not live provider segmentation or photographic quality.

Baseline source: `ff72fcbfc1a926f84aa828c771d882a9ff078b04`.
Baseline: 337 collected; 336 passed, 1 skipped (52.57 s).
Retain original logs; do not carry a checked status to another source revision.

Quality policy constants are versioned heuristics. The two supplied hero masks
are failure fixtures. Existing synthetic accepted fixture controls are the
available pass fixtures; no labelled multi-category photo corpus or hero source
photos were supplied. A geometry pass is not human acceptance of a listing.

Fidelity rules remain alpha 255, tolerance 0, no foreground scaling; paper is
separate, only at alpha 0 with a one pixel guard. Identify prompt/schema must
remain identical to the baseline. New schema 4 verification is Python only.
