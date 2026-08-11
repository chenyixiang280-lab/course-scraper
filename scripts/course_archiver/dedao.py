from __future__ import annotations

import json
import logging
import os
import re
import getpass
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from course_scraper import CourseScraper, ContentExtractionError, LoginTimeoutError, NetworkError

from .content import clean_article_html
from .models import ArticleRecord, CatalogItem
from .storage import ArchiveStore
from .utils import atomic_write_json, normalize_title, now_text


COUNT_PATTERNS = (
    re.compile(r"已更新\s*(\d+)\s*讲\s*/?\s*共\s*(\d+)\s*讲"),
    re.compile(r"(\d+)\s*/\s*(\d+)\s*更新\s*/\s*总讲数"),
    re.compile(r"共\s*(\d+)\s*讲"),
)

# 得到的文章容器会在正文前插入独立的账号问候节点。规则只匹配短、独立的问候，
# 不包含任何账号名称；调用方也可用 ui_noise_patterns 追加站点特有的固定文案。
DEFAULT_DEDAO_UI_NOISE_PATTERNS = (
    r"^[^。！？\n]{1,32}[，,](?:(?:你|您)?好|(?:新年|春节)好)[！!。.]?$",
)


class EnhancedDedaoScraper(CourseScraper):
    """基于 course-scraper 的得到课程专用增强实现。"""

    def __init__(
        self,
        start_url: str,
        output_root: Path,
        course_title: str,
        profile_dir: Path,
        storage_state_path: Path,
        headless: bool,
        login_timeout: int = 600,
        recent_recheck_count: int = 7,
        ui_noise_patterns: Sequence[str] = (),
    ):
        self.ui_noise_patterns = tuple(DEFAULT_DEDAO_UI_NOISE_PATTERNS) + tuple(ui_noise_patterns)
        self.store = ArchiveStore(Path(output_root), course_title, self.ui_noise_patterns)
        log_path = self.store.logs_dir / "scraper.log"
        super().__init__(
            start_url=start_url,
            output_dir=str(self.store.archive_dir),
            output_filename="course_scraper_base.txt",
            headless=headless,
            login_timeout=login_timeout,
            log_file=str(log_path),
        )
        self.course_title = course_title
        self.profile_dir = Path(profile_dir).resolve()
        self.storage_state_path = Path(storage_state_path).resolve()
        self.recent_recheck_count = recent_recheck_count
        self.login_timeout_seconds = login_timeout
        self._configure_logger(log_path)

    def _configure_logger(self, log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if not any(isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == log_path for handler in self.logger.handlers):
            formatter = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
            handler = logging.FileHandler(log_path, encoding="utf-8")
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

    def login_only(self) -> Dict[str, object]:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = self._launch_context(playwright, headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(self.start_url, wait_until="domcontentloaded", timeout=60000)
                page.bring_to_front()
                self._open_login_prompt(page)
                published, total = self._wait_for_access(page, self.login_timeout_seconds)
                self._save_storage_state(context)
            finally:
                context.close()

            # 只有完全关闭浏览器后，用同一配置重新启动且无需扫码，才认定会话已持久化。
            time.sleep(1)
            verify_context = self._launch_context(playwright, headless=True)
            verify_page = verify_context.pages[0] if verify_context.pages else verify_context.new_page()
            try:
                verify_page.goto(self.start_url, wait_until="domcontentloaded", timeout=60000)
                verified_published, verified_total = self._wait_for_access(verify_page, 60)
                if (verified_published, verified_total) != (published, total):
                    raise LoginTimeoutError("重新打开浏览器后课程讲数不一致，登录状态复验失败")
                self._save_storage_state(verify_context)
                result = {
                    "status": "LOGIN_PERSISTED",
                    "published_count": published,
                    "total_count": total,
                    "persistence_verified": True,
                }
                self.store.write_status("LOGIN_PERSISTED", **result)
                return result
            except Exception as exc:
                self.store.write_status("AUTH_STATE_NOT_PERSISTED", error=str(exc))
                raise
            finally:
                verify_context.close()

    @staticmethod
    def _open_login_prompt(page: Page) -> None:
        if EnhancedDedaoScraper._has_course_entitlement(page):
            return
        for label in ("立即登录", "登录"):
            try:
                locator = page.get_by_text(label, exact=True)
                visible = [item for item in locator.all() if item.is_visible()]
                if visible:
                    visible[0].click(timeout=5000)
                    page.wait_for_timeout(500)
                    return
            except Exception:
                continue

    def run_archive(self, mode: str = "incremental") -> Dict[str, object]:
        if mode not in {"full", "incremental"}:
            raise ValueError(f"不支持的抓取模式: {mode}")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        records = self.store.load_manifest()
        original_by_id = {item.article_id: item for item in records}
        failures: List[str] = []
        changed_ids: List[str] = []
        self.store.write_status("RUNNING", mode=mode, existing_records=len(records))

        with sync_playwright() as playwright:
            context = self._launch_context(playwright, headless=self.headless)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(self.start_url, wait_until="domcontentloaded", timeout=60000)
                published_count, total_count = self._wait_for_access(
                    page,
                    min(self.login_timeout_seconds, 60) if self.headless else self.login_timeout_seconds,
                )
                self._save_storage_state(context)
                catalog = self._collect_catalog(page, published_count)
                self.store.save_catalog(catalog, published_count, total_count)
                if len(catalog) != published_count:
                    failures.append(f"目录识别数 {len(catalog)} 与页面已更新讲数 {published_count} 不一致")

                anchor_order = 1
                if mode == "incremental" and records:
                    anchor_index = max(0, len(records) - self.recent_recheck_count)
                    anchor = records[anchor_index]
                    anchor_order = anchor.order
                    page.goto(anchor.url, wait_until="domcontentloaded", timeout=60000)
                    self._wait_for_article(page)
                elif mode == "full" and records:
                    completed_orders = {
                        item.order for item in records if item.status in {"success", "image_only"}
                    }
                    missing_order = next(
                        (order for order in range(1, published_count + 1) if order not in completed_orders),
                        None,
                    )
                    if missing_order is None:
                        anchor_order = 1
                        if not self._open_catalog_order(page, anchor_order, published_count):
                            raise ContentExtractionError("无法从完整目录打开第 1 讲")
                    else:
                        anchor_order = missing_order
                        self.logger.info("从首个缺失序号 %s 继续全量抓取", anchor_order)
                        self._open_catalog_order(page, anchor_order, published_count)
                else:
                    if not self._open_catalog_order(page, anchor_order, published_count):
                        raise ContentExtractionError("无法从完整目录打开第 1 讲")

                catalog_by_title: Dict[str, List[CatalogItem]] = {}
                for item in catalog:
                    catalog_by_title.setdefault(normalize_title(item.title), []).append(item)
                catalog_by_order = {item.order: item for item in catalog}
                maximum_steps = max(1, published_count - anchor_order + 1)
                seen_urls = set()
                processed_steps = 0

                while processed_steps < maximum_steps + 2:
                    current_url = page.url
                    article_id = self._article_id(current_url)
                    if not article_id or current_url in seen_urls:
                        break
                    seen_urls.add(current_url)
                    processed_steps += 1
                    try:
                        existing = original_by_id.get(article_id)
                        record, body_markdown = self._extract_article(
                            page=page,
                            context=context,
                            catalog_by_title=catalog_by_title,
                            catalog_by_order=catalog_by_order,
                            existing=existing,
                            fallback_order=anchor_order + processed_steps - 1,
                        )
                        next_records, changed = self.store.upsert_record(records, record)
                        if changed:
                            self.store.save_article_markdown(record, body_markdown)
                            changed_ids.append(record.article_id)
                        records = next_records
                        self.store.save_manifest(records)
                        self.store.save_progress({
                            "mode": mode,
                            "published_count": published_count,
                            "processed_in_run": processed_steps,
                            "current_order": record.order,
                            "current_article_id": record.article_id,
                            "current_title": record.title,
                            "saved_records": len(records),
                            "updated_at": now_text(),
                        })
                        self.logger.info(
                            "完成 %s/%s：%s（正文 %s 字，图片 %s 张，%s）",
                            record.order,
                            published_count,
                            record.title,
                            record.text_char_count,
                            len(record.images),
                            "有更新" if changed else "无变化",
                        )
                    except Exception as exc:
                        message = f"{current_url}: {type(exc).__name__}: {exc}"
                        failures.append(message)
                        self.logger.exception("正文抓取失败：%s", current_url)

                    if len(seen_urls) >= maximum_steps:
                        break
                    if not self._go_next(page, current_url):
                        next_order = anchor_order + processed_steps
                        if next_order > published_count:
                            break
                        self.logger.info("下一篇控件不可用，改从课程目录打开第 %s 讲", next_order)
                        if not self._open_catalog_order(page, next_order, published_count):
                            failures.append(f"无法从目录打开第 {next_order} 讲")
                            break

                self.store.save_manifest(records)
                self.store.rebuild_index_and_merged(records)
                report_path = self.store.write_update_report(
                    run_mode=mode,
                    published_count=published_count,
                    records=records,
                    changed_ids=changed_ids,
                    failures=failures,
                )
                completeness_path = self.store.write_completeness_report(
                    published_count=published_count,
                    total_count=total_count,
                    catalog_count=len(catalog),
                    records=records,
                    failures=failures,
                )
                successful = [item for item in records if item.status in {"success", "image_only"}]
                status = "SUCCESS" if len(successful) == published_count and not failures else "INCOMPLETE"
                result = {
                    "status": status,
                    "mode": mode,
                    "published_count": published_count,
                    "total_count": total_count,
                    "catalog_count": len(catalog),
                    "record_count": len(successful),
                    "changed_count": len(changed_ids),
                    "failure_count": len(failures),
                    "changed_ids": changed_ids,
                    "failures": failures,
                    "update_report": str(report_path),
                    "completeness_report": str(completeness_path),
                }
                self._save_storage_state(context)
                self.store.write_status(status, **result)
                return result
            except LoginTimeoutError as exc:
                self.store.write_status("AUTH_REQUIRED", error=str(exc))
                raise
            except Exception as exc:
                self.store.write_status("FAILED", error=f"{type(exc).__name__}: {exc}")
                raise
            finally:
                context.close()

    def _launch_context(self, playwright, headless: bool) -> BrowserContext:
        options = {
            "user_data_dir": str(self.profile_dir),
            "headless": headless,
            "viewport": {"width": 1440, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
            ),
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        try:
            context = playwright.chromium.launch_persistent_context(channel="chrome", **options)
        except Exception as chrome_error:
            self.logger.warning("系统 Chrome 启动失败，尝试 Playwright Chromium：%s", chrome_error)
            try:
                context = playwright.chromium.launch_persistent_context(**options)
            except Exception as chromium_error:
                raise NetworkError(f"浏览器启动失败: {chromium_error}") from chromium_error
        self._restore_storage_state(context)
        return context

    def _restore_storage_state(self, context: BrowserContext) -> None:
        """把显式快照补回持久化配置；固定 profile 仍是主会话载体。"""
        if not self.storage_state_path.is_file():
            return
        try:
            state = json.loads(self.storage_state_path.read_text(encoding="utf-8"))
            cookies = state.get("cookies") or []
            if cookies:
                context.add_cookies(cookies)
            origins = {
                item.get("origin"): item.get("localStorage") or []
                for item in (state.get("origins") or [])
                if item.get("origin")
            }
            if origins:
                payload = json.dumps(origins, ensure_ascii=False).replace("</", "<\\/")
                context.add_init_script(
                    script=(
                        "const saved = " + payload + ";"
                        "const entries = saved[location.origin] || [];"
                        "for (const item of entries) localStorage.setItem(item.name, item.value);"
                    )
                )
            self.logger.info("已载入持久化登录状态（Cookie %s 项）", len(cookies))
        except Exception as exc:
            self.logger.warning("登录状态快照载入失败，将继续使用固定浏览器配置：%s", type(exc).__name__)

    def _save_storage_state(self, context: BrowserContext) -> None:
        """原子保存站点原始会话有效期，不延长或伪造 Cookie 过期时间。"""
        self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            state = context.storage_state(indexed_db=True)
        except TypeError:
            state = context.storage_state()
        # 不让浏览器子进程直接创建文件，避免 Chrome 沙箱产生当前用户不可读的 DACL。
        atomic_write_json(self.storage_state_path, state)
        self._restrict_state_file()
        self.logger.info("登录状态已持久化保存")

    def _restrict_state_file(self) -> None:
        if os.name != "nt":
            try:
                self.storage_state_path.chmod(0o600)
            except OSError:
                pass
            return
        completed = subprocess.run(
            [
                "icacls.exe",
                str(self.storage_state_path),
                "/inheritance:r",
                "/grant:r",
                f"{getpass.getuser()}:F",
                "/C",
                "/Q",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            raise PermissionError("无法限制登录状态文件权限")

    def _wait_for_access(self, page: Page, timeout_seconds: int) -> Tuple[int, int]:
        deadline = time.monotonic() + timeout_seconds
        last_text = ""
        while time.monotonic() < deadline:
            try:
                last_text = page.locator("body").inner_text(timeout=3000)
            except Exception:
                last_text = ""
            counts = self._parse_counts(last_text)
            if counts and counts[0] > 0 and self._has_course_entitlement(page):
                return counts
            time.sleep(1)
        raise LoginTimeoutError(
            "在等待时间内未确认课程正文访问权限。请在可见浏览器中登录已购买该课程的得到账号，直到页面显示“继续学习”。"
        )

    @staticmethod
    def _has_course_entitlement(page: Page) -> bool:
        try:
            article = page.locator(".article-body .editor-show, .article-body").first
            if article.count() and article.is_visible() and len(article.inner_text(timeout=2000).strip()) > 300:
                return True
        except Exception:
            pass
        for label in ("继续学习", "开始学习", "去学习"):
            try:
                locator = page.get_by_text(label, exact=True)
                if locator.count() and locator.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _parse_counts(text: str) -> Optional[Tuple[int, int]]:
        for index, pattern in enumerate(COUNT_PATTERNS):
            match = pattern.search(text or "")
            if not match:
                continue
            if index == 2:
                value = int(match.group(1))
                return value, value
            return int(match.group(1)), int(match.group(2))
        return None

    def _collect_catalog(self, page: Page, published_count: int) -> List[CatalogItem]:
        for label in ("展开目录", "课程内容"):
            try:
                locator = page.get_by_text(label, exact=True)
                if locator.count() and locator.first.is_visible():
                    locator.first.click(timeout=3000)
                    page.wait_for_timeout(500)
            except Exception:
                pass
        page.evaluate("window.scrollTo(0, 0)")
        collected: List[CatalogItem] = []
        stable_rounds = 0

        for _ in range(60):
            state = page.evaluate(CATALOG_SCAN_JS)
            latest: List[CatalogItem] = []
            for raw in state.get("items", []):
                title = (raw.get("title") or "").strip()
                if not title:
                    continue
                latest.append(CatalogItem(
                    order=len(latest) + 1,
                    title=title,
                    section=(raw.get("section") or "未分组").strip(),
                    metadata=(raw.get("metadata") or "").strip(),
                ))
            stable_rounds = stable_rounds + 1 if len(latest) == len(collected) else 0
            collected = latest
            if len(collected) >= published_count:
                break
            if stable_rounds >= 10:
                break
            page.wait_for_timeout(1000)
        self.logger.info("目录识别完成：%s / %s", len(collected), published_count)
        return collected

    def _open_first_article(self, page: Page, catalog: Sequence[CatalogItem]) -> None:
        page.evaluate(SCROLL_CATALOG_TOP_JS)
        page.wait_for_timeout(800)
        candidates = page.locator("li.single-content")
        if not candidates.count() and catalog:
            candidates = page.get_by_text(catalog[0].title, exact=True)
        if not candidates.count():
            raise ContentExtractionError("未找到第一讲的可点击目录项")
        candidates.first.click(timeout=10000)
        self._wait_for_article(page)

    def _open_catalog_order(self, page: Page, order: int, published_count: int) -> bool:
        for attempt in range(1, 4):
            try:
                items = page.locator("li.single-content")
                if items.count() < order:
                    page.goto(self.start_url, wait_until="domcontentloaded", timeout=60000)
                    self._wait_for_access(page, min(self.login_timeout_seconds, 60))
                    page.evaluate("window.scrollTo(0, 0)")
                    for _ in range(60):
                        items = page.locator("li.single-content")
                        if items.count() >= order:
                            break
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(1000)
                if items.count() >= order:
                    target = items.nth(order - 1)
                    target.scroll_into_view_if_needed(timeout=10000)
                    target.click(timeout=10000)
                    self._wait_for_article(page)
                    return True
                self.logger.warning(
                    "课程目录第 %s 次只加载到 %s 项，目标为第 %s 讲",
                    attempt,
                    items.count(),
                    order,
                )
            except Exception as exc:
                self.logger.warning("从课程目录打开第 %s 讲第 %s 次失败：%s", order, attempt, exc)
        self.logger.error("无法从课程目录打开第 %s 讲", order)
        return False

    @staticmethod
    def _wait_for_article(page: Page) -> None:
        try:
            page.wait_for_url(re.compile(r"/course/article\?id="), timeout=30000)
            try:
                page.wait_for_function(ARTICLE_READY_JS, timeout=15000)
            except PlaywrightTimeoutError:
                # 直播、视频和部分音频课需要主动点开“文稿”后才渲染正文。
                manuscript = page.get_by_text("文稿", exact=True)
                for item in manuscript.all():
                    if item.is_visible():
                        item.click(timeout=5000)
                        break
                page.wait_for_function(ARTICLE_READY_JS, timeout=45000)
        except PlaywrightTimeoutError as exc:
            raise ContentExtractionError("文章正文（含直播文稿）加载超时") from exc

    def _extract_article(
        self,
        page: Page,
        context: BrowserContext,
        catalog_by_title: Dict[str, List[CatalogItem]],
        catalog_by_order: Dict[int, CatalogItem],
        existing: Optional[ArticleRecord],
        fallback_order: int,
    ) -> Tuple[ArticleRecord, str]:
        self._wait_for_article(page)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
        locator = page.locator(
            ".article-body-wrap .editor-show:visible, .article-body .editor-show:visible"
        ).first
        if not locator.count():
            raise ContentExtractionError("未找到可见正文容器")
        locator.evaluate(ANNOTATE_IMAGES_JS)
        html = locator.inner_html(timeout=15000)
        if not html.strip():
            raise ContentExtractionError("正文容器为空")
        title = self._article_title(page, locator)
        article_id = self._article_id(page.url)
        catalog_matches = catalog_by_title.get(normalize_title(title), [])
        catalog_item = catalog_matches[0] if catalog_matches else catalog_by_order.get(fallback_order)
        if catalog_item:
            title = catalog_item.title
        order = catalog_item.order if catalog_item else (existing.order if existing else fallback_order)
        section = catalog_item.section if catalog_item else (existing.section if existing else "未分组")
        image_dir = self.store.images_dir / article_id

        def downloader(url: str) -> Tuple[bytes, str]:
            response = context.request.get(
                url,
                headers={"Referer": page.url, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"},
                timeout=30000,
                fail_on_status_code=False,
            )
            if not response.ok:
                raise NetworkError(f"HTTP {response.status}")
            return response.body(), response.headers.get("content-type", "application/octet-stream")

        markdown_text, text_count, images, body_sha = clean_article_html(
            html=html,
            page_url=page.url,
            title=title,
            article_id=article_id,
            image_dir=image_dir,
            markdown_image_prefix=f"../images/{article_id}",
            downloader=downloader,
            existing_images=existing.images if existing else None,
            ui_noise_patterns=self.ui_noise_patterns,
        )
        successful_images = [image for image in images if image.status == "success"]
        content_type = self._content_type(title, text_count, len(successful_images))
        if text_count <= 0 and not successful_images:
            raise ContentExtractionError("普通正文与正文图片均为空")
        published_at = self._published_at(page)
        markdown_path = self.store.article_relative_path(order, article_id, title)
        record = ArticleRecord(
            order=order,
            article_id=article_id,
            url=page.url,
            title=title,
            section=section,
            content_type=content_type,
            published_at=published_at,
            markdown_path=markdown_path,
            text_char_count=text_count,
            body_sha256=body_sha,
            scrape_time=now_text(),
            status="image_only" if content_type == "image_only" else "success",
            images=images,
        )
        return record, markdown_text

    @staticmethod
    def _article_title(page: Page, locator) -> str:
        document_title = (page.title() or "").strip()
        document_title = re.sub(r"\s*-\s*得到APP.*$", "", document_title).strip()
        if document_title and document_title != "得到APP":
            return document_title
        text = locator.inner_text(timeout=10000)
        for line in (item.strip() for item in text.splitlines()):
            if not line:
                continue
            if re.fullmatch(r"\d{1,2}(?:时\d{1,2}分\d{1,2}秒|:\d{2}(?::\d{2})?)", line):
                continue
            if line.endswith("亲述"):
                continue
            return line[:200]
        return "未知标题"

    @staticmethod
    def _article_id(url: str) -> str:
        return (parse_qs(urlparse(url).query).get("id") or [""])[0]

    @staticmethod
    def _content_type(title: str, text_count: int, image_count: int) -> str:
        if text_count < 120 and image_count:
            return "image_only"
        if "直播回放" in title:
            return "live_replay"
        if "直播问答" in title or "问答" in title:
            return "live_qa"
        if "长图" in title and image_count:
            return "image_only" if text_count < 500 else "article_with_images"
        return "article"

    @staticmethod
    def _published_at(page: Page) -> str:
        try:
            body_text = page.locator("body").inner_text(timeout=5000)
        except Exception:
            return ""
        match = re.search(r"首次发布[:：]\s*([^\n]+)", body_text)
        return match.group(1).strip() if match else ""

    def _go_next(self, page: Page, before_url: str) -> bool:
        try:
            before_content = page.evaluate(ARTICLE_CONTENT_SIGNATURE_JS)
            module = page.locator("div.button-module").filter(has_text="下一篇")
            if not module.count() or not module.last.is_visible():
                return False
            button = module.last.get_by_role("button")
            if not button.count():
                return False
            button.click(timeout=5000, force=True)
            page.wait_for_function("before => location.href !== before", arg=before_url, timeout=15000)
            page.wait_for_function(
                "before => { const nodes = Array.from(document.querySelectorAll('.article-body-wrap .editor-show, .article-body .editor-show')); const e = nodes.find(x => x.offsetParent !== null); if (!e) return false; const text = (e.innerText || '').trim(); const images = Array.from(e.querySelectorAll('img')); const signature = [text.length, text.slice(0, 500), images.length, images.at(-1)?.currentSrc || images.at(-1)?.src || ''].join('|'); return signature !== before; }",
                arg=before_content,
                timeout=60000,
            )
            self._wait_for_article(page)
            return page.url != before_url
        except Exception:
            return False


CATALOG_SCAN_JS = r"""
() => {
  const lis = Array.from(document.querySelectorAll('li.single-content'));
  const items = lis.map(li => {
    const title = (li.querySelector('.lesson-title')?.innerText || '').trim();
    const section = (li.closest('li.single-sec')?.querySelector('.sec-title')?.innerText || '未分组').trim();
    const metadata = (li.querySelector('.lesson-detail')?.innerText || '').trim().replace(/\s+/g, ' ');
    return {title, section, metadata};
  });
  window.scrollTo(0, document.body.scrollHeight);
  return {items, scrollY: window.scrollY, scrollHeight: document.body.scrollHeight};
}
"""


SCROLL_CATALOG_TOP_JS = r"""
() => {
  window.scrollTo(0, 0);
}
"""


ANNOTATE_IMAGES_JS = r"""
(element) => {
  element.querySelectorAll('img').forEach(img => {
    img.setAttribute('data-archive-src', img.currentSrc || img.src || img.getAttribute('data-src') || '');
    img.setAttribute('data-archive-width', String(img.naturalWidth || img.width || 0));
    img.setAttribute('data-archive-height', String(img.naturalHeight || img.height || 0));
  });
}
"""


ARTICLE_READY_JS = r"""
() => Array.from(document.querySelectorAll('.article-body-wrap .editor-show, .article-body .editor-show'))
  .some(element => element.offsetParent !== null && (
    (element.innerText || '').trim().length > 0 || element.querySelector('img')
  ))
"""


ARTICLE_CONTENT_SIGNATURE_JS = r"""
() => {
  const nodes = Array.from(document.querySelectorAll('.article-body-wrap .editor-show, .article-body .editor-show'));
  const element = nodes.find(item => item.offsetParent !== null);
  if (!element) return '';
  const text = (element.innerText || '').trim();
  const images = Array.from(element.querySelectorAll('img'));
  const last = images.length ? (images[images.length - 1].currentSrc || images[images.length - 1].src || '') : '';
  return [text.length, text.slice(0, 500), images.length, last].join('|');
}
"""
