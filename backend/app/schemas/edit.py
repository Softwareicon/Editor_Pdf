"""Overlay edits. All coordinates are PDF points in the page as it is displayed (after its rotation),
measured from the top-left corner of the visible page, x to the right and y downwards."""
import math
import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator

Coord = Annotated[float, Field(ge=-20000, le=20000)]
Extent = Annotated[float, Field(gt=0, le=20000)]
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_IMAGE_CHARS = 8_000_000      # base64 characters per image (about 6 MB of image data)
MAX_TOTAL_IMAGE_CHARS = 24_000_000


def _color(value: str | None) -> str | None:
    if value is not None and not _COLOR.match(value):
        raise ValueError("colours must look like #rrggbb")
    return value


class _Item(BaseModel):
    page: int = Field(ge=1, description="1-based page number")


class TextItem(_Item):
    type: Literal["text"]
    x: Coord
    y: Coord
    text: str = Field(min_length=1, max_length=5000)
    size: float = Field(default=14, ge=4, le=400)
    color: str = "#000000"
    font: Literal["helvetica", "times", "courier"] = "helvetica"
    bold: bool = False
    italic: bool = False
    align: Literal["left", "center", "right"] = "left"
    rotate: float = Field(default=0, ge=-360, le=360, description="Anticlockwise, about the start of the first baseline")
    opacity: float = Field(default=1, ge=0.05, le=1)
    _c = field_validator("color")(_color)

    @field_validator("text")
    @classmethod
    def _encodable(cls, value: str) -> str:
        value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
        try:
            value.encode("cp1252")
        except UnicodeEncodeError:
            raise ValueError("text contains characters the built-in PDF fonts cannot show; send it as an image")
        if any(ord(ch) < 32 and ch != "\n" for ch in value):
            raise ValueError("text contains control characters")
        return value


class ShapeItem(_Item):
    type: Literal["rect", "ellipse"]
    x: Coord
    y: Coord
    w: Extent
    h: Extent
    fill: str | None = None
    stroke: str | None = None
    stroke_width: float = Field(default=1.5, ge=0.1, le=50)
    opacity: float = Field(default=1, ge=0.05, le=1)
    _c = field_validator("fill", "stroke")(_color)

    @model_validator(mode="after")
    def _visible(self):
        if self.fill is None and self.stroke is None:
            raise ValueError("a shape needs a fill or a stroke colour")
        return self


class LineItem(_Item):
    type: Literal["line"]
    x1: Coord
    y1: Coord
    x2: Coord
    y2: Coord
    color: str = "#000000"
    width: float = Field(default=1.5, ge=0.1, le=50)
    arrow: bool = False
    _c = field_validator("color")(_color)


class InkItem(_Item):
    type: Literal["ink"]
    points: list[tuple[float, float]] = Field(min_length=2, max_length=5000)
    color: str = "#000000"
    width: float = Field(default=1.5, ge=0.1, le=50)
    _c = field_validator("color")(_color)

    @field_validator("points")
    @classmethod
    def _finite(cls, value):
        if any(not (math.isfinite(x) and math.isfinite(y) and abs(x) <= 20000 and abs(y) <= 20000) for x, y in value):
            raise ValueError("points are out of range")
        return value


class ImageItem(_Item):
    type: Literal["image"]
    x: Coord
    y: Coord
    w: Extent
    h: Extent
    data: str = Field(min_length=16, max_length=MAX_IMAGE_CHARS, description="base64 PNG or JPEG (a data: URL is accepted)")

    @field_validator("data")
    @classmethod
    def _strip_prefix(cls, value: str) -> str:
        if value.startswith("data:"):
            head, _, value = value.partition(",")
            if not head.endswith(";base64"):
                raise ValueError("image data must be base64")
        return value


class LinkItem(_Item):
    type: Literal["link"]
    x: Coord
    y: Coord
    w: Extent
    h: Extent
    url: str = Field(min_length=4, max_length=2000)

    @field_validator("url")
    @classmethod
    def _safe_scheme(cls, value: str) -> str:
        value = value.strip()
        if not re.match(r"^(https?://|mailto:)[^\s<>\x00-\x1f]+$", value, re.I):
            raise ValueError("links must start with http://, https:// or mailto:")
        return value


class FieldItem(_Item):
    """A new fillable form field."""
    type: Literal["field"]
    x: Coord
    y: Coord
    w: Extent
    h: Extent
    field: Literal["text", "multiline", "checkbox"] = "text"
    name: str = Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _-]*$")


EditItem = Annotated[Union[TextItem, ShapeItem, LineItem, InkItem, ImageItem, LinkItem, FieldItem], Field(discriminator="type")]


class ApplyEditsRequest(BaseModel):
    document_id: str
    items: list[EditItem] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _total_size(self):
        if sum(len(i.data) for i in self.items if isinstance(i, ImageItem)) > MAX_TOTAL_IMAGE_CHARS:
            raise ValueError("the images in one save are too large; save in smaller steps")
        return self
