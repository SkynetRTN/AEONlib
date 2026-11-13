import pytest

from aeonlib.skynet.pools import InstrumentPools

from ._data import ADA, ADA_GRANT, NO_SUCH_GRANT, TEAM, TEAM_GRANT, load


def test_pools_resolve_what_a_request_needs():
    pools = InstrumentPools.model_validate(load("instrument_pools.json"))
    pool = pools.pool(ADA_GRANT)
    assert pools.account(pool).owner_uid == ADA
    assert [t.slug for t in pools.pool_telescopes(pool)] == [
        "sim-optical-atacama",
        "sim-optical-karoo",
        "sim-optical-outback",
    ]
    assert len(pool.filter_instrument_uids) == 12
    assert pools.filters["r'"].central_wavelength_nm is not None
    assert pool.limits.max_target_snr == 10.0


def test_a_grant_the_user_cannot_spend_has_no_pool():
    pools = InstrumentPools.model_validate(load("instrument_pools.json"))
    with pytest.raises(KeyError):
        pools.pool(NO_SUCH_GRANT)


def test_one_pool_reaches_a_non_sim_telescope():
    pools = InstrumentPools.model_validate(load("instrument_pools.json"))
    slugs = {t.slug for t in pools.pool_telescopes(pools.pool(TEAM_GRANT))}
    assert any(not s.startswith("sim-") for s in slugs)


def test_a_pool_names_its_accounts_owner():
    pools = InstrumentPools.model_validate(load("instrument_pools.json"))
    own, team = pools.owner(pools.pool(ADA_GRANT)), pools.owner(pools.pool(TEAM_GRANT))
    assert own is not None and (own.uid, own.entity_type) == (ADA, "user")
    assert team is not None and (team.uid, team.entity_type) == (TEAM, "organization")
