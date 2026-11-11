from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    original_filename: str
    file_size: int
    page_count: int | None
    mime_type: str
    processing_status: str
    current_version: int
    can_undo: bool
    can_redo: bool
    created_at: datetime
    updated_at: datetime


class DocumentList(BaseModel):
    items: list[DocumentOut]
    total: int
    limit: int
    offset: int


class PageInfo(BaseModel):
    index: int
    width: float
    height: float
    rotation: int


class PagesOut(BaseModel):
    document_id: str
    page_count: int | None
    needs_password: bool
    pages: list[PageInfo]


class MetadataOut(BaseModel):
    document_id: str
    filename: str
    file_size: int
    pdf_version: str
    encrypted: bool
    needs_password: bool
    page_count: int | None
    info: dict[str, str]


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    version_number: int
    change_description: str
    page_count: int | None
    file_size: int | None
    created_at: datetime


class VersionsOut(BaseModel):
    document_id: str
    current_version: int
    versions: list[VersionOut]
