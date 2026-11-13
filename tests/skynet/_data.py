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


# The recorded fixtures' uids (instrument_pools.json): Ada owns an account
# reaching the three simulators and holds a member grant on the Transient
# team organization's account, which reaches one simulator and PROMPT-CTIO-6
ADA = "50484ebe-5f22-4e78-b71f-0743cfcfc8a7"
TEAM = "55305e4c-005e-4cab-928f-e1e2ed1d70f2"
ADA_GRANT = "dd9463a0-b43e-4d0f-8def-90c688558d3c"
TEAM_GRANT = "b394f88c-73b5-4bf2-ae29-fa2c1d222c98"
ADA_ACCOUNT = "839163a6-4dc9-4e29-b60d-f6126399440e"
TEAM_ACCOUNT = "5730745b-e7ae-43e8-acb4-74ac43f00af5"
ATACAMA = "746670bb-4e27-4acb-a307-b6275471020f"
KAROO = "6a94483d-cfce-428f-be47-430dfb7817fd"
OUTBACK = "45265f1b-4b6e-4cb0-9151-336131ee6bcd"
PROMPT = "c248c45d-6a0b-4cf8-b612-bd654f61eaf1"
#: Ada's pool, in pool order
SIMS = [ATACAMA, KAROO, OUTBACK]
NO_SUCH_GRANT = "00000000-0000-4000-8000-000000000000"
