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
    return json.loads((DATA / name).read_text(encoding="utf-8"))


# The recorded fixtures' uids (instrument_pools.json): Ada owns an account
# reaching the three simulators and holds a member grant on the Transient
# team organization's account, which reaches one simulator and PROMPT-CTIO-6
ADA = "12c9fd37-3447-48ad-930d-cf93426d6f48"
TEAM = "9b54d405-3a59-4419-a6c9-9ce1b917dca2"
ADA_GRANT = "1f6a1aa3-6f7c-4eba-ac92-1e7dbfce7a51"
TEAM_GRANT = "b6efc2ac-7aee-4eee-a91c-b20c80f020fa"
ADA_ACCOUNT = "dd471598-31ec-49ad-9f51-6c89b7eb89ff"
TEAM_ACCOUNT = "41c4de53-5294-49e8-a4be-0c9c437362e2"
ATACAMA = "0c1fd16e-ec67-46bd-be3e-a49a06128d07"
KAROO = "9093cc49-c7fd-4c2f-9998-4e05a30ba03c"
OUTBACK = "e0c5e55e-75da-4885-ad63-eeb6a288a1af"
PROMPT = "2425648d-94de-47b5-95b4-651fdbcd8d45"
#: Ada's pool, in pool order
SIMS = [ATACAMA, KAROO, OUTBACK]
NO_SUCH_GRANT = "00000000-0000-4000-8000-000000000000"
