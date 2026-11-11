from pydantic import BaseModel, Field, model_validator

PageList = list[int]  # 1-based page numbers


class DocumentRef(BaseModel):
    document_id: str


class PagesRequest(DocumentRef):
    pages: PageList = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def _unique_positive(self):
        if any(p < 1 for p in self.pages) or len(set(self.pages)) != len(self.pages):
            raise ValueError("pages must be unique numbers starting at 1")
        return self


class RotateRequest(PagesRequest):
    angle: int = Field(description="Clockwise rotation: 90, 180 or 270")

    @model_validator(mode="after")
    def _angle(self):
        if self.angle not in (90, 180, 270):
            raise ValueError("angle must be 90, 180 or 270")
        return self


class ReorderRequest(DocumentRef):
    order: PageList = Field(min_length=1, max_length=5000, description="Current page numbers in their new order")


class AddPagesRequest(DocumentRef):
    after: int = Field(ge=0, description="Insert after this page; 0 inserts at the start")
    count: int = Field(default=1, ge=1, le=50)
    width: float | None = Field(default=None, ge=72, le=14400, description="Points; default: same as the neighbouring page")
    height: float | None = Field(default=None, ge=72, le=14400)

    @model_validator(mode="after")
    def _both_or_neither(self):
        if (self.width is None) != (self.height is None):
            raise ValueError("give both width and height, or neither")
        return self
