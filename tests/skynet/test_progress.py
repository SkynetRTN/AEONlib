import pytest
from pydantic import ValidationError

from aeonlib.skynet.progress import ObservationProgress

from ._data import STATES, load


@pytest.mark.parametrize("state", STATES)
def test_every_recorded_state(state: str):
    progress = ObservationProgress.model_validate(load(f"progress/{state}.json"))
    assert progress.state == state.lower()


def test_products():
    progress = ObservationProgress.model_validate(load("progress_products.json"))
    (product,) = progress.products
    assert product.is_reduced and product.is_final
    assert product.filter_id and product.exposure_time_sec
    assert not progress.products_final


def test_unknown_fields_are_ignored():
    body = load("progress/PENDING.json") | {"aFieldFromTheFuture": 1}
    ObservationProgress.model_validate(body)


def test_an_unknown_state_is_refused():
    with pytest.raises(ValidationError):
        ObservationProgress.model_validate(
            load("progress/PENDING.json") | {"state": "lost"}
        )
