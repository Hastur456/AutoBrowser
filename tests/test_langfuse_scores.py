"""``scripts/langfuse_bridge/scores.py``: score configs and the scores of a synced batch."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from scripts.langfuse_bridge.experiments import span_id
from scripts.langfuse_bridge.scores import config_body, ensure_score_configs
from src.agent_loop.platform_records import ScoreDefinition, score_definitions
from tests.platform_fixtures.batch_folder import T0, Scenario
from tests.platform_fixtures.fake_langfuse import FakeLangfuse
from tests.test_langfuse_client import client
from tests.test_langfuse_sync import Env


def scores_by(fake: FakeLangfuse, name: str) -> list[dict[str, Any]]:
    return [score for score in fake.scores.values() if score["name"] == name]


# -- configs ------------------------------------------------------------------------------------


def test_config_bodies_follow_the_definitions() -> None:
    body = config_body(ScoreDefinition("kind", "categorical", ("a", "b"), description="d"))

    assert body == {"name": "kind", "dataType": "CATEGORICAL", "categories": [{"label": "a", "value": 0}, {"label": "b", "value": 1}], "description": "d"}
    assert config_body(ScoreDefinition("rate", "numeric", min_value=0.0, max_value=1.0))["minValue"] == 0.0
    assert "categories" not in config_body(ScoreDefinition("ok", "boolean"))


def test_configs_are_created_once() -> None:
    fake = FakeLangfuse()
    api = client(fake)

    first = ensure_score_configs(api, score_definitions())
    writes = len(fake.writes())
    second = ensure_score_configs(api, score_definitions())

    assert first == second and set(first) == {definition.name for definition in score_definitions()}
    assert len(fake.writes()) == writes


def test_a_grown_vocabulary_archives_the_old_config_and_creates_a_new_one() -> None:
    fake = FakeLangfuse()
    api = client(fake)
    old = ensure_score_configs(api, [ScoreDefinition("kind", "categorical", ("a",))])["kind"]

    new = ensure_score_configs(api, [ScoreDefinition("kind", "categorical", ("a", "b"))])["kind"]

    assert new != old
    assert fake.configs[old]["isArchived"] is True
    assert [label["label"] for label in fake.configs[new]["categories"]] == ["a", "b"]


# -- scores of a batch --------------------------------------------------------------------------


SCENARIOS = [Scenario("books-1"), Scenario("quotes-1", failure_kind="grade_failed", grade="failed", tasks=["one", "two"])]


def test_a_synced_batch_sends_item_task_and_run_scores(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS)

    report = env.push(batch)

    assert report.state == "complete" and report.scores_accepted == len(env.fake.scores)
    ledger = env.ledger(batch)
    items = ledger["items"]
    passed = {score["traceId"]: score for score in scores_by(env.fake, "passed")}
    assert passed[items["books-1"]["trace_id"]]["value"] == 1
    assert passed[items["books-1"]["trace_id"]]["observationId"] == items["books-1"]["root_span_id"]
    assert passed[items["books-1"]["trace_id"]]["dataType"] == "BOOLEAN"
    kind = next(score for score in scores_by(env.fake, "failure_kind") if score["traceId"] == items["quotes-1"]["trace_id"])
    assert kind["value"] == "grade_failed" and kind["dataType"] == "CATEGORICAL" and kind["configId"]
    task = next(score for score in scores_by(env.fake, "task_failure_kind") if score["traceId"] == items["quotes-1"]["trace_id"])
    trace_id = items["quotes-1"]["trace_id"]
    assert task["observationId"] in {span_id(trace_id, "quotes-1-task-0-goal.started-0"), span_id(trace_id, "quotes-1-task-1-goal.started-0")}
    assert len(scores_by(env.fake, "task_failure_kind")) == 3
    run = scores_by(env.fake, "verdict")[0]
    assert run["datasetRunId"] == "batch-1a2b3c4d5e6f" and run["value"] == "no_baseline"
    assert "traceId" not in run
    assert all(score["environment"] == "batch" for score in env.fake.scores.values())
    finished = json.loads((batch / "batch.json").read_text(encoding="utf-8"))["finished_at"]
    assert {score["timestamp"] for score in env.fake.scores.values()} == {finished}
    assert ledger["scores"]["state"] == "posted" and ledger["scores"]["accepted"] == len(env.fake.scores)


def test_score_ids_are_fixed_so_a_new_send_upserts(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS)
    env.push(batch)
    ids = set(env.fake.scores)

    env.push(batch, retry_rejected=True)

    assert set(env.fake.scores) == ids


def test_a_batch_without_grade_sends_no_grade_scores(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS, graded=False)

    env.push(batch)

    assert not scores_by(env.fake, "grade") and not [s for s in env.fake.scores.values() if s["name"].startswith("check.")]


def test_the_local_verdict_against_the_previous_batch_becomes_scores(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.batch([Scenario("books-1"), Scenario("quotes-1")], batch_id="batch-000000000000", started=T0 - timedelta(hours=1))
    batch = env.batch(SCENARIOS)
    # both copies are the same scenario set: make the hashes equal
    data = json.loads((batch / "batch.json").read_text(encoding="utf-8"))
    base = json.loads((env.batches / "batch-000000000000" / "batch.json").read_text(encoding="utf-8"))
    base["provenance"]["tasks_sha256"] = data["provenance"]["tasks_sha256"]
    (env.batches / "batch-000000000000" / "batch.json").write_text(json.dumps(base), encoding="utf-8")

    env.push(batch)

    assert env.ledger(batch)["baseline_batch_id"] == "batch-000000000000"
    regression = {score["traceId"]: score["value"] for score in scores_by(env.fake, "regression")}
    items = env.ledger(batch)["items"]
    assert regression[items["quotes-1"]["trace_id"]] == "regression"
    assert regression[items["books-1"]["trace_id"]] == "unchanged"
    assert scores_by(env.fake, "verdict")[0]["value"] == "regression"
    assert scores_by(env.fake, "regressions")[0]["value"] == 1.0


def test_rejected_scores_are_reported_not_retried_until_asked(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS)
    env.fake.fail("POST", "/scores", status=207, body={"accepted": 0, "rejected": 1, "errors": [{"index": 0, "id": "x", "message": "bad value"}]})

    report = env.push(batch)

    assert report.state == "partial" and report.scores_rejected == 1 and report.exit_code() == 1
    ledger = env.ledger(batch)
    assert ledger["scores"]["rejected"][0]["message"] == "bad value"
    assert ledger["state"] == "pending"
    posts = [request for request in env.fake.requests if request.path.endswith("/scores")]
    assert len(posts) == 1
    first_body = posts[0].body[0]

    again = env.push(batch)
    assert len([request for request in env.fake.requests if request.path.endswith("/scores")]) == 1
    assert again.scores_rejected == 1

    fixed = env.push(batch, retry_rejected=True)
    resent = [request for request in env.fake.requests if request.path.endswith("/scores")][-1]
    assert [score["id"] for score in resent.body] == [first_body["id"]]
    assert fixed.state == "complete" and env.ledger(batch)["scores"]["rejected"] == []


def test_scores_wait_until_every_trace_is_exported(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(SCENARIOS)
    env.fake.fail("POST", "/otel/v1/traces", status=503, skip=1, times=5)

    env.push(batch)

    assert env.fake.scores == {} and env.ledger(batch)["scores"] is None


# -- annotation queue ------------------------------------------------------------------------------


TRIAGE = [
    Scenario("ok-1"),
    Scenario("fail-1", failure_kind="grade_failed", grade="failed"),
    Scenario("infra-1", failure_kind="timeout", grade="failed"),
    Scenario("stop-1", failure_kind="agent_stopped", grade="failed"),
    Scenario("ungraded-1", failure_kind="ungraded", grade="ungraded"),
]


def test_no_queue_without_a_queue_name(tmp_path: Path) -> None:
    env = Env(tmp_path)
    report = env.push(env.batch(TRIAGE))

    assert report.enqueued == 0 and env.fake.queues == {}


def test_failed_scenarios_go_to_the_queue_once(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(TRIAGE)

    report = env.push(batch, platform={"triage_queue": "autobrowser-triage"})

    queue = next(iter(env.fake.queues.values()))
    assert queue["name"] == "autobrowser-triage"
    config_names = {env.fake.configs[config_id]["name"] for config_id in queue["scoreConfigIds"]}
    assert config_names == {"human.verdict", "human.comment"}
    verdict = next(config for config in env.fake.configs.values() if config["name"] == "human.verdict")
    assert [c["label"] for c in verdict["categories"]] == ["agent_error", "site_changed", "anti_bot", "grader_wrong", "expected_outdated", "ok"]
    items = env.ledger(batch)["items"]
    queued = {entry["objectId"] for entry in env.fake.queue_items[queue["id"]]}
    assert queued == {items[name]["trace_id"] for name in ("fail-1", "stop-1", "ungraded-1")}
    assert report.enqueued == 3
    assert env.ledger(batch)["triage"]["enqueued"] == ["fail-1", "stop-1", "ungraded-1"]

    env.push(batch, platform={"triage_queue": "autobrowser-triage"}, retry_rejected=True)
    assert len(env.fake.queue_items[queue["id"]]) == 3


def test_the_queue_takes_at_most_the_configured_number(tmp_path: Path) -> None:
    env = Env(tmp_path)
    batch = env.batch(TRIAGE)

    env.push(batch, platform={"triage_queue": "q", "triage_max_items": 1})

    assert env.ledger(batch)["triage"]["enqueued"] == ["fail-1"]


# -- scores pull ---------------------------------------------------------------------------------


def test_judge_and_human_scores_are_pulled_into_the_batch(tmp_path: Path) -> None:
    from scripts.langfuse_bridge.scores import PLATFORM_SCORES_FILE_NAME, pull_scores

    env = Env(tmp_path)
    batch = env.batch(SCENARIOS)
    env.push(batch)
    items = env.ledger(batch)["items"]
    trace = items["quotes-1"]["trace_id"]
    task_span = span_id(trace, "quotes-1-task-1-goal.started-0")
    env.fake.add_score(name="judge.fields", value=False, dataType="BOOLEAN", source="EVAL", experimentId="batch-1a2b3c4d5e6f",
                       subject={"kind": "observation", "id": items["quotes-1"]["root_span_id"], "traceId": trace}, comment="no price")
    env.fake.add_score(name="human.verdict", value="grader_wrong", dataType="CATEGORICAL", source="ANNOTATION", experimentId="batch-1a2b3c4d5e6f",
                       subject={"kind": "observation", "id": task_span, "traceId": trace}, authorUserId="u1")
    env.fake.add_score(name="foreign", value=1, dataType="NUMERIC", source="EVAL", experimentId="batch-1a2b3c4d5e6f",
                       subject={"kind": "trace", "id": "ff" * 16})

    with env_client(env) as api:
        rows = pull_scores(api, batch)

    lines = [json.loads(line) for line in (batch / PLATFORM_SCORES_FILE_NAME).read_text(encoding="utf-8").splitlines()]
    assert lines == rows
    assert [(row["scenario_id"], row["name"], row["value"], row["source"]) for row in rows] == [
        ("quotes-1", "judge.fields", False, "EVAL"),
        ("quotes-1", "human.verdict", "grader_wrong", "ANNOTATION"),
    ]
    assert rows[0]["task_id"] is None and rows[0]["comment"] == "no price"
    assert rows[1]["task_id"] == "quotes-1-task-1" and rows[1]["author_user_id"] == "u1"
    request = [r for r in env.fake.requests if r.path.endswith("/v3/scores")][-1]
    assert request.params["experimentId"] == "batch-1a2b3c4d5e6f" and request.params["source"] == "EVAL,ANNOTATION"


def env_client(env: Env) -> Any:
    from scripts.langfuse_bridge.client import LangfuseClient
    from tests.test_langfuse_sync import make_settings

    return LangfuseClient(make_settings(env.tmp_path).eval_platform, transport=env.fake.transport(), sleep=lambda _s: None)
