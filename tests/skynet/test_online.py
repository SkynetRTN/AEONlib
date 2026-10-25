"""
These tests run against a real Skynet API. To run them, you must run the
"online" mark explicitly: pytest -m online.

Set AEON_SKYNET_TOKEN and AEON_SKYNET_API_ROOT (for example
http://127.0.0.1:5001/ for a local development stack). The token's user needs
a spendable observing grant; one whose telescopes are all simulators (slugs
starting "sim-") is preferred. The side_effect tests submit observations and
cancel them.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aeonlib.models import SiderealTarget, Window
from aeonlib.skynet import (
    TERMINAL_STATES,
    FilterExposure,
    InstrumentPool,
    SkynetFacility,
    SkynetImagingRequest,
    SourceModel,
)

pytestmark = pytest.mark.online


@pytest.fixture
def skynet() -> SkynetFacility:
    return SkynetFacility()


def test_instrument_pools(skynet: SkynetFacility):
    pools = skynet.instrument_pools()
    assert pools.pools, "the token's user holds no spendable optical grant"
    for pool in pools.pools:
        assert pools.account(pool).owner_id in pools.entities
        for telescope in pools.pool_telescopes(pool):
            assert -90 <= telescope.latitude_deg <= 90


def sim_pool(skynet: SkynetFacility) -> InstrumentPool:
    """A pool with filters, preferring one that reaches simulators only"""
    pools = skynet.instrument_pools()
    usable = [p for p in pools.pools if p.filter_instrument_ids]
    assert usable, "the token's user holds no grant with filters"
    for pool in usable:
        if all(t.slug.startswith("sim-") for t in pools.pool_telescopes(pool)):
            return pool
    return usable[0]


def request_for(pool: InstrumentPool, **overrides: Any) -> SkynetImagingRequest:
    filter_id = min(pool.filter_instrument_ids)
    now = datetime.now(UTC).replace(microsecond=0)
    fields: dict[str, Any] = {
        "name": "AEONlib online test",
        "grant_id": pool.grant_id,
        "target": SiderealTarget(name="AEONlib test", type="ICRS", ra=150.0, dec=-30.0),
        "window": Window(start=now, end=now + timedelta(days=1)),
        "exposures": [FilterExposure(filter_id=filter_id, target_snr=5, count=1)],
        "source": SourceModel(
            magnitude=15.0,
            filter_id=filter_id,
            measured_at=now - timedelta(hours=1),
            event_time=now - timedelta(hours=5),
            decay_index=1.0,
            spectral_index=0.5,
        ),
    }
    fields.update(overrides)
    return SkynetImagingRequest(**fields)


def test_validate(skynet: SkynetFacility):
    result = skynet.validate(request_for(sim_pool(skynet)))
    assert result.valid, result.errors


def test_validate_names_a_filter_the_pool_lacks(skynet: SkynetFacility):
    pool = sim_pool(skynet)
    request = request_for(
        pool, exposures=[FilterExposure(filter_id="no-such-filter", target_snr=5)]
    )
    result = skynet.validate(request)
    assert not result.valid
    assert list(result.errors) == ["exposures.0.filter_id"]


def test_instrument_preview(skynet: SkynetFacility):
    pool = sim_pool(skynet)
    previews = skynet.instrument_preview(request_for(pool))
    assert previews, "no instrument in the pool offers the filter"
    for preview in previews:
        assert preview.instrument_id in pool.instrument_ids
        assert preview.telescope is not None
        if preview.compatible:
            assert preview.observable is not None
            assert preview.checked_until is not None
            assert preview.plan is not None and preview.plan.total_exposures >= 1
            assert all(w.observable for w in preview.windows)
        else:
            assert preview.reasons


@pytest.mark.side_effect
def test_submit_then_read_progress(skynet: SkynetFacility):
    submitted = skynet.submit(request_for(sim_pool(skynet)))
    try:
        state = skynet.state(submitted.id)
        assert state.state in ("PENDING", "SCHEDULED", "IN_PROGRESS")
        assert skynet.data_products(submitted.id) == []
    finally:
        skynet.cancel(submitted.id)
    assert skynet.state(submitted.id).state in TERMINAL_STATES


@pytest.mark.side_effect
def test_submit_a_cadence_then_cancel(skynet: SkynetFacility):
    now = datetime.now(UTC).replace(microsecond=0)
    request = request_for(
        sim_pool(skynet),
        name="AEONlib online cadence test",
        window=Window(start=now, end=now + timedelta(days=3)),
        epoch_count=2,
        cadence_period=86400.0,
    )
    submitted = skynet.submit(request)
    skynet.cancel(submitted.id)
    assert skynet.state(submitted.id).state == "CANCELED"
