from . import ExecutorState


def retry_router(state: ExecutorState, max_retries: int):
    error_type = state.get("last_error_type")
    attempts = state.get("retry_attempts", 0)

    if error_type == "fatal":
        return "abort"

    if error_type == "retryable" and attempts < max_retries:
        return "backoff"

    return "abort"
