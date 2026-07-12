"""
graph_mermaid.py

Показывает, как сгенерировать mermaid‑код графа агента
и удобно задать целевое место хранения файла.
"""

import argparse
from pathlib import Path
from typing import Optional

from src.agent.agent import AgentWorkflow
from langchain_core.language_models.fake import FakeListLLM
from langchain_core.tools import tool

# ------------------------------------------------------------------
# 1)  Snake‑case helper: если путь уже существует – переименовать
# ------------------------------------------------------------------
def target_path(proposed: Path, suffix: str = ".mmd") -> Path:
    """Если файл существует, добавляем суффикс _1, _2… перед расширением."""
    if not proposed.exists():
        return proposed

    base = proposed.stem
    ext = proposed.suffix or suffix
    n = 1
    while True:
        new = proposed.with_name(f"{base}_{n}{ext}")
        if not new.exists():
            return new
        n += 1


# ------------------------------------------------------------------
# 2)  Генерация mermaid‑кода (синтетический современный вариант)
# ------------------------------------------------------------------
def draw_mermaid(graph, *, hide_edges: bool = False) -> str:
    """Самый простой "упаковщик": просто делаем mermaid из графа."""
    raw = graph.get_graph().draw_mermaid()
    if hide_edges:
        # убираем подписи к стрелкам (практический hack)
        import re

        raw = re.sub(r"(\w+)--\[.*?\]->(\w+)", r"\1-->\\2", raw)
    return raw


# ------------------------------------------------------------------
# 3)  Фактическая сборка графа агента
# ------------------------------------------------------------------
mock_llm = FakeListLLM(responses=[
    "Привет! Я тестовый агент.", "Выполняю задачу..."
])

@tool
def mock_tool(query: str) -> str:
    """Тестовый инструмент для проверки графа."""
    return f"Результат теста для: {query}"

workflow = AgentWorkflow(mock_llm, [mock_tool])
mermaid_code = draw_mermaid(workflow.graph, hide_edges=True)


# ------------------------------------------------------------------
# 4)  Задаём/получаем путь к выходному файлу через argparse
# ------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Сгенерировать mermaid‑код графа агента и сохранить."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("extra/graph.mmd"),
        help="Файл для записи mermaid‑кода (можно указать путь и/или имя).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    out_file = target_path(args.output.resolve())
    out_file.parent.mkdir(parents=True, exist_ok=True)

    # Сохраняем
    out_file.write_text(mermaid_code, encoding="utf-8")
    print(f"[✅] Mermaid‑код записан в {out_file}")

    # При желании — печатаем в консоль
    print("\n--- MERMAID КОД ---")
    print(mermaid_code)


if __name__ == "__main__":
    main()
