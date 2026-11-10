# ADR-2026-11-10: Regression and Langfuse in CI/CD

Status: Accepted
Date: 2026-11-10
Extends: [ADR-2026-10-19: Autonomous Test Pipeline](2026-10-19-autonomous-test-pipeline.md),
[ADR-2026-10-31: Langfuse as the Dataset, Experiment and Score Platform](2026-10-31-langfuse-platform-integration.md)

## Context

CI was one offline gate on Windows: pytest and one of the three [eval](../glossary.md) suites.
Nothing ran the agent on live sites, so prompt and context changes were checked by hand; Linux
and the Docker image were never built; nothing in CI touched Langfuse. A live
[Golden Set](../glossary.md) run costs model calls and is noisy, so the user wants it only around
merges into `main`, each mode optional, with a merge always possible without it, and no
scheduled run. A fresh runner also has no previous batch to compare with, and the "exactly
once" sync ledgers live in the batch folders. The full design, requirements and open questions
are in the [Regression and Langfuse in CI/CD design](../development/2026-11-10-regression-ci-cd-design.md).

## Decision

- **Offline gate** (`gate.yml`): Windows and Linux, pytest plus all three eval suites as
  separate steps, JUnit and a step summary (`scripts/ci_summary.py gate`). No network, model or
  secret. The Linux job is `continue-on-error` until it is stable.
- **Live runs only on request** (`regression.yml` → reusable `live-run.yml`, `ubuntu-latest`,
  headless Chromium behind Playwright MCP, profile `ci/regression.yaml`):
  - label `regression:before-merge`: a run on the PR merge ref that sets the commit status
    `regression/before-merge`;
  - label `regression:after-merge` (set before merging): a run on `merge_commit_sha`;
  - `workflow_dispatch`: a manual measurement of a commit of `main`;
  - repository variable `REGRESSION_DEFAULT` (`off` by default), opted out per PR with
    `regression:skip`.
- **The status is written explicitly, never through a skipped job**: GitHub counts a skipped
  job as passed, so a rerun on an unrelated label would mask a red run. A stdlib-only decision
  script (`scripts/ci_regression_mode.py`) writes `success` when no run is requested or the
  request was removed, `pending` when a run starts, and nothing when a label event does not
  change the request. A run in flight rereads the PR and writes its result only if the head is
  unchanged and the run is still requested. The status may therefore be a required check: it
  blocks only a requested red run.
- **The base is the nearest measured ancestor** (`scripts/ci_state.py compare`): among the
  stored main measurements of the same Golden Set, the one whose commit is an ancestor of the
  measured commit and that no other candidate descends from. A late run of an old commit or a
  run of an unmerged branch never becomes a base.
- **State between runs is the `ci-state` artifact chain**, written only by main measurements
  (after merge, manual) and only over a restored chain: the last
  `pipeline.ci_state_keep_batches` batches, the batch history, the platform journals and
  `ci/verdicts.jsonl`. Before-merge runs read it and never write it.
- **The verdict is local and code** (`scripts/ci_summary.py live`): `pass`, `regression`,
  `no_base`, `perf` (slower or costlier only) or `infra`. `infra` means the run broke, no scenario
  ran, or the share of infra failures exceeded `pipeline.ci_max_inconclusive_ratio`. Main
  measurements comment the PR and open, extend or close the `regression-main` /
  `regression-infra` issue.
- **Langfuse only in main measurements**, from secrets, in a separate project; its failures are
  warnings and never change the verdict. Before-merge runs get no Langfuse keys.
- **CD** (`docker.yml`): the stand image is built and smoke-tested in PRs that touch it.
  A `v*` tag publishes it to GHCR only with a green main measurement of the tag's commit or an
  ancestor within `pipeline.ci_release_verdict_max_age_days`. The variable
  `RELEASE_REQUIRES_REGRESSION=false` switches the check off.
- The open questions take their documented defaults: the status is required (Q1); "no base"
  is `success` (Q2); `gemma4:31b-cloud` (Q3); `golden` only by hand (Q4); no `ci-history` branch
  (Q5); Linux becomes required after two stable weeks (Q6); no Langfuse before merge (Q7); the
  bridge fix for G8 waits for the user's test (Q8); no merge queue (Q9);
  `REGRESSION_DEFAULT=off` (Q10); `capture_content: none` (Q11).

## Consequences

- A merge is never forced to pay for a live run; a requested red run blocks it, and removing
  the label or an admin bypass releases it, visibly in the PR timeline.
- The choice is made with labels in advance: the Merge button has no options, and an
  after-merge run cannot stop a merge, only report it and hold back a release.
- PRs from forks get neither a status nor a run; with the status required they are merged by
  the owner (bypass) or after moving the branch.
- Under frequent merges a waiting main measurement may be replaced by the next one; the later
  measurement covers the changes cumulatively.
- A chain gap longer than the artifact retention (90 days) is a normal "no base", and the next
  measurement becomes the base.
- New settings: `pipeline.ci_state_keep_batches` (5), `pipeline.ci_max_inconclusive_ratio`
  (0.3), `pipeline.ci_release_verdict_max_age_days` (14).
- Follow-up: the first runs on a test PR (positive and negative paths), the repository setup
  (labels, variables, secrets, ruleset), a local Linux benchmark run, and the G8 bridge fix.

## Alternatives Considered

- A label plus `if:` on a required job: a rerun on an unrelated label is skipped and counts as a
  pass, masking a red run.
- `workflow_dispatch` only on the PR branch: the result is not tied to the PR.
- A comment command (`/regression`, `issue_comment`): runs in the context of `main` and needs a
  manual checkout of the PR; possible later on top of the labels.
- A nightly run on a self-hosted Windows runner (the 2026-10-19 design §4.6.6): rejected by the
  user (no schedule); a self-hosted runner is unsafe for a public repository.
- A `ci-history` branch, `actions/cache` or Langfuse as the state: write access and repository
  growth; eviction and immutable keys; 30-day retention and a verdict that must not depend on
  the platform.

## Related

- Design: [Regression and Langfuse in CI/CD](../development/2026-11-10-regression-ci-cd-design.md)
- Guides: [Testing Pipeline Guide](../development/testing-pipeline.md#live-regression-in-ci),
  [Langfuse Integration Runbook](../development/langfuse.md#14-in-ci)
- Diagram: [CI/CD Pipeline](../diagrams/ci-cd-pipeline.md)
- Code: `.github/workflows/{gate,regression,live-run,docker}.yml`,
  `.github/actions/live-setup/action.yml`, `ci/regression.yaml`,
  `scripts/ci_regression_mode.py`, `scripts/ci_state.py`, `scripts/ci_summary.py`
- Tests: `tests/test_ci_regression_mode.py`, `tests/test_ci_state.py`, `tests/test_ci_summary.py`,
  `tests/test_ci_profile.py`
