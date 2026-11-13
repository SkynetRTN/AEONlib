from datetime import datetime
from typing import Literal

from pydantic import Field

from ._response import _SkynetResponse

SkynetProgressState = Literal[
    "pending",
    "scheduled",
    "in_progress",
    "completed",
    "partially_completed",
    "canceled",
    "window_expired",
]


class ProgressProduct(_SkynetResponse):
    """A downloadable science file"""

    asset_uid: str
    file_id: str
    filename: str
    is_reduced: bool = False
    is_final: bool = False
    """A reduced product, or a raw frame no reduced product will replace"""
    processing_flags: list[str] = Field(default_factory=list)
    root_asset_uid: str | None = None
    task_uid: str | None = None
    instrument_uid: str | None = None
    telescope_uid: str | None = None
    filter_id: str | None = None
    exposure_time_sec: float | None = None
    taken_at_start_utc: datetime | None = None
    airmass: float | None = None
    pixel_scale_arcsec_per_px: float | None = None


class ObservationProgress(_SkynetResponse):
    """An observation's state, schedule and downloadable science files"""

    uid: str
    status: str
    """Skynet status: active, completed or canceled"""
    state: SkynetProgressState
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    progress_fraction: float = 0.0
    result_counts: dict[str, int] = Field(default_factory=dict)
    products: list[ProgressProduct] = Field(default_factory=list)
    products_final: bool = False
    """The state is terminal and no more products will be added"""
