# CI/CD Pipeline

The offline gate on every pull request, the optional live regression runs around merges into
`main`, the `ci-state` artifact chain, Langfuse, and the stand image. Design:
[Regression and Langfuse in CI/CD](../development/2026-11-10-regression-ci-cd-design.md);
decision: [ADR-2026-11-10](../decisions/2026-11-10-regression-and-langfuse-in-ci-cd.md); usage:
[Testing Pipeline Guide](../development/testing-pipeline.md#live-regression-in-ci).

## Levels and Triggers

```mermaid
flowchart LR
  PR["PR into main"] --> GATE["gate.yml<br/>pytest + evals x3<br/>Windows + Linux"]
  PR -- "image paths" --> DOCK["docker.yml<br/>build + smoke"]
  PR --> DEC["regression.yml: decide<br/>ci_regression_mode.py<br/>(labels, REGRESSION_DEFAULT)"]
  DEC -- "not requested / removed" --> OK["status regression/before-merge<br/>= success"]
  DEC -- "unrelated event" --> KEEP["nothing written<br/>(last result stands)"]
  DEC -- "regression:before-merge" --> BM["live-run.yml before-merge<br/>on the PR merge ref"]
  BM --> ST1["status success / failure / error<br/>+ sticky PR comment"]
  MERGE["PR merged"] --> DEC
  DEC -- "regression:after-merge" --> AM["live-run.yml after-merge<br/>on merge_commit_sha"]
  DISP["workflow_dispatch"] --> AM
  AM --> STATE[("artifact ci-state")]
  STATE -. "read only" .-> BM
  STATE --> AM
  AM -- "regression / infra" --> ISSUE["issue regression-main /<br/>regression-infra + PR comment"]
  AM -- "keys set" --> LF["Langfuse autobrowser-ci"]
  TAG["tag v*"] --> DOCK
  DOCK --> REL["release-check<br/>ci_state.py verdict"]
  STATE -.-> REL
  REL --> GHCR["ghcr.io image"]
```

## One Live Run

```mermaid
sequenceDiagram
  autonumber
  participant W as live-run.yml
  participant ST as artifact ci-state
  participant B as run_batch.py
  participant S as ci_state.py
  participant Y as ci_summary.py
  participant GH as GitHub (status, PR, issues)
  W->>ST: restore (newest unexpired)
  W->>W: check-ref (main modes), doctor --llm
  W->>B: --tasks <suite> --continue-on-error --grade
  B-->>W: batch.json (batch_dir)
  W->>S: compare: nearest measured ancestor, same Golden Set
  S-->>W: compare.json (status, counts, base, findings)
  W->>Y: live --mode
  alt before-merge
    Y->>GH: reread PR: same head and still requested?
    Y->>GH: status + sticky comment (else nothing)
  else after-merge / manual
    Y->>Y: append ci/verdicts.jsonl
    Y->>GH: PR comment, open / extend / close issues
    W->>ST: save + upload (only over a restored chain)
  end
```
