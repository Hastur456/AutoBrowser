"""Generic graders of a final answer: text, items, numbers, unknown keys."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from src.agent_loop.batch import load_batch_scenarios
from src.agent_loop.graders import answer_numbers, count_items, grade_answer

RU_LIST = """Первые пять курток:
1. Куртка демисезонная Zolla — 3 999 ₽
2. Куртка утеплённая Gloria Jeans — 4 499 ₽
   - цвет: чёрный
3. Пуховик Outventure — 12 990 ₽
4. Ветровка Demix — 2 799 ₽
5. Парка Finn Flare — 15 490 ₽
"""

RU_TABLE = """| Товар | Цена | Память |
|---|---:|---|
| MSI RTX 5070 Ventus | 64 990 ₽ | 12 ГБ |
| Palit RTX 5070 GamingPro | 62 450 ₽ | 12 ГБ |
"""

EN_LIST = """Top results:
- Gaming laptop A: $1,299.99
- Gaming laptop B: $999
* Gaming laptop C: $1,050.50
"""


def _statuses(answer: str, expected: dict[str, Any]) -> dict[str, str]:
    return {check.key: check.status for check in grade_answer(answer, expected).checks}


@pytest.mark.parametrize(
    "answer, items",
    [
        # numbering decides when the answer is numbered
        ("1. A\n2. B\n3. C\n\nПримечания:\n- фильтр\n- сортировка", 3),
        ("1. A\n2. B\n3. C\n\n- Примечание 1\n- Примечание 2", 3),
        ("Результаты:\n  1. A\n  2. B\n  3. C\n\n- Примечание", 3),
        ("1. Куртка A\nЦена: 5 990 ₽\n2. Куртка B\nЦена: 6 990 ₽\n3. Куртка C", 3),
        ("**1.** A — 5 990 ₽\n**2.** B\n**3.** C", 3),
        ("**1. Куртка A**\n   Цена: 1\n**2. Куртка B**\n   Цена: 2", 2),
        ("\n".join(f"### {i}. Товар {i}\n- Название\n- Цена\n- Рейтинг" for i in range(1, 6)), 5),
        ("1. A\n   1. step\n   2. step\n2. B", 2),
        ("1. A\n2. B\n\n* * *", 2),
        ("1.5 кг — вес\n2.0 кг", 0),
        ("Что сделано:\n1. Открыл Ozon.\n\nТовары:\n- A — 1 ₽\n- B — 2 ₽\n- C — 3 ₽", 3),
        ("1. ASUS TUF\n   - Цена: 99 990 ₽\n   - Рейтинг: 4.8", 1),
        # bullets and tables
        ("- a\n- b\n\n   - nested\n- c", 3),
        ("```\n- x\n- y\n- z\n```\n- a", 1),
        ("| a | b |\n|:--|--:|\n| x | 1 |\n| y | 2 |", 2),
        ("| a | b |\n|-|-|\n| x | 1 |", 1),
        ("a | b\n--- | ---\nx | 1\ny | 2", 2),
        ("| a | b |\n|---|---|\n| x | 1 |\n| y | 2 |\nИсточник: Ozon | 2026", 2),
        ("Price is 5 | 6 dollars", 0),
        ("| # | Товар | Цена |\n|---|---|---|\n| 1 | Куртка \\| черная | 100 ₽ |\n| 2 | B | 200 ₽ |", 2),
        ("| a | b |\n|---|---|\n| x | 1 |\n| wrapped cell\n| y | 2 |", 2),
    ],
)
def test_count_items_reads_markdown_shapes(answer: str, items: int) -> None:
    assert count_items(answer) == items


def test_unnumbered_attribute_bullets_look_like_items() -> None:
    """The documented limit: pair an item count with a per-item check."""

    one_product = "Нашёл только один ноутбук:\n- Название: ASUS TUF\n- Цена: 99 990 ₽\n- Рейтинг: 4.8"

    assert count_items(one_product) == 3
    assert grade_answer(one_product, {"items": 3, "regex": "(?s)(?:(?:₽|руб).*){3}"}).passed is False


def test_count_items_counts_top_level_list_items_only() -> None:
    assert count_items(RU_LIST) == 5
    assert count_items(EN_LIST) == 3


def test_count_items_counts_table_body_rows() -> None:
    assert count_items(RU_TABLE) == 2


def test_a_table_without_a_separator_counts_every_row() -> None:
    assert count_items("| a | 1 |\n| b | 2 |\n| c | 3 |") == 3


def test_plain_text_has_no_items() -> None:
    assert count_items("The first paragraph of the article about Python.") == 0


@pytest.mark.parametrize(
    "text, numbers",
    [
        ("Цена 120 000 ₽", [120000.0]),
        ("1 299,99 руб.", [1299.99]),
        ("$1,299.99 and $999", [1299.99, 999.0]),
        ("16,5 ГБ", [16.5]),
        ("narrow space 45\u202f990 ₽ and no-break 7\u00a0490", [45990.0, 7490.0]),
        ("RTX 5070, 12 ГБ.", [5070.0, 12.0]),
        ("1. Ноутбук — 89 990 ₽\n2) Ноутбук — 99 990 ₽", [89990.0, 99990.0]),
        ("**1.** Ноутбук — 89 990 ₽", [89990.0]),
        ("https://www.ozon.ru/product/noutbuk-1517329426/ — 89 990 ₽", [89990.0]),
        ("ASUS TUF A15 115 990 ₽", [15.0, 115990.0]),
        ("скидка -15%, курс −2,5, диапазон 10-20", [-15.0, -2.5, 10.0, 20.0]),
        ("ozon.ru/product/noutbuk-1234567890/ — 89 990 ₽", [89990.0]),
        ("Core Ultra 7 155H", [7.0, 155.0]),
        ("[ссылка](/product/noutbuk-asus-1234567890/) 74 990 ₽", [74990.0]),
        ("### 1. Ноутбук — 74 990 ₽", [74990.0]),
        ("15 990р и 120 000 руб", [15990.0, 120000.0]),
    ],
)
def test_answer_numbers_reads_thousands_separators(text: str, numbers: list[float]) -> None:
    assert answer_numbers(text) == numbers


def test_contains_is_case_insensitive_and_takes_a_list() -> None:
    answer = "Python — высокоуровневый язык программирования."

    assert _statuses(answer, {"contains": "python"}) == {"contains": "passed"}
    assert _statuses(answer, {"contains": ["PYTHON", "язык"]}) == {"contains": "passed"}
    (check,) = grade_answer(answer, {"contains": ["Python", "Ruby"]}).checks
    assert check.status == "failed"
    assert "Ruby" in check.message


def test_not_contains_fails_on_any_present_text() -> None:
    assert _statuses("All good.", {"not_contains": ["error", "captcha"]}) == {
        "not_contains": "passed"
    }
    assert _statuses("Ошибка: captcha", {"not_contains": ["CAPTCHA"]}) == {
        "not_contains": "failed"
    }


def test_regex_needs_every_pattern() -> None:
    answer = "Repository: https://github.com/langchain-ai/langgraph, 12.3k stars"

    assert _statuses(answer, {"regex": r"github\.com/langchain-ai/langgraph"}) == {
        "regex": "passed"
    }
    assert _statuses(answer, {"regex": [r"\d+(\.\d+)?k stars", r"forks"]}) == {"regex": "failed"}


def test_item_counts() -> None:
    assert _statuses(RU_LIST, {"items": 5, "min_items": 3, "max_items": 5}) == {
        "items": "passed",
        "min_items": "passed",
        "max_items": "passed",
    }
    assert _statuses(RU_TABLE, {"items": 3}) == {"items": "failed"}


def test_number_bounds() -> None:
    assert _statuses(RU_LIST, {"numbers_at_most": 120000, "numbers_at_least": 1000}) == {
        "numbers_at_most": "passed",
        "numbers_at_least": "passed",
    }
    (check,) = grade_answer(RU_LIST, {"numbers_at_most": 10000}).checks
    assert check.status == "failed"
    assert "12990.0" in check.message


def test_an_unknown_key_is_ungraded_and_never_fails() -> None:
    grade = grade_answer(RU_TABLE, {"items": 2, "fields": ["price", "memory"]})

    assert _statuses(RU_TABLE, {"items": 2, "fields": ["price"]}) == {
        "items": "passed",
        "fields": "ungraded",
    }
    assert grade.passed is True
    assert grade.status == "passed"


def test_only_ungraded_keys_make_an_ungraded_grade() -> None:
    grade = grade_answer("anything", {"fields": ["stars"]})

    assert grade.passed is True
    assert grade.status == "ungraded"


def test_number_bounds_need_a_number() -> None:
    (check,) = grade_answer("Ничего не найдено.", {"numbers_at_most": 120000}).checks

    assert (check.status, check.message) == ("failed", "the answer has no numbers")


def test_an_empty_expected_mapping_is_ungraded() -> None:
    assert grade_answer("anything", {}).status == "ungraded"


@pytest.mark.parametrize(
    "expected",
    [
        {"contains": []},
        {"contains": ""},
        {"not_contains": [""]},
        {"regex": ""},
        {"regex": "("},
        {"items": "five"},
        {"items": -1},
        {"numbers_at_most": "cheap"},
        {"contains": 5},
    ],
)
def test_a_malformed_expectation_fails_with_a_reason(expected: dict[str, Any]) -> None:
    (check,) = grade_answer("1. a", expected).checks

    assert check.status == "failed"
    assert check.message.startswith("invalid expectation")


def test_a_grade_serializes_its_checks() -> None:
    grade = grade_answer("Python", {"contains": "python", "fields": ["x"]})

    assert grade.to_json_dict() == {
        "status": "passed",
        "checks": [
            {"key": "contains", "status": "passed", "message": ""},
            {"key": "fields", "status": "ungraded", "message": "no deterministic grader for this key"},
        ],
    }


def test_the_golden_set_uses_the_grader_vocabulary() -> None:
    """Every golden ``expected`` key is graded, except ``fields`` (Q7: no judge yet)."""

    golden = Path(__file__).parent / "golden" / "tasks.jsonl"
    for scenario in load_batch_scenarios(golden):
        for check in grade_answer("", scenario.expected).checks:
            assert not check.message.startswith("invalid expectation"), (scenario.scenario_id, check)
            assert check.status != "ungraded" or check.key == "fields", (scenario.scenario_id, check)


def test_the_golden_price_check_reads_ruble_spellings_but_not_words() -> None:
    golden = Path(__file__).parent / "golden" / "tasks.jsonl"
    (laptops,) = [s for s in load_batch_scenarios(golden) if s.scenario_id == "ozon-gaming-laptops"]
    pattern = laptops.expected["regex"]

    assert re.search(pattern, "A — 74 990 р.\nB — 80 000 RUB\nC — 90 000 Руб.")
    assert re.search(pattern, "A — 1 ₽\nB — 2 рублей\nC — 3 руб")
    assert not re.search(pattern, "Куртка-рубашка 1\nКуртка-рубашка 2\nКуртка-рубашка 3")
