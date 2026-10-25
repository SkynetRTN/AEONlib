from datetime import UTC
from math import asin, degrees, log
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    field_validator,
    model_validator,
)
from pydantic.types import NonNegativeFloat, StringConstraints

from aeonlib.models import SiderealTarget, Window
from aeonlib.types import Angle, Time

from ._response import _SkynetResponse


class FilterExposure(BaseModel):
    """Exposures in one filter, set by target SNR or by total exposure time"""

    model_config: ClassVar[ConfigDict] = ConfigDict(validate_assignment=True)
    filter_id: str
    """A filter the chosen pool offers"""
    target_snr: PositiveFloat | None = None
    """Signal-to-noise ratio to reach. Skynet sizes the exposures per telescope."""
    total_exposure_time: PositiveFloat | None = None
    """Total exposure time in seconds. Skynet splits it into frames."""
    count: PositiveInt = 1
    """Number of samples"""

    @model_validator(mode="after")
    def _one_exposure_mode(self) -> "FilterExposure":
        if (self.target_snr is None) == (self.total_exposure_time is None):
            raise ValueError("Set exactly one of target_snr and total_exposure_time")
        return self


class SourceModel(BaseModel):
    """
    Source brightness used to size exposures for a target SNR. With the default
    power-law decay the flux follows F_nu ∝ (t - T0)^-decay_index * nu^-spectral_index.
    With exponential decay it follows exp(-decay_index * (t - T0) / decay_timescale).
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(validate_assignment=True)
    magnitude: float
    """Reference AB magnitude, without extinction"""
    filter_id: str
    """Filter the reference magnitude was measured in"""
    measured_at: Time
    """When the reference magnitude was measured"""
    event_time: Time
    """T0 of the power-law decay"""
    decay_index: float
    """Temporal decay index: alpha for a power law, the rate for exponential decay"""
    decay: Literal["power_law", "exponential"] = "power_law"
    decay_timescale: PositiveFloat | None = None
    """Exponential decay timescale in seconds. Required for exponential decay only."""
    spectral_index: float
    """Spectral power-law index (beta)"""
    point_source: bool = True
    extinction_av: NonNegativeFloat = 0.0
    """Dust extinction A_V in magnitudes"""
    rv: PositiveFloat = 3.1
    """R_V of the extinction law"""

    @model_validator(mode="after")
    def _measured_after_the_event(self) -> "SourceModel":
        if self.measured_at <= self.event_time:
            raise ValueError("measured_at must be after event_time")
        if (self.decay == "exponential") != (self.decay_timescale is not None):
            raise ValueError(
                "decay_timescale is required for, and only for, exponential decay"
            )
        return self


class SkynetImagingRequest(BaseModel):
    """An optical imaging request. Skynet chooses the telescope."""

    model_config: ClassVar[ConfigDict] = ConfigDict(validate_assignment=True)
    name: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    grant_id: int
    """The observing grant to spend, from `SkynetFacility.instrument_pools`"""
    target: SiderealTarget
    window: Window
    """Both start and end are required"""
    exposures: Annotated[list[FilterExposure], Field(min_length=1)]
    source: SourceModel | None = None
    """Required when any exposure sets a target SNR"""
    max_airmass: Annotated[float, Field(gt=1.0)] | None = None
    min_moon_separation: Angle | None = None
    """Minimum Moon separation"""
    epoch_count: PositiveInt = 1
    """Number of epochs. More than one repeats every exposure each cadence_period."""
    cadence_period: PositiveFloat | None = None
    """Seconds from the start of one epoch to the start of the next"""

    @field_validator("min_moon_separation")
    @classmethod
    def _moon_separation_range(cls, value: Any) -> Any:
        if value is not None and not 0.0 <= value.deg <= 180.0:
            raise ValueError("min_moon_separation must be between 0 and 180 degrees")
        return value

    @model_validator(mode="after")
    def _check(self) -> "SkynetImagingRequest":
        if self.target.type != "ICRS":
            raise ValueError("Skynet accepts ICRS targets only")
        if self.window.start is None:
            raise ValueError("window.start is required")
        if self.window.start >= self.window.end:
            raise ValueError("window.start must be before window.end")
        if any(e.target_snr is not None for e in self.exposures):
            if self.source is None:
                raise ValueError("A target SNR needs a source brightness model")
            if self.source.event_time >= self.window.start:
                raise ValueError("source.event_time must be before window.start")
        if (self.epoch_count > 1) != (self.cadence_period is not None):
            raise ValueError(
                "cadence_period is required for, and only for, more than one epoch"
            )
        return self

    def to_publish_body(self, owner_id: int) -> dict[str, Any]:
        """Build the publish payload. owner_id is the funding account's owner."""
        configuration: dict[str, Any] = {
            "trackingMode": "sidereal",
            "ditherStrategy": "none",
            "temporalOffsetSec": 0.0,
            "maxTiles": 1,
            "tileOverlap": 0.1,
        }
        if self.max_airmass is not None:
            configuration["minElevationDeg"] = round(
                degrees(asin(1.0 / self.max_airmass)), 6
            )
        if self.min_moon_separation is not None:
            configuration["minMoonSeparationDeg"] = float(self.min_moon_separation.deg)
        if self.source is not None:
            configuration["brightnessModel"] = _brightness_model(self.source)

        requests = []
        for order, exposure in enumerate(self.exposures):
            request: dict[str, Any] = {
                "requestType": "optical_imaging",
                "order": order,
                "filterSpecifierIds": [exposure.filter_id],
            }
            if exposure.target_snr is not None:
                request["targetSnr"] = exposure.target_snr
            else:
                request["exposureTimeSec"] = exposure.total_exposure_time
            request["sampleCount"] = exposure.count
            requests.append(request)

        assert self.window.start is not None  # checked in _check
        data = {
            # Skynet's dry-run endpoints need the name inside the spec as well
            "name": self.name,
            "startAfter": _iso(self.window.start),
            "cancelOn": _iso(self.window.end),
            "copiesMode": "single",
            "instrumentMode": "none",
            "target": {
                "name": self.target.name,
                "position": {
                    "positionType": "fixed",
                    "coordinates": {
                        "coordinateType": "equatorial",
                        "raDeg": float(self.target.ra.deg),
                        "decDeg": float(self.target.dec.deg),
                    },
                },
            },
            "opticalImagingConfiguration": configuration,
            "requests": requests,
        }
        if self.cadence_period is not None:
            data["epochCount"] = self.epoch_count
            data["cadenceKind"] = "period"
            data["cadencePeriodSec"] = self.cadence_period
            data["cadenceLevel"] = "epoch"
        return {
            "ownerId": owner_id,
            "observingGrantIds": [self.grant_id],
            "name": self.name,
            "spec": {
                "schemaKind": "observation_spec",
                "schemaVersion": 1,
                "data": data,
            },
        }


def _iso(time: Any) -> str:
    """Format a time as UTC ISO-8601 with a Z suffix"""
    moment = time.utc.to_datetime(timezone=UTC)
    if moment.microsecond == 0:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.isoformat().replace("+00:00", "Z")


def _brightness_model(source: SourceModel) -> dict[str, Any]:
    since_event_sec = float((source.measured_at - source.event_time).to_value("s"))
    magnitude = source.magnitude
    if source.decay == "exponential":
        assert source.decay_timescale is not None  # checked in SourceModel
        # Skynet's exponential term is 1 at T0, so its magnitude is the one at T0
        magnitude -= (
            2.5
            * source.decay_index
            * since_event_sec
            / (source.decay_timescale * log(10))
        )
        reference_time_sec = source.decay_timescale
    else:
        # The power-law term is 1 at t - T0 = referenceTimeSec, so the magnitude
        # holds at the measurement time
        reference_time_sec = since_event_sec
    return {
        "modelType": "source",
        "isPointSource": source.point_source,
        "magnitudeMag": round(magnitude, 6),
        "filterId": source.filter_id,
        "avMag": source.extinction_av,
        "rv": source.rv,
        "temporalComponent": {
            "temporalComponentType": source.decay,
            "index": source.decay_index,
            "eventTime": _iso(source.event_time),
            "referenceTimeSec": round(reference_time_sec, 6),
        },
        "spectralComponent": {
            "spectralComponentType": "power_law",
            "index": source.spectral_index,
        },
    }


class SubmittedObservation(_SkynetResponse):
    """A submitted observation"""

    id: int
    uid: str


# Where each field of the publish body comes from, for mapping Skynet's 422
# error locations back to request fields. Paths are dotted, with "*" for a
# list index; the fixed defaults to_publish_body adds map to no field.
WIRE_FIELDS: dict[str, str] = {
    "name": "name",
    "observingGrantIds": "grant_id",
    "ownerId": "grant_id",
    "startAfter": "window.start",
    "cancelOn": "window.end",
    "target": "target",
    "epochCount": "epoch_count",
    "cadencePeriodSec": "cadence_period",
    "requests.*": "exposures.*",
    "requests.*.filterSpecifierIds": "exposures.*.filter_id",
    "requests.*.targetSnr": "exposures.*.target_snr",
    "requests.*.exposureTimeSec": "exposures.*.total_exposure_time",
    "requests.*.sampleCount": "exposures.*.count",
    "opticalImagingConfiguration.minElevationDeg": "max_airmass",
    "opticalImagingConfiguration.minMoonSeparationDeg": "min_moon_separation",
    "opticalImagingConfiguration.brightnessModel": "source",
    "opticalImagingConfiguration.brightnessModel.magnitudeMag": "source.magnitude",
    "opticalImagingConfiguration.brightnessModel.filterId": "source.filter_id",
    "opticalImagingConfiguration.brightnessModel.isPointSource": "source.point_source",
    "opticalImagingConfiguration.brightnessModel.avMag": "source.extinction_av",
    "opticalImagingConfiguration.brightnessModel.rv": "source.rv",
    "opticalImagingConfiguration.brightnessModel.temporalComponent": "source",
    "opticalImagingConfiguration.brightnessModel.temporalComponent.index": "source.decay_index",
    "opticalImagingConfiguration.brightnessModel.temporalComponent.eventTime": "source.event_time",
    "opticalImagingConfiguration.brightnessModel.temporalComponent.referenceTimeSec": "source",
    "opticalImagingConfiguration.brightnessModel.spectralComponent.index": "source.spectral_index",
}
# Pydantic puts a discriminated union's tag in the location, and for a plain
# union of them a "tagged-union[...]" part naming the branch
_UNION_TAGS = frozenset(
    {
        "optical_imaging",
        "power_law",
        "exponential",
        "blackbody",
        "fixed",
        "equatorial",
        "source",
    }
)


def _is_union_part(part: str | int) -> bool:
    return isinstance(part, str) and (part in _UNION_TAGS or "[" in part)


_ENVELOPE = ("spec", "data")


def request_field(loc: list[str | int] | tuple[str | int, ...]) -> str | None:
    """
    Return the request field a Skynet error location points at, such as
    "exposures.0.target_snr" for ["requests", 0, "optical_imaging", "targetSnr"],
    or None if it isn't one the request sets.
    """
    parts = [p for p in loc if not _is_union_part(p)]
    if parts and parts[0] == "body":
        parts = parts[1:]
    for key in _ENVELOPE:
        if parts and parts[0] == key:
            parts = parts[1:]
    indexes = [p for p in parts if isinstance(p, int)]
    pattern = ".".join("*" if isinstance(p, int) else p for p in parts)
    while pattern:
        field = WIRE_FIELDS.get(pattern)
        if field is not None:
            for index in indexes:
                field = field.replace("*", str(index), 1)
            return field
        pattern, _, last = pattern.rpartition(".")
        if last == "*" and indexes:
            indexes.pop()
    return None
