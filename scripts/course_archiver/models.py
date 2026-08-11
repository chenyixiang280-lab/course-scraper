from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass
class ImageRecord:
    sequence: int
    original_url: str
    local_path: str
    mime_type: str
    width: int
    height: int
    sha256: str
    bytes: int
    status: str = "success"
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ImageRecord":
        return cls(**data)


@dataclass
class ArticleRecord:
    order: int
    article_id: str
    url: str
    title: str
    section: str
    content_type: str
    published_at: str
    markdown_path: str
    text_char_count: int
    body_sha256: str
    scrape_time: str
    status: str = "success"
    images: List[ImageRecord] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["images"] = [image.to_dict() for image in self.images]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArticleRecord":
        payload = dict(data)
        payload["images"] = [ImageRecord.from_dict(item) for item in payload.get("images", [])]
        return cls(**payload)


@dataclass
class CatalogItem:
    order: int
    title: str
    section: str
    metadata: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

