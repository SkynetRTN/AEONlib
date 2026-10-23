from typing import ClassVar

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class _SkynetResponse(BaseModel):
    """Base for Skynet API responses: camelCase keys, unknown fields ignored"""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="ignore"
    )
