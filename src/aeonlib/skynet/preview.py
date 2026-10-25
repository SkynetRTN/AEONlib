from datetime import datetime

from pydantic import BaseModel, Field

from ._response import _SkynetResponse
from .pools import Telescope


class SpecValidation(BaseModel):
    """Whether Skynet accepts a request. Says nothing about which instruments can run it."""

    valid: bool
    errors: dict[str, list[str]] = Field(default_factory=dict)
    """Request field path, such as "exposures.0.target_snr", to messages. Errors
    that point at no field are under "non_field_errors"."""


class ObservabilityWindow(_SkynetResponse):
    starts_on: datetime
    ends_on: datetime
    observable: bool
    reason: str | None = None


class PlanTotals(BaseModel):
    """Skynet's estimate for the whole request on one instrument"""

    total_exposures: int
    total_exposure_time: float
    """Seconds of exposure, not wall-clock time"""


class InstrumentPreview(BaseModel):
    """How one instrument would carry out a request"""

    instrument_id: int
    telescope: Telescope | None = None
    compatible: bool
    reasons: list[str] = Field(default_factory=list)
    """Why the instrument is incompatible"""
    observable: bool | None = None
    """Whether any observable window was found. None when not checked (incompatible)."""
    windows: list[ObservabilityWindow] = Field(default_factory=list)
    """The observable windows found"""
    checked_until: datetime | None = None
    """End of the span checked. Before the request window's end, the check is partial."""
    plan: PlanTotals | None = None
    """None when not checked (incompatible) or when Skynet returned no plan"""


class _Compatibility(_SkynetResponse):
    instrument_id: int
    compatible: bool
    reasons: list[str] = Field(default_factory=list)


class _CompatibilityResponse(_SkynetResponse):
    results: list[_Compatibility] = Field(default_factory=list)


class _ObservabilityResponse(_SkynetResponse):
    windows: list[ObservabilityWindow] = Field(default_factory=list)


class _Plan(_SkynetResponse):
    total_exposures: int
    total_exposure_time_sec: float


class _PlanResponse(_SkynetResponse):
    optical_imaging_configuration_plan: _Plan | None = None
