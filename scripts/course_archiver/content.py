from __future__ import annotations

import mimetypes
import re
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Pattern, Tuple, Union
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
import markdownify
from PIL import Image

from .models import ImageRecord
from .utils import normalize_title, sha256_bytes, sha256_text


IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
}

UI_NOISE_TEXTS = {"写笔记划线删除划线复制", "添加到笔记", "已添加到笔记"}
NoisePattern = Union[str, Pattern[str]]


def _matches_noise_pattern(value: str, patterns: Iterable[NoisePattern]) -> bool:
    for pattern in patterns:
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
        if compiled.fullmatch(value):
            return True
    return False


def is_ui_noise_text(value: str, extra_patterns: Iterable[NoisePattern] = ()) -> bool:
    """返回正文容器中应剔除的固定界面文案或调用方配置的界面噪声。"""
    compact = re.sub(r"\s+", "", value or "")
    return compact in UI_NOISE_TEXTS or _matches_noise_pattern(compact, extra_patterns)


def markdown_line_content(line: str) -> str:
    """去掉常见 Markdown 行前缀后，取得用于界面文案判断的纯文本。"""
    plain = (line or "").strip()
    plain = re.sub(r"^(?:#{1,6}\s*|>\s*|[-*+]\s*|\d+[.)]\s*)", "", plain)
    return re.sub(r"\s+", "", plain)


def is_ui_noise_markdown_line(line: str, extra_patterns: Iterable[NoisePattern] = ()) -> bool:
    return is_ui_noise_text(markdown_line_content(line), extra_patterns)


def choose_image_source(tag: Tag, page_url: str) -> str:
    for attribute in ("data-archive-src", "data-src", "data-original", "src"):
        value = (tag.get(attribute) or "").strip()
        if value and not value.startswith(("data:", "blob:", "chrome-extension:")):
            return urljoin(page_url, value)
    srcset = (tag.get("srcset") or "").strip()
    if srcset:
        candidates = [item.strip().split()[0] for item in srcset.split(",") if item.strip()]
        if candidates:
            return urljoin(page_url, candidates[-1])
    return ""


def validate_image_signature(data: bytes, mime_type: str) -> bool:
    if mime_type == "image/svg+xml":
        prefix = data[:512].lstrip().lower()
        return prefix.startswith(b"<svg") or b"<svg" in prefix
    signatures = (
        data.startswith(b"\x89PNG\r\n\x1a\n"),
        data.startswith(b"\xff\xd8\xff"),
        data.startswith((b"GIF87a", b"GIF89a")),
        len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
        data.startswith(b"BM"),
    )
    return any(signatures)


def detect_extension(mime_type: str, url: str) -> str:
    normalized = mime_type.split(";", 1)[0].lower().strip()
    if normalized in IMAGE_EXTENSIONS:
        return IMAGE_EXTENSIONS[normalized]
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    guessed = mimetypes.guess_extension(normalized) if normalized else None
    return guessed or ".img"


def clean_article_html(
    html: str,
    page_url: str,
    title: str,
    article_id: str,
    image_dir: Path,
    markdown_image_prefix: str,
    downloader: Callable[[str], Tuple[bytes, str]],
    existing_images: Optional[List[ImageRecord]] = None,
    ui_noise_patterns: Iterable[NoisePattern] = (),
) -> Tuple[str, int, List[ImageRecord], str]:
    """清理单篇正文、下载图片并返回 Markdown、字数、图片清单和正文哈希。"""

    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style, noscript, svg, video, audio, iframe, form, button, input"):
        node.decompose()
    for node in reversed(list(soup.find_all(True))):
        if node.parent is None:
            continue
        compact_text = re.sub(r"\s+", "", node.get_text("", strip=True))
        if is_ui_noise_text(compact_text, ui_noise_patterns):
            node.decompose()
    existing_by_url = {item.original_url: item for item in (existing_images or []) if item.status == "success"}
    image_records: List[ImageRecord] = []
    image_dir.mkdir(parents=True, exist_ok=True)

    for sequence, image in enumerate(list(soup.find_all("img")), 1):
        source = choose_image_source(image, page_url)
        alt = (image.get("alt") or "").strip() or f"正文插图 {sequence}"
        width = _safe_int(image.get("data-archive-width") or image.get("width"))
        height = _safe_int(image.get("data-archive-height") or image.get("height"))
        if not source:
            image.replace_with(soup.new_string(f"[图片占位已剔除：{alt}]"))
            continue
        prior = existing_by_url.get(source)
        if prior:
            prior_path = image_dir.parents[2] / Path(prior.local_path)
            if prior_path.exists() and prior_path.stat().st_size == prior.bytes:
                if prior.width <= 0 or prior.height <= 0:
                    with Image.open(prior_path) as local_image:
                        prior.width, prior.height = local_image.size
                image["src"] = f"{markdown_image_prefix}/{Path(prior.local_path).name}"
                image["alt"] = alt
                for attr in ("srcset", "data-src", "data-original", "data-archive-src"):
                    image.attrs.pop(attr, None)
                image_records.append(prior)
                continue
        try:
            data, mime_type = downloader(source)
            mime_type = mime_type.split(";", 1)[0].strip().lower()
            if not data:
                raise ValueError("下载结果为空")
            if not validate_image_signature(data, mime_type):
                raise ValueError(f"图片签名无效，Content-Type={mime_type or 'unknown'}")
            if width <= 0 or height <= 0:
                with Image.open(BytesIO(data)) as downloaded_image:
                    width, height = downloaded_image.size
            extension = detect_extension(mime_type, source)
            filename = f"{sequence:03d}{extension}"
            destination = image_dir / filename
            temporary = destination.with_name(f".{destination.name}.tmp")
            temporary.write_bytes(data)
            temporary.replace(destination)
            record = ImageRecord(
                sequence=sequence,
                original_url=source,
                local_path=(Path("archive") / "images" / article_id / filename).as_posix(),
                mime_type=mime_type,
                width=width,
                height=height,
                sha256=sha256_bytes(data),
                bytes=len(data),
            )
            image["src"] = f"{markdown_image_prefix}/{filename}"
            image["alt"] = alt
            for attr in ("srcset", "data-src", "data-original", "data-archive-src", "style", "class"):
                image.attrs.pop(attr, None)
            image_records.append(record)
        except Exception as exc:
            image.replace_with(soup.new_string(f"[图片下载失败：{alt}]"))
            image_records.append(ImageRecord(
                sequence=sequence,
                original_url=source,
                local_path="",
                mime_type="",
                width=width,
                height=height,
                sha256="",
                bytes=0,
                status="failed",
                error=str(exc),
            ))

    first_heading = soup.find(["h1", "h2", "h3"])
    if first_heading and normalize_title(first_heading.get_text(" ", strip=True)) == normalize_title(title):
        first_heading.decompose()

    markdown_text = markdownify.markdownify(
        str(soup),
        heading_style="ATX",
        bullets="-",
        strip=["a"],
    )
    markdown_text = re.sub(r"[ \t]+\n", "\n", markdown_text)
    markdown_text = re.sub(r"\n{3,}", "\n\n", markdown_text).strip()
    markdown_text = "\n".join(
        line for line in markdown_text.splitlines()
        if not is_ui_noise_markdown_line(line, ui_noise_patterns)
    ).strip()
    text_only = soup.get_text("", strip=True)
    text_only = re.sub(r"\s+", "", text_only)
    digest_material = markdown_text + "\n" + "\n".join(
        f"{item.sequence}:{item.sha256}:{item.status}" for item in image_records
    )
    return markdown_text, len(text_only), image_records, sha256_text(digest_material)


def _safe_int(value: object) -> int:
    try:
        return int(str(value or "0").split(".", 1)[0])
    except (TypeError, ValueError):
        return 0
