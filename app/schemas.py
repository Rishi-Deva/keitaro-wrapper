from pydantic import BaseModel, Field


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    geo: str = Field(min_length=2, max_length=2)
    offer_id: int = Field(gt=0)


class OfferAdd(BaseModel):
    offer_id: int = Field(gt=0)
