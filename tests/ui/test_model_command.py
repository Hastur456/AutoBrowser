"""``/model``: the picker it opens and the completion of ``/model <id>``."""

from __future__ import annotations

from types import SimpleNamespace

from src.ui.tui.commands import MODEL_PICKER, find_command, model_picker


def test_the_picker_marks_the_current_model_and_sanitizes_ids() -> None:
    picker = model_picker(["gemma-4", "evil\x1b[2Jmodel"], "gemma-4", ru=False)

    assert picker.picker_id == MODEL_PICKER
    assert picker.title == "Model · now gemma-4"
    assert [option.value for option in picker.options] == ["gemma-4", "evil\x1b[2Jmodel"]
    assert [option.detail for option in picker.options] == ["current", ""]
    assert "\x1b" not in picker.options[1].label


def test_model_ids_complete_from_the_cached_list() -> None:
    command = find_command("model")
    assert command is not None and command.complete_args is not None
    host = SimpleNamespace(model_ids=("gemma-4", "gpt-5", "gpt-5-mini"))

    assert command.complete_args(host, "gpt") == ["gpt-5", "gpt-5-mini"]  # type: ignore[arg-type]
    assert command.complete_args(SimpleNamespace(model_ids=()), "") == []  # type: ignore[arg-type]
