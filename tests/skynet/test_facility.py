import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from aeonlib.conf import Settings
from aeonlib.exceptions import AuthenticationError, ServiceNetworkError
from aeonlib.skynet.facility import (
    PRESIGN_EXPIRES_IN,
    SkynetError,
    SkynetFacility,
)
from aeonlib.skynet.progress import ObservationProgress
from aeonlib.skynet.request import FilterExposure

from ._data import load
from .test_request import snr_request

API_ROOT = "https://api.skynet.test/"
TOKEN = "personal-token"

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """MockTransport handler that answers by method and path and records requests"""

    def __init__(self, routes: dict[tuple[str, str], Any]):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.routes.get((request.method, request.url.path))
        if answer is None:
            return httpx.Response(404, json={"detail": "Not Found"})
        if callable(answer):
            return answer(request)
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]


def facility(
    recorder: Handler, web_root: str = "https://skynet.test/"
) -> SkynetFacility:
    settings = Settings(
        skynet_token=TOKEN, skynet_api_root=API_ROOT, skynet_web_root=web_root
    )
    client = httpx.Client(base_url=API_ROOT, transport=httpx.MockTransport(recorder))
    return SkynetFacility(settings=settings, client=client)


POOLS = ("GET", "/v1/me/observing-access/instrument-pools")
PUBLISH = ("POST", "/v1/observation-specs:publish")
PRESIGN = ("POST", "/v1/files/data/presign")


def progress_path(observation_id: int) -> tuple[str, str]:
    return ("GET", f"/v1/observations/{observation_id}/progress")


class TestConfiguration:
    def test_a_token_is_required(self):
        with pytest.raises(ValueError, match="AEON_SKYNET_TOKEN"):
            SkynetFacility(settings=Settings(skynet_token="", skynet_api_root=API_ROOT))

    def test_an_api_root_is_required(self):
        with pytest.raises(ValueError, match="AEON_SKYNET_API_ROOT"):
            SkynetFacility(settings=Settings(skynet_token=TOKEN, skynet_api_root=""))

    def test_requests_carry_the_bearer_token(self):
        recorder = Recorder({POOLS: load("instrument_pools.json")})
        facility(recorder).instrument_pools()
        (request,) = recorder.requests
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.params["observationType"] == "optical_imaging"


class TestInstrumentPools:
    def test_cached_until_refreshed(self):
        recorder = Recorder({POOLS: load("instrument_pools.json")})
        skynet = facility(recorder)
        first = skynet.instrument_pools()
        assert skynet.instrument_pools() is first
        assert len(recorder.sent(*POOLS)) == 1
        skynet.instrument_pools(refresh=True)
        assert len(recorder.sent(*POOLS)) == 2


class TestSubmit:
    def test_publishes_the_contract_body_with_the_pool_owner(self):
        recorder = Recorder(
            {
                POOLS: load("instrument_pools.json"),
                PUBLISH: {"id": 4242, "uid": "6a1d3c58-0000-4000-8000-000000004242"},
            }
        )
        submitted = facility(recorder).submit(snr_request())
        assert (submitted.id, submitted.uid) == (
            4242,
            "6a1d3c58-0000-4000-8000-000000004242",
        )
        (publish,) = recorder.sent(*PUBLISH)
        assert json.loads(publish.content) == load("publish_snr.json")

    def test_a_grant_the_user_cannot_spend_is_refused_locally(self):
        recorder = Recorder({POOLS: load("instrument_pools.json")})
        with pytest.raises(ValueError, match="not one of your spendable"):
            facility(recorder).submit(snr_request(grant_id=12345))
        assert recorder.sent(*PUBLISH) == []

    def test_a_filter_the_pool_lacks_is_refused_locally(self):
        recorder = Recorder({POOLS: load("instrument_pools.json")})
        request = snr_request(
            exposures=[FilterExposure(filter_id="z'", target_snr=10, count=1)]
        )
        with pytest.raises(ValueError, match="no instrument with filter z'"):
            facility(recorder).submit(request)
        assert recorder.sent(*PUBLISH) == []

    def test_a_server_refusal_raises_with_its_detail(self):
        detail = [{"loc": ["body", "spec"], "msg": "bad"}]
        recorder = Recorder(
            {
                POOLS: load("instrument_pools.json"),
                PUBLISH: httpx.Response(422, json={"detail": detail}),
            }
        )
        with pytest.raises(SkynetError) as raised:
            facility(recorder).submit(snr_request())
        assert raised.value.status_code == 422 and raised.value.detail == detail


STATES = {
    "PENDING": False,
    "SCHEDULED": False,
    "IN_PROGRESS": False,
    "COMPLETED": True,
    "PARTIALLY_COMPLETED": True,
    "CANCELED": True,
    "WINDOW_EXPIRED": True,
}


class TestState:
    @pytest.mark.parametrize(("state", "terminal"), STATES.items())
    def test_each_recorded_state(self, state: str, terminal: bool):
        body = load(f"progress/{state}.json")
        recorder = Recorder({progress_path(body["id"]): body})
        result = facility(recorder).state(body["id"])
        assert result.state == state and result.terminal is terminal

    def test_schedule_comes_through(self):
        body = load("progress/SCHEDULED.json")
        recorder = Recorder({progress_path(body["id"]): body})
        result = facility(recorder).state(body["id"])
        assert result.scheduled_start is not None and result.scheduled_end is not None
        assert result.scheduled_start < result.scheduled_end


def _product(n: int, *, final: bool = True) -> dict[str, Any]:
    return {
        "assetId": n,
        "fileId": f"00000000-0000-4000-8000-{n:012d}",
        "filename": f"frame-{n}.fits",
        "isReduced": final,
        "isFinal": final,
    }


def _presign_all(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    items = [
        {"fileId": f, "url": f"https://data.test/{f}?sig=1"} for f in body["fileIds"]
    ]
    return httpx.Response(200, json={"items": items, "expiresIn": body["expiresIn"]})


class TestDataProducts:
    def test_presigns_the_final_products_for_900_seconds(self):
        progress = load("progress_products.json")
        recorder = Recorder(
            {progress_path(progress["id"]): progress, PRESIGN: load("presign.json")}
        )
        (product,) = facility(recorder).data_products(progress["id"])
        expected = progress["products"][0]
        assert (
            product.file_id == expected["fileId"]
            and product.filename == expected["filename"]
        )
        assert (
            (product.url or "").startswith("https://")
            and product.is_reduced
            and product.is_final
        )
        assert product.filter_id == expected["filterId"]
        assert product.telescope_id == expected["telescopeId"]
        (presign,) = recorder.sent(*PRESIGN)
        assert json.loads(presign.content) == {
            "fileIds": [expected["fileId"]],
            "expiresIn": PRESIGN_EXPIRES_IN,
        }
        assert PRESIGN_EXPIRES_IN == 900

    def test_raw_frames_still_processing_are_left_out_unless_asked(self):
        body = load("progress/PENDING.json") | {
            "products": [_product(1), _product(2, final=False)]
        }
        recorder = Recorder({progress_path(body["id"]): body, PRESIGN: _presign_all})
        skynet = facility(recorder)
        assert [p.filename for p in skynet.data_products(body["id"])] == [
            "frame-1.fits"
        ]
        everything = skynet.data_products(body["id"], final_only=False)
        assert [p.filename for p in everything] == ["frame-1.fits", "frame-2.fits"]

    def test_files_skynet_will_not_presign_have_no_url(self):
        body = load("progress/PENDING.json") | {"products": [_product(1), _product(2)]}

        def presign_first_only(request: httpx.Request) -> httpx.Response:
            first = json.loads(request.content)["fileIds"][0]
            return httpx.Response(
                200, json={"items": [{"fileId": first, "url": "u"}], "expiresIn": 900}
            )

        recorder = Recorder(
            {progress_path(body["id"]): body, PRESIGN: presign_first_only}
        )
        products = facility(recorder).data_products(body["id"])
        assert [(p.filename, p.url) for p in products] == [
            ("frame-1.fits", "u"),
            ("frame-2.fits", None),
        ]

    def test_a_progress_already_read_is_not_read_again(self):
        progress = load("progress_products.json")
        recorder = Recorder({PRESIGN: load("presign.json")})
        skynet = facility(recorder)
        (product,) = skynet.data_products(
            progress["id"], progress=ObservationProgress.model_validate(progress)
        )
        assert product.url
        assert recorder.sent(*progress_path(progress["id"])) == []

    def test_presigns_in_batches_of_200(self):
        body = load("progress/PENDING.json") | {
            "products": [_product(n) for n in range(450)]
        }
        recorder = Recorder({progress_path(body["id"]): body, PRESIGN: _presign_all})
        products = facility(recorder).data_products(body["id"])
        sizes = [len(json.loads(r.content)["fileIds"]) for r in recorder.sent(*PRESIGN)]
        assert sizes == [200, 200, 50] and len(products) == 450

    def test_no_products_means_no_presign(self):
        body = load("progress/PENDING.json")
        recorder = Recorder({progress_path(body["id"]): body})
        assert facility(recorder).data_products(body["id"]) == []
        assert recorder.sent(*PRESIGN) == []


class TestCancel:
    def test_patches_the_status(self):
        recorder = Recorder({("PATCH", "/v1/observations/42"): {"id": 42}})
        facility(recorder).cancel(42)
        (sent,) = recorder.requests
        assert json.loads(sent.content) == {"status": "canceled"}

    def test_a_refusal_raises(self):
        recorder = Recorder(
            {
                ("PATCH", "/v1/observations/42"): httpx.Response(
                    409, json={"detail": "Observation is already completed"}
                )
            }
        )
        with pytest.raises(SkynetError, match="already completed"):
            facility(recorder).cancel(42)


class TestErrors:
    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_failures(self, status: int):
        recorder = Recorder(
            {POOLS: httpx.Response(status, json={"detail": "insufficient_scope"})}
        )
        with pytest.raises(AuthenticationError, match="insufficient_scope"):
            facility(recorder).instrument_pools()

    def test_not_found(self):
        recorder = Recorder({})
        with pytest.raises(SkynetError) as raised:
            facility(recorder).progress(1)
        assert raised.value.status_code == 404

    def test_a_non_json_error_keeps_its_text(self):
        recorder = Recorder({POOLS: httpx.Response(502, text="Bad Gateway")})
        with pytest.raises(SkynetError, match="Bad Gateway"):
            facility(recorder).instrument_pools()

    def test_network_failures(self):
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        with pytest.raises(ServiceNetworkError):
            facility(unreachable).instrument_pools()


def test_observation_url_uses_the_web_root():
    skynet = facility(Recorder({}), web_root="https://skynet.test/")
    assert skynet.observation_url(310) == "https://skynet.test/observations/310"
