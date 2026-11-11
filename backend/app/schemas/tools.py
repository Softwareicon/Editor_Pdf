import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_PAGES = re.compile(r"^\s*(all|\d+(\s*-\s*\d+)?(\s*,\s*\d+(\s*-\s*\d+)?)*)\s*$", re.I)
Pages = str  # "all" or a list such as "1-3, 5, 8-10"


class _Doc(BaseModel):
    document_id: str


class _Paged(_Doc):
    pages: Pages = Field(default="all", max_length=500)

    @field_validator("pages")
    @classmethod
    def _syntax(cls, value: str) -> str:
        if not _PAGES.match(value):
            raise ValueError('pages must be "all" or a list such as "1-3, 5, 8-10"')
        return value


def _printable(value: str) -> str:
    try:
        value.encode("cp1252")
    except UnicodeEncodeError:
        raise ValueError("this tool supports Latin text only for now")
    return value


class MergeRequest(BaseModel):
    document_ids: list[str] = Field(min_length=2, max_length=20)
    name: str = Field(default="Merged.pdf", max_length=255)


class SplitRequest(_Doc):
    mode: Literal["ranges", "every"] = "ranges"
    ranges: str = Field(default="", max_length=500, description='One new document per group, e.g. "1-3, 4-6, 9"')
    every: int = Field(default=1, ge=1, le=5000)


class CompressRequest(_Doc):
    level: Literal["lossless", "balanced", "strong"] = "balanced"


class PasswordRequest(_Doc):
    password: str = Field(min_length=1, max_length=128)


class ProtectRequest(_Doc):
    password: str = Field(min_length=6, max_length=128)


class CropRequest(_Paged):
    left: float = Field(default=0, ge=0, le=14400)
    top: float = Field(default=0, ge=0, le=14400)
    right: float = Field(default=0, ge=0, le=14400)
    bottom: float = Field(default=0, ge=0, le=14400)


class MetadataRequest(_Doc):
    title: str = Field(default="", max_length=500)
    author: str = Field(default="", max_length=500)
    subject: str = Field(default="", max_length=500)
    keywords: str = Field(default="", max_length=500)


class StampRequest(_Paged):
    """Page numbers, headers and footers. {n} is the page number and {total} the page count."""
    text: str = Field(default="{n}", min_length=1, max_length=200)
    position: Literal["top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right"] = "bottom-center"
    size: float = Field(default=10, ge=4, le=72)
    color: str = "#000000"
    margin: float = Field(default=28, ge=0, le=400)
    start: int = Field(default=1, ge=-100000, le=100000, description="Number given to the first stamped page")
    _t = field_validator("text")(_printable)

    @field_validator("color")
    @classmethod
    def _colour(cls, value: str) -> str:
        if not _COLOR.match(value):
            raise ValueError("colours must look like #rrggbb")
        return value


class WatermarkRequest(_Paged):
    text: str = Field(min_length=1, max_length=100)
    size: float = Field(default=60, ge=8, le=300)
    color: str = "#cc0000"
    opacity: float = Field(default=0.25, ge=0.05, le=1)
    angle: float = Field(default=45, ge=-90, le=90)
    _t = field_validator("text")(_printable)
    _c = field_validator("color")(StampRequest._colour.__func__)


class ToImagesRequest(_Doc):
    dpi: int = Field(default=150, ge=50, le=300)
    format: Literal["jpg", "png"] = "jpg"


class FormFillRequest(_Doc):
    values: dict[str, str | bool] = Field(min_length=1, max_length=1000)
