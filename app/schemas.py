from uuid import UUID

import pycountry
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateCampaign(Input):
    request_id: UUID
    name: str = Field(min_length=1, max_length=180)
    country: str
    offer_id: int = Field(gt=0)
    domain_id: int = Field(gt=0)
    group_id: int = Field(gt=0)
    traffic_source_id: int = Field(gt=0)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("Укажите название без управляющих символов")
        return value

    @field_validator("country")
    @classmethod
    def country_code(cls, value):
        value = value.strip().upper()
        if not pycountry.countries.get(alpha_2=value):
            raise ValueError("Нужен существующий код страны ISO 3166-1 alpha-2")
        return value


class Revision(Input):
    revision: int = Field(ge=1)


class AddOffer(Revision):
    offer_id: int = Field(gt=0)


class PinOffer(Revision):
    pinned: bool
    share: int | None = Field(default=None, ge=0, le=100, strict=True)


class SyncCampaign(Input):
    discard_drafts: bool = False
