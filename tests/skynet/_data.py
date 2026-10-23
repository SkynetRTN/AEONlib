import json
from pathlib import Path
from typing import Any

DATA = Path(__file__).parent / "data"
STATES = (
    "PENDING",
    "SCHEDULED",
    "IN_PROGRESS",
    "COMPLETED",
    "PARTIALLY_COMPLETED",
    "CANCELED",
    "WINDOW_EXPIRED",
)


def load(name: str) -> Any:
    return json.loads((DATA / name).read_text())
