import copy
import json
from typing import Any

import httpx
import pytest

from aeonlib.skynet.facility import SkynetError
from aeonlib.skynet.request import FilterExposure

from ._data import load
from .test_facility import POOLS, Recorder, facility
from .test_request import snr_request

VALIDATE = ("POST", "/v1/observation-specs:validate")
COMPATIBLE = ("POST", "/v1/observation-specs:compatible-instruments")
OBSERVABILITY = ("POST", "/v1/observation-specs:observability")
PLAN = ("POST", "/v1/observation-specs:plan")


def compatible(incompatible: frozenset[int] = frozenset()):
    """Answer :compatible-instruments for the instruments asked, from the recordings"""
    ok = load("dry_run/compatible_instruments.json")["results"][0]
    refused = load("dry_run/compatible_instruments_incompatible.json")["results"][0]

    def answer(request: httpx.Request) -> httpx.Response:
        ids = json.loads(request.content)["instrumentIds"]
        results = [
            {**(refused if i in incompatible else ok), "instrumentId": i} for i in ids
        ]
        return httpx.Response(200, json={"results": results})

    return answer


def observability(observable: frozenset[int]):
    """The recorded windows (none observable), with one made observable for some instruments"""
    recorded = load("dry_run/observability.json")

    def answer(request: httpx.Request) -> httpx.Response:
        instrument_id = json.loads(request.content)["instrumentId"]
        body = copy.deepcopy(recorded)
        for window in body["windows"]:
            window["instrumentId"] = instrument_id
        if instrument_id in observable:
            body["windows"][0]["observable"] = True
            body["windows"][0]["reason"] = None
        return httpx.Response(200, json=body)

    return answer


def plan(totals: dict[int, tuple[int, float]]):
    recorded = load("dry_run/plan.json")

    def answer(request: httpx.Request) -> httpx.Response:
        body = copy.deepcopy(recorded)
        count, seconds = totals[json.loads(request.content)["instrumentId"]]
        body["opticalImagingConfigurationPlan"]["totalExposures"] = count
        body["opticalImagingConfigurationPlan"]["totalExposureTimeSec"] = seconds
        return httpx.Response(200, json=body)

    return answer


def preview_routes() -> dict[tuple[str, str], Any]:
    return {
        POOLS: load("instrument_pools.json"),
        COMPATIBLE: compatible(),
        OBSERVABILITY: observability(frozenset({105, 106, 107})),
        PLAN: plan({105: (6, 12.0), 106: (6, 15.5), 107: (6, 20.0)}),
    }


class TestValidate:
    def test_a_valid_request(self):
        recorder = Recorder({POOLS: load("instrument_pools.json"), VALIDATE: {}})
        result = facility(recorder).validate(snr_request())
        assert result.valid
        assert result.errors == {}
        (sent,) = recorder.sent(*VALIDATE)
        assert json.loads(sent.content) == load("publish_snr.json")["spec"]

    def test_a_422_becomes_field_errors(self):
        recorder = Recorder(
            {
                POOLS: load("instrument_pools.json"),
                VALIDATE: httpx.Response(422, json=load("dry_run/validate_error.json")),
            }
        )
        result = facility(recorder).validate(snr_request())
        assert not result.valid
        assert result.errors == {
            "exposures.0.target_snr": [
                "Input should be a valid number, unable to parse string as a number"
            ]
        }

    def test_a_422_with_a_message_is_a_non_field_error(self):
        recorder = Recorder(
            {
                POOLS: load("instrument_pools.json"),
                VALIDATE: httpx.Response(422, json={"detail": "Unknown schema kind"}),
            }
        )
        result = facility(recorder).validate(snr_request())
        assert result.errors == {"non_field_errors": ["Unknown schema kind"]}

    def test_a_grant_the_user_cannot_spend(self):
        recorder = Recorder({POOLS: load("instrument_pools.json")})
        result = facility(recorder).validate(snr_request(grant_id=12345))
        assert not result.valid
        assert list(result.errors) == ["grant_id"]
        assert recorder.sent(*VALIDATE) == []

    def test_a_filter_the_pool_lacks_names_its_exposure(self):
        recorder = Recorder({POOLS: load("instrument_pools.json")})
        request = snr_request(
            exposures=[
                FilterExposure(filter_id="r'", target_snr=10),
                FilterExposure(filter_id="z'", target_snr=10),
            ]
        )
        result = facility(recorder).validate(request)
        assert list(result.errors) == ["exposures.1.filter_id"]
        assert recorder.sent(*VALIDATE) == []

    def test_other_errors_still_raise(self):
        recorder = Recorder(
            {
                POOLS: load("instrument_pools.json"),
                VALIDATE: httpx.Response(500, json={"detail": "boom"}),
            }
        )
        with pytest.raises(SkynetError, match="500"):
            facility(recorder).validate(snr_request())


class TestInstrumentPreview:
    def test_every_pool_instrument_with_a_requested_filter(self):
        recorder = Recorder(preview_routes())
        previews = facility(recorder).instrument_preview(snr_request())
        (sent,) = recorder.sent(*COMPATIBLE)
        assert json.loads(sent.content)["instrumentIds"] == [105, 106, 107]
        assert [p.instrument_id for p in previews] == [105, 106, 107]
        assert [p.telescope.slug for p in previews if p.telescope] == [
            "sim-optical-atacama",
            "sim-optical-karoo",
            "sim-optical-outback",
        ]
        assert all(p.compatible and p.observable for p in previews)
        assert [p.plan.total_exposure_time for p in previews if p.plan] == [
            12.0,
            15.5,
            20.0,
        ]

    def test_an_incompatible_instrument_has_reasons_and_no_further_checks(self):
        recorder = Recorder(preview_routes())
        recorder.routes[COMPATIBLE] = compatible(frozenset({106}))
        previews = facility(recorder).instrument_preview(snr_request())
        karoo = previews[1]
        assert not karoo.compatible
        assert karoo.reasons
        assert karoo.observable is None and karoo.plan is None
        asked = [json.loads(r.content)["instrumentId"] for r in recorder.sent(*PLAN)]
        assert asked == [105, 107]

    def test_an_instrument_with_no_observable_window(self):
        recorder = Recorder(preview_routes())
        recorder.routes[OBSERVABILITY] = observability(frozenset({105}))
        previews = facility(recorder).instrument_preview(snr_request())
        assert [p.observable for p in previews] == [True, False, False]
        assert previews[0].windows and not previews[1].windows

    def test_named_instruments_only(self):
        recorder = Recorder(preview_routes())
        previews = facility(recorder).instrument_preview(
            snr_request(), instrument_ids=[106]
        )
        assert [p.instrument_id for p in previews] == [106]

    def test_the_span_is_the_window_up_to_a_week(self):
        recorder = Recorder(preview_routes())
        facility(recorder).instrument_preview(snr_request(), instrument_ids=[105])
        (sent,) = recorder.sent(*OBSERVABILITY)
        body = json.loads(sent.content)
        assert body["start"] == "2026-11-01T00:00:00Z"
        assert body["spanDays"] == pytest.approx(7.0)
        assert body["resolutionSec"] == 300.0

    def test_a_long_window_is_checked_partly(self):
        from aeonlib.models import Window

        from .test_request import utc

        request = snr_request(
            window=Window(start=utc(2026, 11, 1), end=utc(2026, 12, 1))
        )
        recorder = Recorder(preview_routes())
        (preview,) = facility(recorder).instrument_preview(
            request, instrument_ids=[105]
        )
        assert preview.checked_until is not None
        assert preview.checked_until.isoformat() == "2026-11-08T00:00:00+00:00"

    def test_plan_is_asked_for_the_window(self):
        recorder = Recorder(preview_routes())
        facility(recorder).instrument_preview(snr_request(), instrument_ids=[105])
        (sent,) = recorder.sent(*PLAN)
        body = json.loads(sent.content)
        assert (body["startsOn"], body["endsOn"]) == (
            "2026-11-01T00:00:00Z",
            "2026-11-08T00:00:00Z",
        )
        assert body["spec"] == load("publish_snr.json")["spec"]

    def test_an_invalid_spec_raises(self):
        recorder = Recorder(preview_routes())
        recorder.routes[COMPATIBLE] = httpx.Response(
            422, json=load("dry_run/validate_error.json")
        )
        with pytest.raises(SkynetError, match="422"):
            facility(recorder).instrument_preview(snr_request())

    def test_a_grant_the_user_cannot_spend_raises(self):
        recorder = Recorder(preview_routes())
        with pytest.raises(ValueError, match="not one of your spendable"):
            facility(recorder).instrument_preview(snr_request(grant_id=12345))
