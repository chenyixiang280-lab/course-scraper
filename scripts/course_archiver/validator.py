from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List

from .content import is_ui_noise_markdown_line, validate_image_signature
from .storage import ArchiveStore
from .utils import atomic_write_json, now_text, sha256_bytes


CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
EXCEL_EXTRACT_SCRIPT = CODEX_HOME / "skills" / "windows-office-files" / "scripts" / "extract_excel_com.ps1"
PWSH = Path(r"C:\Program Files\PowerShell-7.6.0\pwsh.exe")


def _index_row_count(path: Path, qa_dir: Path) -> int:
    signature = path.read_bytes()[:16]
    if not signature.startswith(b"%TSD-Header-"):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return sum(1 for _ in csv.DictReader(handle))
    if not EXCEL_EXTRACT_SCRIPT.exists() or not PWSH.exists():
        raise RuntimeError("course_index.csv 已被透明加密，但 Excel COM 提取脚本不可用")
    with tempfile.TemporaryDirectory(prefix="csv_validate_", dir=qa_dir) as temporary:
        temporary_dir = Path(temporary)
        manifest_path = temporary_dir / "manifest.json"
        result_path = temporary_dir / "result.json"
        atomic_write_json(manifest_path, [str(path.resolve())])
        completed = subprocess.run(
            [
                str(PWSH), "-NoProfile", "-File", str(EXCEL_EXTRACT_SCRIPT),
                "-ManifestPath", str(manifest_path), "-OutputPath", str(result_path),
                "-MaxRows", "1000", "-MaxColumns", "30", "-Force",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Excel COM 无法回读 course_index.csv: {completed.stderr.strip()}")
        extracted = json.loads(result_path.read_text(encoding="utf-8-sig"))
        first = extracted[0] if isinstance(extracted, list) else extracted
        if first.get("status") != "OK" or not first.get("sheets"):
            raise RuntimeError(f"Excel COM 回读 course_index.csv 失败: {first.get('error', '')}")
        return max(0, int(first["sheets"][0]["row_count"]) - 1)


def validate_archive(store: ArchiveStore) -> Dict[str, object]:
    records = store.load_manifest()
    errors: List[str] = []
    warnings: List[str] = []
    seen_ids = set()
    seen_orders = set()
    image_count = 0

    for record in records:
        if record.article_id in seen_ids:
            errors.append(f"重复文章 ID: {record.article_id}")
        seen_ids.add(record.article_id)
        if record.order in seen_orders:
            errors.append(f"重复顺序号: {record.order}")
        seen_orders.add(record.order)
        markdown_path = store.root / Path(record.markdown_path)
        if not markdown_path.exists():
            errors.append(f"Markdown 缺失: {record.markdown_path}")
            continue
        markdown = markdown_path.read_text(encoding="utf-8")
        for line in markdown.splitlines():
            if is_ui_noise_markdown_line(line, store.ui_noise_patterns):
                errors.append(f"正文残留界面控件或账户问候: {record.title}: {line.strip()[:80]}")
                break
        if record.content_type != "image_only" and record.text_char_count <= 0:
            errors.append(f"普通正文为空: {record.title}")
        links = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown)
        for link in links:
            if link.startswith(("http://", "https://", "data:")):
                errors.append(f"正文仍含远程或 data 图片: {record.title}: {link[:120]}")
                continue
            resolved = (markdown_path.parent / link).resolve()
            if not resolved.exists():
                errors.append(f"Markdown 图片链接失效: {record.title}: {link}")
        successful_images = 0
        for image in record.images:
            image_count += 1
            if image.status != "success":
                errors.append(f"图片失败: {record.title}: {image.original_url}: {image.error}")
                continue
            path = store.root / Path(image.local_path)
            if not path.exists():
                errors.append(f"图片文件缺失: {image.local_path}")
                continue
            data = path.read_bytes()
            if len(data) != image.bytes:
                errors.append(f"图片大小不一致: {image.local_path}")
            if sha256_bytes(data) != image.sha256:
                errors.append(f"图片 SHA256 不一致: {image.local_path}")
            if not validate_image_signature(data, image.mime_type):
                errors.append(f"图片签名无效: {image.local_path}")
            if image.width <= 0 or image.height <= 0:
                errors.append(f"图片尺寸无效: {image.local_path}: {image.width}x{image.height}")
            successful_images += 1
        if record.content_type == "image_only" and successful_images == 0:
            errors.append(f"图片型正文没有成功图片: {record.title}")

    expected_orders = set(range(1, len(records) + 1))
    if seen_orders != expected_orders:
        missing = sorted(expected_orders - seen_orders)
        extra = sorted(seen_orders - expected_orders)
        errors.append(f"顺序号不连续: missing={missing[:20]}, extra={extra[:20]}")

    referenced_markdown = {(store.root / Path(item.markdown_path)).resolve() for item in records}
    actual_markdown = {path.resolve() for path in store.articles_dir.glob("*.md")}
    for orphan in sorted(actual_markdown - referenced_markdown):
        errors.append(f"孤立 Markdown 文件: {orphan.name}")
    if len(actual_markdown) != len(records):
        errors.append(f"逐讲文件数 {len(actual_markdown)} != 清单数 {len(records)}")

    merged_path = store.archive_dir / "全集.md"
    if not merged_path.exists():
        errors.append("全集.md 缺失")
    else:
        merged_count = merged_path.read_text(encoding="utf-8").count("> 原文：")
        if merged_count != len(records):
            errors.append(f"全集.md 篇数 {merged_count} != 清单数 {len(records)}")

    index_path = store.archive_dir / "course_index.csv"
    if not index_path.exists():
        errors.append("course_index.csv 缺失")
    else:
        index_count = _index_row_count(index_path, store.qa_dir)
        if index_count != len(records):
            errors.append(f"course_index.csv 行数 {index_count} != 清单数 {len(records)}")

    catalog_path = store.archive_dir / "catalog.json"
    published_count = 0
    catalog_count = 0
    total_count = 0
    if catalog_path.exists():
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        published_count = int(catalog.get("published_count", 0))
        total_count = int(catalog.get("total_count", 0))
        catalog_count = len(catalog.get("items", []))
        if catalog_count != published_count:
            errors.append(f"目录数 {catalog_count} != 页面已更新数 {published_count}")
        if len(records) != published_count:
            errors.append(f"本地记录数 {len(records)} != 页面已更新数 {published_count}")
    else:
        warnings.append("尚无 catalog.json，无法核对页面讲数")

    result = {
        "status": "PASS" if not errors else "FAIL",
        "validated_at": now_text(),
        "record_count": len(records),
        "published_count": published_count,
        "total_count": total_count,
        "catalog_count": catalog_count,
        "unique_article_ids": len(seen_ids),
        "image_count": image_count,
        "errors": errors,
        "warnings": warnings,
    }
    atomic_write_json(store.qa_dir / "archive_validation.json", result)
    return result
