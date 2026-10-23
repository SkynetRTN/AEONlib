import logging
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel

from aeonlib.conf import Settings
from aeonlib.conf import settings as default_settings
from aeonlib.exceptions import AuthenticationError, ServiceNetworkError

from .pools import InstrumentPools
from .progress import ObservationProgress, ProgressProduct
from .request import SkynetImagingRequest, SubmittedObservation

logger = logging.getLogger(__name__)

# Skynet progress states mapped to the LCO-style names TOM users know
STATES = {
    "pending": "PENDING",
    "scheduled": "SCHEDULED",
    "in_progress": "IN_PROGRESS",
    "completed": "COMPLETED",
    "partially_completed": "PARTIALLY_COMPLETED",
    "canceled": "CANCELED",
    "window_expired": "WINDOW_EXPIRED",
}
TERMINAL_STATES = frozenset(
    {"COMPLETED", "PARTIALLY_COMPLETED", "CANCELED", "WINDOW_EXPIRED"}
)

# Skynet's maximum presigned URL lifetime in seconds (its default is 60)
PRESIGN_EXPIRES_IN = 900
PRESIGN_BATCH = 200


class SkynetError(RuntimeError):
    """Raised when the Skynet API returns an error"""

    def __init__(self, status_code: int, detail: Any):
        super().__init__(f"Skynet API error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class ObservationState(BaseModel):
    """An observation's state and schedule"""

    state: str
    """One of the values of STATES"""
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    progress_fraction: float = 0.0
    terminal: bool = False


class DataProduct(BaseModel):
    """A science file with a presigned download URL"""

    file_id: str
    filename: str
    url: str
    is_reduced: bool
    is_final: bool
    telescope_id: int | None = None
    instrument_id: int | None = None
    filter_id: str | None = None
    exposure_time_sec: float | None = None
    taken_at_start_utc: datetime | None = None
    airmass: float | None = None
    pixel_scale_arcsec_per_px: float | None = None


class SkynetFacility:
    """
    Skynet Robotic Telescope Network facility

    Configuration:
        - AEON_SKYNET_TOKEN: An unscoped Skynet personal API token
        - AEON_SKYNET_API_ROOT: Skynet API root
        - AEON_SKYNET_WEB_ROOT: Skynet website, for observation links
    """

    def __init__(
        self,
        settings: Settings = default_settings,
        client: httpx.Client | None = None,
    ):
        if not settings.skynet_api_root:
            raise ValueError("AEON_SKYNET_API_ROOT is not set")
        if not settings.skynet_token:
            raise ValueError("AEON_SKYNET_TOKEN is not set")
        self.web_root = settings.skynet_web_root.rstrip("/")
        headers = {
            "Authorization": f"Bearer {settings.skynet_token}",
            "Accept": "application/json",
        }
        if client is None:
            client = httpx.Client(base_url=settings.skynet_api_root, timeout=30.0)
        client.headers.update(headers)
        self._client = client
        self._pools: dict[str, InstrumentPools] = {}

    def __del__(self):
        client = getattr(self, "_client", None)
        if client is not None:
            client.close()

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        logger.debug("-> %s %s", method, url)
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            raise ServiceNetworkError(f"Skynet: {exc}") from exc
        if response.status_code in (401, 403):
            raise AuthenticationError(f"Skynet: {_detail(response)}")
        if response.is_error:
            raise SkynetError(response.status_code, _detail(response))
        return response.json()

    def instrument_pools(
        self, observation_type: str = "optical_imaging", refresh: bool = False
    ) -> InstrumentPools:
        """Get the observing grants the user can spend and what each reaches (cached)"""
        if refresh or observation_type not in self._pools:
            body = self._request(
                "GET",
                "v1/me/observing-access/instrument-pools",
                params={"observationType": observation_type},
            )
            self._pools[observation_type] = InstrumentPools.model_validate(body)
        return self._pools[observation_type]

    def submit(self, request: SkynetImagingRequest) -> SubmittedObservation:
        """Submit the request after checking its grant and filters against the user's pools"""
        pools = self.instrument_pools()
        try:
            pool = pools.pool(request.grant_id)
        except KeyError:
            raise ValueError(
                f"Grant {request.grant_id} is not one of your spendable observing grants"
            ) from None
        missing = sorted(
            {e.filter_id for e in request.exposures} - set(pool.filter_instrument_ids)
        )
        if missing:
            raise ValueError(
                f"Grant {request.grant_id} reaches no instrument with filter(s) {missing}"
            )
        body = request.to_publish_body(owner_id=pools.account(pool).owner_id)
        response = self._request("POST", "v1/observation-specs:publish", json=body)
        return SubmittedObservation.model_validate(response)

    def progress(self, observation_id: int | str) -> ObservationProgress:
        """Get the observation's progress and downloadable science files"""
        body = self._request("GET", f"v1/observations/{observation_id}/progress")
        return ObservationProgress.model_validate(body)

    def state(self, observation_id: int | str) -> ObservationState:
        """Get the observation's state and schedule"""
        progress = self.progress(observation_id)
        state = STATES[progress.state]
        return ObservationState(
            state=state,
            scheduled_start=progress.scheduled_start,
            scheduled_end=progress.scheduled_end,
            progress_fraction=progress.progress_fraction,
            terminal=state in TERMINAL_STATES,
        )

    def data_products(
        self, observation_id: int | str, final_only: bool = True
    ) -> list[DataProduct]:
        """
        Get the observation's science files with presigned URLs. With final_only,
        raw frames that are still being processed are left out.
        """
        products = [
            p
            for p in self.progress(observation_id).products
            if p.is_final or not final_only
        ]
        urls: dict[str, str] = {}
        for start in range(0, len(products), PRESIGN_BATCH):
            batch = products[start : start + PRESIGN_BATCH]
            body = self._request(
                "POST",
                "v1/files/data/presign",
                json={
                    "fileIds": [p.file_id for p in batch],
                    "expiresIn": PRESIGN_EXPIRES_IN,
                },
            )
            urls.update({item["fileId"]: item["url"] for item in body["items"]})
        return [
            _data_product(p, urls[p.file_id]) for p in products if p.file_id in urls
        ]

    def observation_url(self, observation_id: int | str) -> str:
        """Get the URL of the observation's page on the Skynet website"""
        return f"{self.web_root}/observations/{observation_id}"


def _detail(response: httpx.Response) -> Any:
    try:
        return response.json().get("detail", response.text)
    except ValueError:
        return response.text


def _data_product(product: ProgressProduct, url: str) -> DataProduct:
    return DataProduct(
        file_id=product.file_id,
        filename=product.filename,
        url=url,
        is_reduced=product.is_reduced,
        is_final=product.is_final,
        telescope_id=product.telescope_id,
        instrument_id=product.instrument_id,
        filter_id=product.filter_id,
        exposure_time_sec=product.exposure_time_sec,
        taken_at_start_utc=product.taken_at_start_utc,
        airmass=product.airmass,
        pixel_scale_arcsec_per_px=product.pixel_scale_arcsec_per_px,
    )
