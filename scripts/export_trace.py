#!/usr/bin/env python3
"""
Export LangSmith traces as readable trees.

Requirements:
    pip install -U langsmith

Environment:
    LANGSMITH_API_KEY=...
"""

from pathlib import Path
import json
from dotenv import load_dotenv
from langsmith import Client

load_dotenv()

PROJECT = "MyFirstApp"
LIMIT = 20

OUTPUT = Path("traces")
OUTPUT.mkdir(exist_ok=True)


client = Client()

run = client.read_run(
    "<RUN_ID>",
    load_child_runs=True,
)

with open("trace.json", "w", encoding="utf-8") as f:
    json.dump(
        run.model_dump(mode="json"),
        f,
        ensure_ascii=False,
        indent=2,
    )