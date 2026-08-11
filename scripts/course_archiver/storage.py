from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from PIL import Image

from .content import is_ui_noise_markdown_line, markdown_line_content
from .models import ArticleRecord, CatalogItem
from .utils import atomic_write_json, atomic_write_text, now_text, safe_filename, sha256_text


class ArchiveStore:
    """稳定保存抓取结果，并在成功后原子更新汇总文件。"""

    def __init__(self, root: Path, course_title: str, ui_noise_patterns: Sequence[str] = ()):
        self.root = Path(root).resolve()
        self.course_title = course_title
        self.ui_noise_patterns = tuple(ui_noise_patterns)
        self.archive_dir = self.root / "archive"
        self.articles_dir = self.archive_dir / "articles"
        self.images_dir = self.archive_dir / "images"
        self.editions_dir = self.root / "editions"
        self.reports_dir = self.root / "reports"
        self.status_dir = self.root / "status"
        self.logs_dir = self.root / "logs"
        self.qa_dir = self.root / "qa"
        self.manifest_path = self.archive_dir / "manifest.jsonl"
        self.progress_path = self.root / "progress.json"
        for directory in (
            self.articles_dir,
            self.images_dir,
            self.editions_dir,
            self.reports_dir,
            self.status_dir,
            self.logs_dir,
            self.qa_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def load_manifest(self) -> List[ArticleRecord]:
        if not self.manifest_path.exists():
            return []
        records: List[ArticleRecord] = []
        for line_number, line in enumerate(self.manifest_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(ArticleRecord.from_dict(json.loads(line)))
            except Exception as exc:
                raise ValueError(f"manifest.jsonl 第 {line_number} 行无效: {exc}") from exc
        return sorted(records, key=lambda item: (item.order, item.article_id))

    def save_manifest(self, records: Sequence[ArticleRecord]) -> None:
        lines = [json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) for record in sorted(records, key=lambda x: (x.order, x.article_id))]
        atomic_write_text(self.manifest_path, "\n".join(lines) + ("\n" if lines else ""))

    def upsert_record(
        self,
        records: Sequence[ArticleRecord],
        incoming: ArticleRecord,
    ) -> Tuple[List[ArticleRecord], bool]:
        result = list(records)
        changed = True
        for index, existing in enumerate(result):
            if existing.article_id == incoming.article_id:
                changed = (
                    existing.body_sha256 != incoming.body_sha256
                    or existing.title != incoming.title
                    or existing.section != incoming.section
                    or existing.published_at != incoming.published_at
                    or existing.status != incoming.status
                    or existing.markdown_path != incoming.markdown_path
                    or existing.images != incoming.images
                )
                result[index] = incoming if changed else existing
                break
        else:
            result.append(incoming)
        result.sort(key=lambda item: (item.order, item.article_id))
        return result, changed

    def article_relative_path(self, order: int, article_id: str, title: str) -> str:
        filename = f"{order:04d}_{safe_filename(article_id, 36)}_{safe_filename(title, 70)}.md"
        return (Path("archive") / "articles" / filename).as_posix()

    def save_article_markdown(self, record: ArticleRecord, body_markdown: str) -> Path:
        path = self.root / Path(record.markdown_path)
        image_note = f"正文图片：{len(record.images)} 张" if record.images else "正文图片：0 张"
        header = (
            f"# {record.title}\n\n"
            f"> 课程：{self.course_title}  \n"
            f"> 章节：{record.section or '未分组'}  \n"
            f"> 原文：{record.url}  \n"
            f"> 首次发布：{record.published_at or '页面未标明'}  \n"
            f"> 类型：{record.content_type}  \n"
            f"> {image_note}\n\n"
        )
        atomic_write_text(path, header + body_markdown.strip() + "\n")
        return path

    def save_catalog(self, items: Sequence[CatalogItem], published_count: int, total_count: int) -> None:
        payload = {
            "captured_at": now_text(),
            "published_count": published_count,
            "total_count": total_count,
            "items": [item.to_dict() for item in items],
        }
        atomic_write_json(self.archive_dir / "catalog.json", payload)

    def save_progress(self, payload: Dict) -> None:
        atomic_write_json(self.progress_path, payload)

    def repair_local_metadata(self) -> Dict[str, int]:
        """只读本地原图并以已保存目录校正章节、标题和真实像素尺寸。"""
        records = self.load_manifest()
        catalog_path = self.archive_dir / "catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog_by_order = {
            int(item["order"]): item for item in catalog.get("items", []) if item.get("order")
        }
        metadata_changes = 0
        dimension_changes = 0
        noise_lines_removed = 0
        for record in records:
            item = catalog_by_order.get(record.order)
            if item:
                new_title = str(item.get("title") or record.title).strip()
                new_section = str(item.get("section") or record.section).strip()
                if new_title != record.title or new_section != record.section:
                    record.title = new_title
                    record.section = new_section
                    metadata_changes += 1
            for image_record in record.images:
                if image_record.status != "success":
                    continue
                image_path = self.root / Path(image_record.local_path)
                with Image.open(image_path) as local_image:
                    width, height = local_image.size
                if (image_record.width, image_record.height) != (width, height):
                    image_record.width, image_record.height = width, height
                    dimension_changes += 1

            article_path = self.root / Path(record.markdown_path)
            if article_path.exists():
                lines = article_path.read_text(encoding="utf-8").splitlines()
                cleaned_lines = []
                removed_chars = 0
                for line in lines:
                    if is_ui_noise_markdown_line(line, self.ui_noise_patterns):
                        removed_chars += len(markdown_line_content(line))
                        noise_lines_removed += 1
                        continue
                    cleaned_lines.append(line)
                lines = cleaned_lines
                record.text_char_count = max(0, record.text_char_count - removed_chars)
                if lines and lines[0].startswith("# "):
                    lines[0] = f"# {record.title}"
                for index, line in enumerate(lines):
                    if line.startswith("> 章节："):
                        lines[index] = f"> 章节：{record.section or '未分组'}  "
                        break
                final_text = "\n".join(lines).rstrip() + "\n"
                atomic_write_text(article_path, final_text)
                body_index = 1 if lines and lines[0].startswith("# ") else 0
                while body_index < len(lines) and (
                    not lines[body_index].strip() or lines[body_index].lstrip().startswith(">")
                ):
                    body_index += 1
                body_markdown = "\n".join(lines[body_index:]).strip()
                digest_material = body_markdown + "\n" + "\n".join(
                    f"{item.sequence}:{item.sha256}:{item.status}" for item in record.images
                )
                record.body_sha256 = sha256_text(digest_material)

        self.save_manifest(records)
        self.rebuild_index_and_merged(records)
        self.write_completeness_report(
            published_count=int(catalog.get("published_count", 0)),
            total_count=int(catalog.get("total_count", 0)),
            catalog_count=len(catalog.get("items", [])),
            records=records,
            failures=[],
        )
        return {
            "records": len(records),
            "metadata_changes": metadata_changes,
            "dimension_changes": dimension_changes,
            "noise_lines_removed": noise_lines_removed,
        }

    def write_status(self, state: str, **extra: object) -> None:
        payload = {"status": state, "updated_at": now_text(), **extra}
        atomic_write_json(self.status_dir / "latest_run.json", payload)

    def rebuild_index_and_merged(self, records: Sequence[ArticleRecord]) -> None:
        ordered = sorted(records, key=lambda item: (item.order, item.article_id))
        fieldnames = [
            "order", "section", "article_id", "title", "content_type", "published_at",
            "url", "markdown_path", "text_char_count", "image_count", "body_sha256",
            "scrape_time", "status", "error",
        ]
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=fieldnames)
        writer.writeheader()
        for record in ordered:
            writer.writerow({
                "order": record.order,
                "section": record.section,
                "article_id": record.article_id,
                "title": record.title,
                "content_type": record.content_type,
                "published_at": record.published_at,
                "url": record.url,
                "markdown_path": record.markdown_path,
                "text_char_count": record.text_char_count,
                "image_count": len(record.images),
                "body_sha256": record.body_sha256,
                "scrape_time": record.scrape_time,
                "status": record.status,
                "error": record.error,
            })
        atomic_write_text(self.archive_dir / "course_index.csv", "\ufeff" + buffer.getvalue())

        md_parts = [f"# {self.course_title} 全集", "", f"> 整理时间：{now_text()}", ""]
        txt_parts = [self.course_title + " 全集", "整理时间：" + now_text(), ""]
        for record in ordered:
            article_path = self.root / Path(record.markdown_path)
            if not article_path.exists():
                continue
            article_text = article_path.read_text(encoding="utf-8").strip()
            md_parts.extend([article_text, "", "---", ""])
            plain = self._markdown_to_plain(article_text)
            txt_parts.extend([plain, "", "—END—", ""])
        atomic_write_text(self.archive_dir / "全集.md", "\n".join(md_parts).rstrip() + "\n")
        atomic_write_text(self.archive_dir / "全集.txt", "\n".join(txt_parts).rstrip() + "\n")

    @staticmethod
    def _markdown_to_plain(markdown_text: str) -> str:
        import re

        text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"[图片：\1]", markdown_text)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
        text = re.sub(r"[*_`]+", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def write_update_report(
        self,
        run_mode: str,
        published_count: int,
        records: Sequence[ArticleRecord],
        changed_ids: Sequence[str],
        failures: Sequence[str],
    ) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = self.reports_dir / f"update_{stamp}.md"
        lines = [
            f"# 课程更新报告 {stamp}",
            "",
            f"- 运行模式：{run_mode}",
            f"- 页面已更新讲数：{published_count}",
            f"- 本地成功记录数：{sum(1 for item in records if item.status in {'success', 'image_only'})}",
            f"- 新增或修订数：{len(changed_ids)}",
            f"- 失败数：{len(failures)}",
            "",
        ]
        if changed_ids:
            lines.extend(["## 新增或修订", ""] + [f"- {item}" for item in changed_ids] + [""])
        else:
            lines.extend(["## 本周无更新", "", "课程页面未发现新增或修订内容。", ""])
        if failures:
            lines.extend(["## 失败项", ""] + [f"- {item}" for item in failures] + [""])
        atomic_write_text(report_path, "\n".join(lines).rstrip() + "\n")
        return report_path

    def write_completeness_report(
        self,
        published_count: int,
        total_count: int,
        catalog_count: int,
        records: Sequence[ArticleRecord],
        failures: Sequence[str],
    ) -> Path:
        successful = [item for item in records if item.status in {"success", "image_only"}]
        unique_ids = {item.article_id for item in successful}
        duplicate_count = len(successful) - len(unique_ids)
        empty_normal = [item.title for item in successful if item.content_type != "image_only" and item.text_char_count <= 0]
        missing_images = [
            item.title for item in successful
            if item.content_type == "image_only" and not any(image.status == "success" for image in item.images)
        ]
        article_files = list(self.articles_dir.glob("*.md"))
        image_records = [image for item in successful for image in item.images]
        image_failures = [image for image in image_records if image.status != "success"]
        zero_dimension_images = [
            image for image in image_records
            if image.status == "success" and (image.width <= 0 or image.height <= 0)
        ]
        complete = (
            len(successful) == published_count
            and catalog_count == published_count
            and duplicate_count == 0
            and not empty_normal
            and not missing_images
            and not failures
            and not image_failures
            and not zero_dimension_images
            and len(article_files) == len(successful)
        )
        lines = [
            "# 完整性报告",
            "",
            f"- 生成时间：{now_text()}",
            f"- 页面已更新 / 总讲数：{published_count} / {total_count}",
            f"- 目录识别数：{catalog_count}",
            f"- 本地成功记录数：{len(successful)}",
            f"- 唯一文章 ID 数：{len(unique_ids)}",
            f"- 重复文章数：{duplicate_count}",
            f"- 逐讲 Markdown 文件数：{len(article_files)}",
            f"- 正文图片记录数：{len(image_records)}",
            f"- 图片失败数：{len(image_failures)}",
            f"- 图片尺寸缺失数：{len(zero_dimension_images)}",
            f"- 普通正文空内容数：{len(empty_normal)}",
            f"- 图片型正文无图数：{len(missing_images)}",
            f"- 抓取失败数：{len(failures)}",
            f"- 逐讲文件与清单数量一致：{'是' if len(article_files) == len(successful) else '否'}",
            f"- 最终结论：{'通过' if complete else '未通过'}",
            "",
        ]
        if failures:
            lines.extend(["## 抓取失败项", ""] + [f"- {item}" for item in failures] + [""])
        if image_failures:
            lines.extend(["## 图片失败项", ""] + [f"- {item.original_url}: {item.error}" for item in image_failures] + [""])
        report = self.reports_dir / "完整性报告.md"
        atomic_write_text(report, "\n".join(lines).rstrip() + "\n")
        return report
