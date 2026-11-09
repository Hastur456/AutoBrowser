"""Docker smoke test of the stand (``-m live``; skipped without a Docker daemon).

Builds the image (or uses ``AUTOBROWSER_SMOKE_IMAGE``), starts it with a probe-only job on a
``data:`` page, checks ``/healthz``, runs the job over the API and reads its report: the probe
ran in headless Chromium behind Playwright MCP without a model. The main task then fails on
purpose (the model host is unreachable), which is still a complete report. SIGTERM stops the
stand cleanly (``serve.stopped``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "smoke-t0ken"
PROFILE = """
llm:
  provider: ollama
  model: smoke-no-model
  host: http://127.0.0.1:9
browser:
  launch: external
mcp_servers:
  playwright:
    transport: stdio
    command: playwright-mcp
    args: [--headless, --browser, chromium, --user-data-dir, "{profile_dir}"]
    stateful: true
replay:
  enabled: true
serve:
  host: 0.0.0.0
  allow_remote: true
  jobs:
    smoke:
      task: "Report the number on the page."
      probe:
        steps:
          - {tool: browser_navigate, args: {url: "data:text/html,<h1>Smoke 42</h1>"}}
          - {tool: browser_snapshot}
        select: "(?s)heading.*"
"""


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


@pytest.fixture(scope="module")
def image() -> str:
    if shutil.which("docker") is None or _docker("info").returncode != 0:
        pytest.skip("no Docker daemon")
    tag = os.environ.get("AUTOBROWSER_SMOKE_IMAGE", "")
    if tag:
        return tag
    tag = "autobrowser-serve:smoke"
    built = _docker("build", "-t", tag, str(ROOT), timeout=1800)
    assert built.returncode == 0, built.stderr[-2000:]
    return tag


def _wait(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(1)
    raise AssertionError(f"timed out waiting for {what}")


def test_the_stand_runs_a_probe_in_its_container(image: str, tmp_path: Path) -> None:
    (tmp_path / "serve.yaml").write_text(PROFILE, encoding="utf-8")
    started = _docker(
        "run", "-d", "--shm-size", "1g", "-p", "127.0.0.1::8765",
        "-e", f"AUTOBROWSER_SERVE__TOKEN={TOKEN}", "-v", f"{tmp_path}:/data", image,
    )
    assert started.returncode == 0, started.stderr
    container = started.stdout.strip()
    try:
        address = _docker("port", container, "8765").stdout.strip().splitlines()[0]
        base = f"http://{address}"

        def healthy() -> bool:
            try:
                return httpx.get(f"{base}/healthz", timeout=2).json() == {"status": "ok"}
            except (httpx.HTTPError, ValueError):
                return False

        _wait(healthy, 180, "/healthz")
        auth = {"Authorization": f"Bearer {TOKEN}"}
        assert httpx.get(f"{base}/v1/status", timeout=5).status_code == 401
        fired = httpx.post(f"{base}/v1/jobs/smoke/runs", headers=auth, timeout=5)
        assert fired.status_code == 202, fired.text
        run_id = fired.json()["run_id"]

        def reported() -> dict | None:
            body = httpx.get(f"{base}/v1/runs/{run_id}", headers=auth, timeout=5).json()
            return body if "status" in body else None

        report = _wait(reported, 240, "the run report")
        probe = report["tasks"][0]
        assert probe["kind"] == "probe"
        assert probe["status"] == "completed", json.dumps(report)
        assert report["changed"] is True
    finally:
        _docker("stop", "-t", "30", container, timeout=60)
        logs = _docker("logs", container).stdout if _docker("inspect", container).returncode == 0 else ""
        _docker("rm", "-f", container)
    events = (tmp_path / "serve" / "events.jsonl").read_text(encoding="utf-8")
    assert '"serve.stopped"' in events, logs
