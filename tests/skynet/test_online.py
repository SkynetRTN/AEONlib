"""
These tests run against a real Skynet API. To run them, you must run the
"online" mark explicitly: pytest -m online.

Set AEON_SKYNET_TOKEN and AEON_SKYNET_API_ROOT (for example
http://127.0.0.1:5001/ for a local development stack). The token's user needs
a spendable observing grant. The side_effect test submits an observation and
cancels it.
"""

from datetime import UTC, datetime, timedelta

import pytest

from aeonlib.models import SiderealTarget, Window
from aeonlib.skynet import (
    TERMINAL_STATES,
    FilterExposure,
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


@pytest.mark.side_effect
def test_submit_then_read_progress(skynet: SkynetFacility):
    pools = skynet.instrument_pools()
    pool = next(p for p in pools.pools if p.filter_instrument_ids)
    filter_id = min(pool.filter_instrument_ids)
    now = datetime.now(UTC).replace(microsecond=0)
    request = SkynetImagingRequest(
        name="AEONlib online test",
        grant_id=pool.grant_id,
        target=SiderealTarget(name="AEONlib test", type="ICRS", ra=150.0, dec=-30.0),
        window=Window(start=now, end=now + timedelta(days=1)),
        exposures=[FilterExposure(filter_id=filter_id, target_snr=5, count=1)],
        source=SourceModel(
            magnitude=15.0,
            filter_id=filter_id,
            measured_at=now - timedelta(hours=1),
            event_time=now - timedelta(hours=5),
            decay_index=1.0,
            spectral_index=0.5,
        ),
    )
    submitted = skynet.submit(request)
    try:
        state = skynet.state(submitted.id)
        assert state.state in ("PENDING", "SCHEDULED", "IN_PROGRESS")
        assert skynet.data_products(submitted.id) == []
    finally:
        # Cancel directly until the facility supports it
        skynet._request(
            "PATCH", f"v1/observations/{submitted.id}", json={"status": "canceled"}
        )
    assert skynet.state(submitted.id).state in TERMINAL_STATES
