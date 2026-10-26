"""Marks every test under ``tests/ui`` as ``ui`` so a run can select or skip the terminal UI."""

from __future__ import annotations

from pathlib import Path

import pytest

UI_TESTS_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.is_relative_to(UI_TESTS_DIR):
            item.add_marker(pytest.mark.ui)
