import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel

from aeonlib.conf import Settings
from aeonlib.conf import settings as default_settings
from aeonlib.exceptions import AuthenticationError, ServiceNetworkError

from .pools import InstrumentPool, InstrumentPools, SkynetUser
from .preview import (
    InstrumentPreview,
    PlanTotals,
    SpecValidation,
    _CompatibilityResponse,
    _ObservabilityResponse,
    _PlanResponse,
)
from .progress import ObservationProgress, ProgressProduct
from .request import SkynetImagingRequest, SubmittedObservation, _iso, request_field

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

# The observability check covers at most this much of a request's window, on
# this grid (Skynet computes it per grid step and sets no limit itself)
PREVIEW_SPAN = timedelta(days=7)
PREVIEW_RESOLUTION_SEC = 300.0
NON_FIELD_ERRORS = "non_field_errors"


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
    url: str | None
    """None when Skynet didn't presign the file. Ask again later."""
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
        self._me: SkynetUser | None = None

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

    def me(self, refresh: bool = False) -> SkynetUser:
        """Get the Skynet user the token acts for (cached)"""
        if refresh or self._me is None:
            self._me = SkynetUser.model_validate(self._request("GET", "v1/me"))
        return self._me

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
        errors = self._pool_errors(request)
        if errors:
            raise ValueError(
                "; ".join(m for messages in errors.values() for m in messages)
            )
        response = self._request(
            "POST", "v1/observation-specs:publish", json=self._publish_body(request)
        )
        return SubmittedObservation.model_validate(response)

    def validate(self, request: SkynetImagingRequest) -> SpecValidation:
        """
        Check that Skynet accepts the request: its grant and filters against the
        user's pools, then the spec itself. Use instrument_preview for which
        instruments can carry it out.
        """
        errors = self._pool_errors(request)
        if errors:
            return SpecValidation(valid=False, errors=errors)
        try:
            self._request(
                "POST",
                "v1/observation-specs:validate",
                json=self._publish_body(request)["spec"],
            )
        except SkynetError as exc:
            if exc.status_code != 422:
                raise
            return SpecValidation(valid=False, errors=_field_errors(exc.detail))
        return SpecValidation(valid=True)

    def instrument_preview(
        self, request: SkynetImagingRequest, instrument_ids: list[int] | None = None
    ) -> list[InstrumentPreview]:
        """
        Report, per instrument, whether it can carry out the request, when it can
        observe it in the window, and Skynet's exposure plan. By default it checks
        every instrument in the grant's pool that offers one of the requested
        filters. The request must be valid: Skynet answers an invalid one with a
        422, raised as SkynetError.
        """
        pools = self.instrument_pools()
        pool = self._pool(request)
        if instrument_ids is None:
            instrument_ids = _candidates(request, pool)
        if not instrument_ids:
            return []
        spec = self._publish_body(request)["spec"]
        compatibility = _CompatibilityResponse.model_validate(
            self._request(
                "POST",
                "v1/observation-specs:compatible-instruments",
                json={"spec": spec, "instrumentIds": instrument_ids},
            )
        )
        assert request.window.start is not None  # checked in SkynetImagingRequest
        # What's left of the window: a start in the past would count windows
        # that have gone
        start = max(request.window.start.utc.to_datetime(timezone=UTC), _now())
        end = request.window.end.utc.to_datetime(timezone=UTC)
        checked_until = min(end, start + PREVIEW_SPAN)
        previews = []
        for result in compatibility.results:
            instrument = pools.instruments.get(result.instrument_id)
            telescope_id = instrument.telescope_id if instrument else None
            preview = InstrumentPreview(
                instrument_id=result.instrument_id,
                telescope=pools.telescopes.get(telescope_id)
                if telescope_id is not None
                else None,
                compatible=result.compatible,
                reasons=result.reasons,
            )
            if result.compatible and start >= end:
                # The window has closed: nothing to observe in it
                preview.observable = False
                preview.checked_until = end
            elif result.compatible:
                observability = _ObservabilityResponse.model_validate(
                    self._request(
                        "POST",
                        "v1/observation-specs:observability",
                        json={
                            "spec": spec,
                            "instrumentId": result.instrument_id,
                            "start": start.isoformat().replace("+00:00", "Z"),
                            "spanDays": (checked_until - start).total_seconds() / 86400,
                            "resolutionSec": PREVIEW_RESOLUTION_SEC,
                        },
                    )
                )
                plan = _PlanResponse.model_validate(
                    self._request(
                        "POST",
                        "v1/observation-specs:plan",
                        json={
                            "spec": spec,
                            "instrumentId": result.instrument_id,
                            "startsOn": _iso(request.window.start),
                            "endsOn": _iso(request.window.end),
                        },
                    )
                ).optical_imaging_configuration_plan
                preview.windows = [w for w in observability.windows if w.observable]
                preview.observable = bool(preview.windows)
                preview.checked_until = checked_until
                if plan is not None:
                    preview.plan = PlanTotals(
                        total_exposures=plan.total_exposures,
                        total_exposure_time=plan.total_exposure_time_sec,
                    )
            previews.append(preview)
        return previews

    def _pool(self, request: SkynetImagingRequest) -> InstrumentPool:
        try:
            return self.instrument_pools().pool(request.grant_id)
        except KeyError:
            raise ValueError(
                f"Grant {request.grant_id} is not one of your spendable observing grants"
            ) from None

    def _pool_errors(self, request: SkynetImagingRequest) -> dict[str, list[str]]:
        """Check the request's grant and filters against the user's pools"""
        try:
            pool = self._pool(request)
        except ValueError as exc:
            return {"grant_id": [str(exc)]}
        errors: dict[str, list[str]] = {}
        for index, exposure in enumerate(request.exposures):
            if exposure.filter_id not in pool.filter_instrument_ids:
                message = (
                    f"Grant {request.grant_id} reaches no instrument with filter "
                    f"{exposure.filter_id}"
                )
                errors[f"exposures.{index}.filter_id"] = [message]
        return errors

    def _publish_body(self, request: SkynetImagingRequest) -> dict[str, Any]:
        pools = self.instrument_pools()
        owner_id = pools.account(self._pool(request)).owner_id
        return request.to_publish_body(owner_id=owner_id)

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
        self,
        observation_id: int | str,
        final_only: bool = True,
        progress: ObservationProgress | None = None,
    ) -> list[DataProduct]:
        """
        Get the observation's science files with presigned URLs. With final_only,
        raw frames that are still being processed are left out. Pass a progress
        already read to save reading it again. A file Skynet doesn't presign comes
        back with url None.
        """
        if progress is None:
            progress = self.progress(observation_id)
        products = [p for p in progress.products if p.is_final or not final_only]
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
        missing = [p.file_id for p in products if p.file_id not in urls]
        if missing:
            logger.warning(
                "Skynet didn't presign %d file(s): %s", len(missing), missing
            )
        return [_data_product(p, urls.get(p.file_id)) for p in products]

    def cancel(self, observation_id: int | str) -> None:
        """Cancel the observation. Frames already taken are kept."""
        self._request(
            "PATCH", f"v1/observations/{observation_id}", json={"status": "canceled"}
        )

    def observation_url(self, observation_id: int | str) -> str:
        """Get the URL of the observation's page on the Skynet website"""
        return f"{self.web_root}/observations/{observation_id}"


def _now() -> datetime:
    return datetime.now(UTC)


def _detail(response: httpx.Response) -> Any:
    try:
        body = response.json()
    except ValueError:
        return response.text
    return body.get("detail", response.text) if isinstance(body, dict) else body


def _data_product(product: ProgressProduct, url: str | None) -> DataProduct:
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


def _candidates(request: SkynetImagingRequest, pool: InstrumentPool) -> list[int]:
    """The pool's instruments that offer any of the requested filters"""
    ids: set[int] = set()
    for exposure in request.exposures:
        ids.update(pool.filter_instrument_ids.get(exposure.filter_id, []))
    return sorted(ids)


def _field_errors(detail: Any) -> dict[str, list[str]]:
    """Map a FastAPI 422 detail onto request field paths"""
    errors: dict[str, list[str]] = {}
    if not isinstance(detail, list):
        return {NON_FIELD_ERRORS: [str(detail)]}
    for item in detail:
        if not isinstance(item, dict):
            errors.setdefault(NON_FIELD_ERRORS, []).append(str(item))
            continue
        if _read_model_branch(item.get("loc", [])):
            continue
        field = request_field(item.get("loc", [])) or NON_FIELD_ERRORS
        errors.setdefault(field, []).append(str(item.get("msg", item)))
    return errors


def _read_model_branch(loc: Any) -> bool:
    """
    Whether an error comes from a union branch for stored models (it names no
    *Create model): Skynet accepts either a stored or a new brightness model, so
    a new one also fails the stored branch, on fields such as id it never had
    """
    return any(
        isinstance(part, str)
        and part.startswith("tagged-union[")
        and "Create" not in part
        for part in loc
    )
