from pathlib import Path

from course_archiver.content import clean_article_html, validate_image_signature
from course_archiver.dedao import DEFAULT_DEDAO_UI_NOISE_PATTERNS


PNG = b"\x89PNG\r\n\x1a\n" + b"test-payload"


def test_localizes_body_images_and_hashes_content(tmp_path: Path):
    markdown, text_count, images, digest = clean_article_html(
        html="<div><p>可保留的课程正文。</p><img data-src='https://example.test/body.png' width='800' height='600'></div>",
        page_url="https://example.test/article?id=sample",
        title="示例课程",
        article_id="sample",
        image_dir=tmp_path / "archive" / "images" / "sample",
        markdown_image_prefix="../images/sample",
        downloader=lambda _: (PNG, "image/png"),
    )
    assert "可保留的课程正文" in markdown
    assert "../images/sample/001.png" in markdown
    assert text_count > 0 and images[0].sha256 and digest
    assert validate_image_signature(PNG, "image/png")


def test_removes_only_configured_ui_noise(tmp_path: Path):
    markdown, _, _, _ = clean_article_html(
        html="<div><p>示例账户，你好。</p><p>需要保留的正文。</p></div>",
        page_url="https://example.test/article?id=sample",
        title="示例课程",
        article_id="sample",
        image_dir=tmp_path / "archive" / "images" / "sample",
        markdown_image_prefix="../images/sample",
        downloader=lambda _: (PNG, "image/png"),
        ui_noise_patterns=DEFAULT_DEDAO_UI_NOISE_PATTERNS,
    )
    assert "示例账户" not in markdown
    assert "需要保留的正文" in markdown
