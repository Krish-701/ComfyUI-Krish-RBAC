"""Unit tests for utils.workflow_run_log (no ComfyUI runtime required)."""
import os
import tempfile

import pytest

from utils.workflow_run_log import WorkflowRunLog


@pytest.fixture
def run_log():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "workflow_runs.json")
        yield WorkflowRunLog(path, max_runs=100)


def test_log_and_complete(run_log):
    r = run_log.log_queued(
        prompt_id="abc",
        user_id="uid-1",
        username="alice",
        workflow_name="portrait.json",
        node_count=5,
    )
    assert r["username"] == "alice"
    assert r["status"] == "queued"

    run_log.update_status("abc", "running")
    done = run_log.update_status("abc", "completed", finished=True)
    assert done["status"] == "completed"
    assert done["finished_at"] is not None
    assert done["duration_sec"] is not None

    listed = run_log.list_runs(username="alice")
    assert listed["total"] == 1
    assert listed["runs"][0]["workflow_name"] == "portrait.json"


def test_stats_by_user(run_log):
    run_log.log_queued(prompt_id="1", user_id="a", username="alice", workflow_name="A")
    run_log.log_queued(prompt_id="2", user_id="a", username="alice", workflow_name="B")
    run_log.log_queued(prompt_id="3", user_id="b", username="bob", workflow_name="A")
    run_log.update_status("1", "completed", finished=True)
    run_log.update_status("2", "error", finished=True)

    stats = run_log.stats()
    assert stats["total_runs"] == 3
    by_name = {u["username"]: u for u in stats["users"]}
    assert by_name["alice"]["total_runs"] == 2
    assert by_name["bob"]["total_runs"] == 1


def test_extract_workflow_name():
    assert (
        WorkflowRunLog.extract_workflow_name({"usgromana_workflow": "x.json"})
        == "x.json"
    )
    assert (
        WorkflowRunLog.extract_workflow_name(
            {"extra_pnginfo": {"workflow": {"name": "from-png"}}}
        )
        == "from-png"
    )
    assert WorkflowRunLog.extract_workflow_name({}, {"1": {}, "2": {}}) == "Prompt (2 nodes)"
