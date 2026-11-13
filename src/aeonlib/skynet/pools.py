from typing import Literal

from pydantic import Field

from ._response import _SkynetResponse

# Skynet addresses every resource by uid (its C2 contract): the pools response
# keys its sideloads by uid and each pool carries its grant, account and
# instruments as uids. Nothing here reads an integer id.


class Filter(_SkynetResponse):
    id: str
    """Filter id, such as V or r'"""
    central_wavelength_nm: float | None = None
    bandpass_nm: float | None = None


class Telescope(_SkynetResponse):
    uid: str
    name: str
    slug: str
    latitude_deg: float
    longitude_deg: float
    elevation_m: float


class Instrument(_SkynetResponse):
    uid: str
    name: str | None = None
    telescope_uid: str | None = None


class ObservingAccount(_SkynetResponse):
    uid: str
    name: str | None = None
    slug: str | None = None
    owner_uid: str
    """The entity that owns the account and the observations it funds"""


class Entity(_SkynetResponse):
    uid: str
    entity_type: Literal["user", "organization"]
    slug: str
    name: str | None = None


class SkynetUser(_SkynetResponse):
    """The Skynet user a token acts for (GET /v1/me)"""

    uid: str
    """Also the user's entity uid: the ownerUid of the accounts they own"""
    username: str
    name: str | None = None
    slug: str | None = None


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

    grant_uid: str
    grant_type: str
    account_uid: str
    instrument_uids: list[str] = Field(default_factory=list)
    filter_instrument_uids: dict[str, list[str]] = Field(default_factory=dict)
    """Instrument uids offering each filter"""
    limits: PoolLimits = Field(default_factory=PoolLimits)


class InstrumentPools(_SkynetResponse):
    """
    The user's instrument pools, with the objects they refer to. Every map is
    keyed by uid: Skynet's contract carries no integer id.
    """

    observation_type: str
    pools: list[InstrumentPool] = Field(default_factory=list)
    observing_accounts: dict[str, ObservingAccount] = Field(default_factory=dict)
    entities: dict[str, Entity] = Field(default_factory=dict)
    telescopes: dict[str, Telescope] = Field(default_factory=dict)
    instruments: dict[str, Instrument] = Field(default_factory=dict)
    filters: dict[str, Filter] = Field(default_factory=dict)

    def pool(self, grant_uid: str) -> InstrumentPool:
        """Return the pool for a grant. Raises KeyError if the user can't spend it."""
        for pool in self.pools:
            if pool.grant_uid == grant_uid:
                return pool
        raise KeyError(grant_uid)

    def account(self, pool: InstrumentPool) -> ObservingAccount:
        return self.observing_accounts[pool.account_uid]

    def owner(self, pool: InstrumentPool) -> Entity | None:
        """The entity that owns the pool's account, and so the observations it funds"""
        return self.entities.get(self.account(pool).owner_uid)

    def pool_telescopes(self, pool: InstrumentPool) -> list[Telescope]:
        """Return the telescopes the pool's instruments are on, in instrument order"""
        telescopes: dict[str, Telescope] = {}
        for instrument_uid in pool.instrument_uids:
            instrument = self.instruments.get(instrument_uid)
            if instrument is None or instrument.telescope_uid is None:
                continue
            telescope = self.telescopes.get(instrument.telescope_uid)
            if telescope is not None:
                telescopes.setdefault(telescope.uid, telescope)
        return list(telescopes.values())
