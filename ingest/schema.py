from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Base for every source response model: a new, renamed or retyped field fails loudly."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
