# 出版阅读版参考

仅在用户要求阅读版时执行本流程，并调用 `windows-office-files`。Markdown、图片和 manifest 是归档权威源；DOCX 是可编辑权威版本；PDF 是从 XPS 产生的交付版，不直接编辑。

## 生成

1. 按发布月或用户指定分卷，从 manifest 顺序读取文章与本地图片。每讲另起页；长图按页宽等比缩放，必要时分段，不能裁掉有效内容。
2. 每个分卷 DOCX 在正文前插入 `TOC \\o "1-3" \\h \\z \\u` 字段，文章标题使用 Heading 样式；`\\h` 不得省略，它使目录项链接到各讲。设置 Word 打开时更新字段。
3. 生成 `总目录.docx`：按分卷列出讲次，并为每个分卷提供可点击的 PDF 链接。链接目标必须是最终原文件名，不能指向临时文件。
4. 用真实 Microsoft Word 只导出 XPS，同时获取 Word 页数并更新目录字段。不用 Word 直接生成交付 PDF，避免本地透明加密软件封装为 `%TSD-Header-###%`。
5. 用 PyMuPDF 将标准 XPS 转为临时 PDF；通过验收后再原子替换正式 PDF。现有 TSD PDF 先移入 `qa/encrypted_pdf_backup/<timestamp>/`，保留相对目录并写入包含路径、大小、SHA256 和签名的 `manifest.json`。

## 验收

- DOCX 必须是标准 ZIP/OOXML；分卷 DOCX 必须包含带 `\\h` 的 TOC 字段，总目录 DOCX 的外部超链接数不少于分卷数。
- XPS 必须以 `PK` 开头；逐页渲染，检查空白页、标题孤行、图片裁切、字体替换和页眉页脚。
- 交付 PDF 必须以 `%PDF-` 开头，`pypdf.PdfReader.is_encrypted` 为 `False`，页数与 Word/XPS 一致。PDF 链接数不少于分卷文章数；总目录 PDF 链接数不少于分卷数。
- `publication_qa.json` 记录 `pdf_signature: NATIVE_PDF`、`pdf_encrypted: false`、`pdf_source: xps`、`pdf_sha256`、`pdf_links`、页数和 DOCX SHA256。
- 仅在全部检查通过后替换正式成果；转换或链接验收失败时保留上一版成功交付，不静默降级为图片 PDF。

## 运行

用户明确要求阅读版时执行：

```powershell
python <skill>/scripts/run_course_archiver.py --config <config> --mode publish
```

只重建包含指定文章 ID 的分卷时，可重复传入 `--changed-id <article_id>`。若只需 DOCX，使用 `--no-fixed-export`。
