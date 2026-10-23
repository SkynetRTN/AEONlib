from typing import Literal

from pydantic import Field

from ._response import _SkynetResponse


class Filter(_SkynetResponse):
    id: str
    """Filter id, such as V or r'"""
    central_wavelength_nm: float | None = None
    bandpass_nm: float | None = None


class Telescope(_SkynetResponse):
    id: int
    name: str
    slug: str
    latitude_deg: float
    longitude_deg: float
    elevation_m: float


class Instrument(_SkynetResponse):
    id: int
    name: str | None = None
    telescope_id: int | None = None


class ObservingAccount(_SkynetResponse):
    id: int
    name: str | None = None
    slug: str | None = None
    owner_id: int
    """The entity that owns the account and the observations it funds"""


class Entity(_SkynetResponse):
    id: int
    entity_type: Literal["user", "organization"]
    slug: str
    name: str | None = None


class PoolLimits(_SkynetResponse):
    """Advisory bounds for a request form. None means unbounded."""

    min_target_snr: float | None = None
    max_target_snr: float | None = None
    min_exposure_time_sec: float | None = None
    max_exposure_time_sec: float | None = None
    min_sample_count: int | None = None
    max_sample_count: int | None = None
    min_epoch_count: int | None = None
    max_epoch_count: int | None = None
    max_window_duration_sec: float | None = None


class InstrumentPool(_SkynetResponse):
    """An observing grant the user can spend and the instruments it can fund"""

    grant_id: int
    grant_type: str
    account_id: int
    instrument_ids: list[int] = Field(default_factory=list)
    filter_instrument_ids: dict[str, list[int]] = Field(default_factory=dict)
    """Instrument ids offering each filter"""
    limits: PoolLimits = Field(default_factory=PoolLimits)


class InstrumentPools(_SkynetResponse):
    """The user's instrument pools, with the objects they refer to"""

    observation_type: str
    pools: list[InstrumentPool] = Field(default_factory=list)
    observing_accounts: dict[int, ObservingAccount] = Field(default_factory=dict)
    entities: dict[int, Entity] = Field(default_factory=dict)
    telescopes: dict[int, Telescope] = Field(default_factory=dict)
    instruments: dict[int, Instrument] = Field(default_factory=dict)
    filters: dict[str, Filter] = Field(default_factory=dict)

    def pool(self, grant_id: int) -> InstrumentPool:
        """Return the pool for a grant. Raises KeyError if the user can't spend it."""
        for pool in self.pools:
            if pool.grant_id == grant_id:
                return pool
        raise KeyError(grant_id)

    def account(self, pool: InstrumentPool) -> ObservingAccount:
        return self.observing_accounts[pool.account_id]

    def pool_telescopes(self, pool: InstrumentPool) -> list[Telescope]:
        """Return the telescopes the pool's instruments are on"""
        ids = {
            self.instruments[i].telescope_id
            for i in pool.instrument_ids
            if i in self.instruments
        }
        return [self.telescopes[t] for t in sorted(i for i in ids if i is not None)]
