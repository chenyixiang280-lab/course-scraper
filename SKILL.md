---
name: course-scraper
description: 归档用户有权访问且通常需要登录的课程文章。用于得到、极客时间等知识平台的全量或增量抓取、持久登录会话、课程目录遍历、直播文字稿和正文插图下载、Markdown/TXT/清单对账，以及生成带链接目录的未加密 DOCX/PDF 阅读版时。
---

# Course Scraper

使用 `scripts/course_archiver` 作为默认实现。它提供得到适配器和通用归档内核；旧版 `scripts/course_scraper` 仅用于兼容已有调用，不能替代可恢复归档流程。

## 边界

- 仅处理用户本人有权访问的内容；通过正常的可见登录完成授权，不绕过登录、付费、风控或访问控制。
- 把浏览器 profile、Cookie/localStorage 快照、日志和交付目录分离。绝不把账号、令牌、Cookie、二维码或个人固定问候写入源码、报告或交付物。
- 将站点特有 selector、固定界面文案和排除规则放进配置或适配器，不把某个课程、账户或页面 DOM 当成通用规则。

## 默认流程（得到）

1. 在用户工作目录创建隔离 Python 环境，安装 `scripts/requirements.txt`，再安装 Playwright Chromium；运行产物始终写在用户工作目录，不能写回本 Skill。
2. 复制 `references/dedao-config.example.json`，填入课程 URL、课程名、输出根目录和会话目录。会话目录必须在交付目录之外，并纳入 `.gitignore`。
3. 首次运行可见登录：`python <skill>/scripts/run_course_archiver.py --config <config> --mode login`。等待 `LOGIN_PERSISTED`；它会关闭浏览器后重新无头打开课程并验证状态真的可恢复。
4. 首次归档：使用 `--mode full`。过程按文章 ID/URL 去重、每讲写入断点和清单；中断后直接重跑，不用删除已有成果。
5. 周期更新：使用 `--mode incremental --headless`。只处理新增文章 ID，并复查最近 `recent_recheck_count` 讲的正文/图片哈希；消失内容只写异常报告，不自动删除本地历史。
6. 运行 `--mode validate` 后再交付。`PASS` 要求目录数、逐讲 Markdown、清单、合并版、图片签名/尺寸/SHA256 和正文哈希一致。

## 结果与清洗标准

- 正文只取已确认的正文容器，排除目录、评论、推荐、页脚、控制按钮及站点固定界面文本。
- 直播回放和问答优先保存页面已有文字稿；图片型正文必须至少保留一张本地可验证图片。
- 只下载正文容器内图片，依次尝试 `currentSrc`、`src`、`data-src`、`data-original` 和 `srcset`；保存原始 URL、MIME、像素、SHA256 与相对路径，Markdown 不能残留 CDN 或 `data:` 图片链接。
- 对站点账号问候或其他固定噪声，使用配置中的 `ui_noise_patterns`（正则全行匹配）；不要硬编码用户名称。

## 平台适配与出版

- 得到之外的平台：先阅读 [适配器参考](references/platform-adapter.md)，仅在真实页面验证目录、正文与“下一篇”选择器后新增适配器。不要用整页最大文本块代替正文容器。
- 用户要求 DOCX/PDF 阅读版时，再使用 `windows-office-files` 生成并验证。先以 Markdown/图片/manifest 为权威源，DOCX 为可编辑源；Word 只导出 XPS，再转成未加密原生 PDF。分卷目录必须链接到各讲，总目录必须链接到各分卷 PDF；详见 [出版参考](references/publication-editions.md)。

## 运行前与交付前检查

- 先抽验普通正文、直播回放、直播问答和图片型正文各一项，确认无目录/评论污染。
- 登录失效时返回 `AUTH_REQUIRED` 并停止；不要用空页面或不完整清单覆盖上一次成功结果。
- 无新增内容时仅保留本周“无更新”报告；不要重建无关成果。
- 修改脚本后运行 `pytest -q`，并用一次无新增增量运行验证幂等性。
