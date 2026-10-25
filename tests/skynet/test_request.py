from datetime import UTC, datetime
from math import log
from typing import Any

import astropy.units as u
import pytest
from astropy.coordinates import Angle
from pydantic import ValidationError

from aeonlib.models import SiderealTarget, Window
from aeonlib.skynet.request import (
    FilterExposure,
    SkynetImagingRequest,
    SourceModel,
    request_field,
)

from ._data import load


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def target() -> SiderealTarget:
    return SiderealTarget(name="AT 2026abc", type="ICRS", ra=150.025, dec=-32.4)


def window() -> Window:
    return Window(start=utc(2026, 11, 1), end=utc(2026, 11, 8))


MEASURED_AT = utc(2026, 10, 31, 6)
EVENT_TIME = utc(2026, 10, 31, 2)


def afterglow(
    measured_at: datetime = MEASURED_AT,
    event_time: datetime = EVENT_TIME,
) -> SourceModel:
    """r' = 17.8 AB four hours after the event, alpha 1.2, beta 0.7"""
    return SourceModel(
        magnitude=17.8,
        filter_id="r'",
        measured_at=measured_at,
        event_time=event_time,
        decay_index=1.2,
        spectral_index=0.7,
    )


def snr_request(**overrides: Any) -> SkynetImagingRequest:
    fields: dict[str, Any] = {
        "name": "AT 2026abc follow-up",
        "grant_id": 99,
        "target": target(),
        "window": window(),
        "exposures": [
            FilterExposure(filter_id="r'", target_snr=10, count=3),
            FilterExposure(filter_id="i'", target_snr=10, count=3),
        ],
        "source": afterglow(),
        "max_airmass": 2.0,
    }
    fields.update(overrides)
    return SkynetImagingRequest(**fields)


class TestPublishBody:
    def test_snr_request_matches_the_contract(self):
        assert snr_request().to_publish_body(owner_id=133) == load("publish_snr.json")

    def test_exposure_request_matches_the_contract(self):
        request = SkynetImagingRequest(
            name="AT 2026abc fixed exposure",
            grant_id=99,
            target=target(),
            window=window(),
            exposures=[FilterExposure(filter_id="V", total_exposure_time=600)],
        )
        assert request.to_publish_body(owner_id=133) == load("publish_exposure.json")

    def test_cadence_request_matches_the_contract(self):
        request = SkynetImagingRequest(
            name="AT 2026abc nightly",
            grant_id=100,
            target=target(),
            window=window(),
            exposures=[FilterExposure(filter_id="r'", target_snr=5, count=2)],
            source=afterglow(),
            min_moon_separation=30,
            epoch_count=4,
            cadence_period=86400,
        )
        assert request.to_publish_body(owner_id=134) == load("publish_cadence.json")

    def test_one_epoch_sends_no_cadence(self):
        data = snr_request().to_publish_body(owner_id=1)["spec"]["data"]
        assert not {"epochCount", "cadenceKind", "cadencePeriodSec"} & set(data)

    def test_exponential_decay_moves_the_magnitude_to_the_event(self):
        # Four hours after T0 with rate 1 and a two-hour timescale the source
        # has faded by e^2, so at T0 it was 2.5 * 2 / ln 10 mag brighter
        source = SourceModel(
            magnitude=17.8,
            filter_id="r'",
            measured_at=MEASURED_AT,
            event_time=EVENT_TIME,
            decay_index=1.0,
            decay="exponential",
            decay_timescale=7200.0,
            spectral_index=0.7,
        )
        body = snr_request(source=source).to_publish_body(owner_id=1)
        model = body["spec"]["data"]["opticalImagingConfiguration"]["brightnessModel"]
        assert model["magnitudeMag"] == pytest.approx(17.8 - 5 / log(10))
        assert model["temporalComponent"] == {
            "temporalComponentType": "exponential",
            "index": 1.0,
            "eventTime": "2026-10-31T02:00:00Z",
            "referenceTimeSec": 7200.0,
        }

    def test_reference_time_is_measured_from_the_event(self):
        source = afterglow(measured_at=utc(2026, 10, 31, 3))
        body = snr_request(source=source).to_publish_body(owner_id=1)
        model = body["spec"]["data"]["opticalImagingConfiguration"]["brightnessModel"]
        assert model["temporalComponent"]["referenceTimeSec"] == 3600.0

    @pytest.mark.parametrize(("airmass", "elevation"), [(2.0, 30.0), (1.5, 41.810315)])
    def test_max_airmass_becomes_min_elevation(self, airmass: float, elevation: float):
        body = snr_request(max_airmass=airmass).to_publish_body(owner_id=1)
        config = body["spec"]["data"]["opticalImagingConfiguration"]
        assert config["minElevationDeg"] == pytest.approx(elevation)

    def test_unset_options_are_left_out(self):
        body = snr_request(max_airmass=None).to_publish_body(owner_id=1)
        config = body["spec"]["data"]["opticalImagingConfiguration"]
        assert "minElevationDeg" not in config
        assert "minMoonSeparationDeg" not in config

    @pytest.mark.parametrize(
        ("separation", "degrees"),
        [
            (30.0, 30.0),
            (30, 30.0),
            ("30d", 30.0),
            (Angle(30, unit="deg"), 30.0),
            (30 * u.deg, 30.0),
            (90 * u.arcmin, 1.5),
        ],
    )
    def test_moon_separation_accepts_any_angle(self, separation: Any, degrees: float):
        body = snr_request(min_moon_separation=separation).to_publish_body(owner_id=1)
        config = body["spec"]["data"]["opticalImagingConfiguration"]
        assert config["minMoonSeparationDeg"] == pytest.approx(degrees)
        assert isinstance(config["minMoonSeparationDeg"], float)

    def test_subsecond_times_keep_their_fraction(self):
        start = datetime(2026, 11, 1, 0, 0, 0, 500000, tzinfo=UTC)
        request = snr_request(window=Window(start=start, end=utc(2026, 11, 8)))
        data = request.to_publish_body(owner_id=1)["spec"]["data"]
        assert data["startAfter"] == "2026-11-01T00:00:00.500000Z"


class TestRequestValidation:
    def test_non_icrs_targets_are_refused(self):
        hour_angle = SiderealTarget(type="HOUR_ANGLE", ra=1, dec=2, hour_angle=3)
        with pytest.raises(ValidationError, match="ICRS"):
            snr_request(target=hour_angle)

    def test_the_window_needs_a_start(self):
        with pytest.raises(ValidationError, match="window.start is required"):
            snr_request(window=Window(end=utc(2026, 11, 8)))

    def test_the_window_must_be_open(self):
        with pytest.raises(ValidationError, match="before window.end"):
            snr_request(window=Window(start=utc(2026, 11, 8), end=utc(2026, 11, 8)))

    def test_snr_needs_a_source(self):
        with pytest.raises(ValidationError, match="source brightness model"):
            snr_request(source=None)

    def test_the_event_must_precede_the_window(self):
        late = afterglow(measured_at=utc(2026, 11, 3), event_time=utc(2026, 11, 2))
        with pytest.raises(ValidationError, match="event_time must be before"):
            snr_request(source=late)

    def test_the_measurement_must_follow_the_event(self):
        with pytest.raises(ValidationError, match="measured_at must be after"):
            SourceModel(
                magnitude=17.8,
                filter_id="r'",
                measured_at=utc(2026, 10, 31, 2),
                event_time=utc(2026, 10, 31, 2),
                decay_index=1.2,
                spectral_index=0.7,
            )

    @pytest.mark.parametrize(
        "fields",
        [
            {"filter_id": "V"},
            {"filter_id": "V", "target_snr": 5, "total_exposure_time": 60},
        ],
    )
    def test_exactly_one_exposure_mode(self, fields: dict[str, Any]):
        with pytest.raises(ValidationError, match="exactly one"):
            FilterExposure(**fields)

    @pytest.mark.parametrize("separation", [-1.0, 181.0, -1 * u.deg, "200d"])
    def test_moon_separation_is_between_0_and_180_degrees(self, separation: Any):
        with pytest.raises(ValidationError, match="between 0 and 180"):
            snr_request(min_moon_separation=separation)

    @pytest.mark.parametrize("airmass", [1.0, 0.5])
    def test_max_airmass_is_above_one(self, airmass: float):
        with pytest.raises(ValidationError):
            snr_request(max_airmass=airmass)

    def test_at_least_one_exposure(self):
        with pytest.raises(ValidationError):
            snr_request(exposures=[])

    def test_an_exposure_only_request_needs_no_source(self):
        request = snr_request(
            exposures=[FilterExposure(filter_id="V", total_exposure_time=60)],
            source=None,
        )
        assert request.source is None

    @pytest.mark.parametrize("fields", [{"epoch_count": 3}, {"cadence_period": 3600.0}])
    def test_cadence_needs_both_epochs_and_a_period(self, fields: dict[str, Any]):
        with pytest.raises(ValidationError, match="cadence_period"):
            snr_request(**fields)

    @pytest.mark.parametrize(
        "fields", [{"decay": "exponential"}, {"decay_timescale": 3600.0}]
    )
    def test_exponential_decay_needs_a_timescale(self, fields: dict[str, Any]):
        with pytest.raises(ValidationError, match="decay_timescale"):
            SourceModel.model_validate(afterglow().model_dump() | fields)


# Body fields to_publish_body fills with fixed values
FIXED = {
    "spec.schemaKind",
    "spec.schemaVersion",
    "spec.data.copiesMode",
    "spec.data.instrumentMode",
    "spec.data.cadenceKind",
    "spec.data.cadenceLevel",
    "spec.data.requests.*.requestType",
    "spec.data.requests.*.order",
    "spec.data.opticalImagingConfiguration.trackingMode",
    "spec.data.opticalImagingConfiguration.ditherStrategy",
    "spec.data.opticalImagingConfiguration.temporalOffsetSec",
    "spec.data.opticalImagingConfiguration.maxTiles",
    "spec.data.opticalImagingConfiguration.tileOverlap",
    "spec.data.opticalImagingConfiguration.brightnessModel.modelType",
    "spec.data.opticalImagingConfiguration.brightnessModel.temporalComponent.temporalComponentType",
    "spec.data.opticalImagingConfiguration.brightnessModel.spectralComponent.spectralComponentType",
}


def leaves(value: Any, path: tuple[str | int, ...] = ()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from leaves(item, (*path, key))
    elif isinstance(value, list) and value and isinstance(value[0], dict):
        for index, item in enumerate(value):
            yield from leaves(item, (*path, index))
    else:
        yield path


class TestRequestField:
    @pytest.mark.parametrize(
        "name", ["publish_snr.json", "publish_exposure.json", "publish_cadence.json"]
    )
    def test_every_field_the_body_writes_maps_back(self, name: str):
        for path in leaves(load(name)):
            pattern = ".".join("*" if isinstance(p, int) else p for p in path)
            if pattern in FIXED:
                continue
            assert request_field(path) is not None, pattern

    def test_the_recorded_422_names_the_exposure(self):
        (error,) = load("dry_run/validate_error.json")["detail"]
        assert request_field(error["loc"]) == "exposures.0.target_snr"

    @pytest.mark.parametrize(
        ("loc", "field"),
        [
            (
                [
                    "body",
                    "spec",
                    "data",
                    "requests",
                    1,
                    "optical_imaging",
                    "sampleCount",
                ],
                "exposures.1.count",
            ),
            (
                ["requests", 2, "optical_imaging", "filterSpecifierIds", 0],
                "exposures.2.filter_id",
            ),
            (
                ["target", "position", "fixed", "coordinates", "equatorial", "decDeg"],
                "target",
            ),
            (
                [
                    "opticalImagingConfiguration",
                    "brightnessModel",
                    "temporalComponent",
                    "power_law",
                    "index",
                ],
                "source.decay_index",
            ),
            (["cancelOn"], "window.end"),
            (["opticalImagingConfiguration", "trackingMode"], None),
            (["somethingElse"], None),
        ],
    )
    def test_locations(self, loc: list[str | int], field: str | None):
        assert request_field(loc) == field
