from langgraph.graph import END

from src.agent.routers import reflect_router


def make_state(reflection, replan_count=0, retry_attempts=0, last_error_type=None):
    return {
        "messages": [],
        "error_count": 0,
        "retry_attempts": retry_attempts,
        "total_tool_calls": 0,
        "last_error_type": last_error_type,
        "last_action": None,
        "observation": None,
        "reflection": reflection,
        "replan_count": replan_count,
    }


def test_continue_routes_to_execute():
    assert reflect_router(make_state("continue")) == "execute"


def test_replan_routes_to_plan_when_under_limit():
    assert reflect_router(make_state("replan", replan_count=0)) == "plan"


def test_replan_routes_to_end_when_limit_reached():
    assert reflect_router(make_state("replan", replan_count=2)) == END


def test_retry_routes_to_backoff():
    assert reflect_router(make_state("retry")) == "backoff"


def test_done_routes_to_end():
    assert reflect_router(make_state("done")) == END


def test_fatal_routes_to_end():
    assert reflect_router(make_state("fatal")) == END
