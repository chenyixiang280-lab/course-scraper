import os
import time
import re
from datetime import datetime
from typing import List, Dict, Any, Optional

from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup
import markdownify

from .logger import setup_logger
from .exceptions import (
    LoginTimeoutError,
    CourseNotFoundError,
    ContentExtractionError,
    NetworkError
)

class CourseScraper:
    """
    得到课程自动化抓取模块
    
    该模块封装了 Playwright 的自动化操作，实现了对得到网页端课程的
    自动登录、目录遍历、DOM清洗去重、格式化提取及异常处理。
    """
    
    def __init__(
        self,
        start_url: str,
        output_dir: str = "output",
        output_filename: str = "汇总文档.txt",
        headless: bool = False,
        login_timeout: int = 30,
        log_file: str = "scraper.log"
    ):
        """
        初始化抓取器
        
        :param start_url: 目标课程起始 URL
        :param output_dir: 结果保存目录
        :param output_filename: 汇总文档文件名
        :param headless: 是否使用无头模式 (首次抓取需设为 False 以便扫码登录)
        :param login_timeout: 等待用户扫码登录的超时时间 (秒)
        :param log_file: 日志文件路径
        """
        self.start_url = start_url
        self.output_dir = output_dir
        self.output_filepath = os.path.join(output_dir, output_filename)
        self.headless = headless
        self.login_timeout = login_timeout * 1000  # 转为毫秒
        self.logger = setup_logger(log_file=log_file)
        
        self.extracted_titles = set()
        self.scraped_data: List[Dict[str, Any]] = []
        
        os.makedirs(self.output_dir, exist_ok=True)

    def _polite_delay(self, seconds: float = 2.0):
        """在文章切换后等待页面稳定，避免对用户有权页面施加无必要请求压力。"""
        time.sleep(seconds)

    def _simulate_scroll(self, page: Page):
        """模拟页面滚动，触发懒加载内容"""
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1)
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(1)
        except Exception as e:
            self.logger.warning(f"页面滚动时发生异常 (可忽略): {e}")

    def _extract_clean_content(self, page: Page) -> Dict[str, str]:
        """
        在浏览器环境中执行深度 DOM 清洗，剔除干扰信息并提取纯净的正文与标题
        """
        title = "未知标题"
        try:
            for selector in ['h1', '.title', '.article-title']:
                loc = page.locator(selector).first
                if loc.is_visible(timeout=1000):
                    title = loc.text_content().strip()
                    break
        except Exception:
            pass

        # 注入 JS 清洗无用节点，锁定正文区块
        clean_html = page.evaluate('''() => {
            let maxScore = -1;
            let mainContainer = document.body;
            
            document.querySelectorAll('div, article, main').forEach(container => {
                const cls = (container.className || '').toLowerCase();
                if (cls.includes('sidebar') || cls.includes('menu') || cls.includes('catalog') || 
                    cls.includes('layout-left') || cls.includes('nav')) {
                    return;
                }
                
                const pCount = container.querySelectorAll('p').length;
                const textLen = (container.innerText || '').length;
                const score = pCount * 100 + textLen;
                if (score > maxScore) {
                    maxScore = score;
                    mainContainer = container;
                }
            });
            
            const clone = mainContainer.cloneNode(true);
            const removeTags = ['script', 'style', 'noscript', 'svg', 'img[src^="data:"]'];
            removeTags.forEach(tag => clone.querySelectorAll(tag).forEach(el => el.remove()));
            
            const walk = document.createTreeWalker(clone, NodeFilter.SHOW_TEXT, null, false);
            let node;
            const blockNodesToRemove = [];
            while(node = walk.nextNode()) {
                const val = node.nodeValue.trim();
                if(['我的留言', '用户留言', '首次发布:', '下一篇', '回顶部', '手机端', '分享', '版权声明', '推荐阅读'].some(k => val.includes(k))) {
                    let p = node.parentElement;
                    while(p && !['DIV', 'SECTION', 'ARTICLE', 'MAIN', 'BODY'].includes(p.tagName)) {
                        p = p.parentElement;
                    }
                    if (p) blockNodesToRemove.push(p);
                }
            }
            
            blockNodesToRemove.forEach(target => {
                if (target && target.tagName !== 'BODY' && clone.contains(target)) {
                    let sibling = target.nextElementSibling;
                    while(sibling) {
                        let next = sibling.nextElementSibling;
                        sibling.remove();
                        sibling = next;
                    }
                    target.remove();
                }
            });
            
            return clone.innerHTML;
        }''')
        
        soup = BeautifulSoup(clean_html, 'html.parser')
        
        # 若页面选择器未能提取到标题，尝试降级提取
        if title == "未知标题":
            first_h = soup.find(['h1', 'h2', 'h3'])
            if first_h:
                title = first_h.text.strip()
            else:
                lines = [line.strip() for line in soup.text.splitlines() if line.strip()]
                if lines:
                    title = lines[0][:50]
                    
        # 转换为 Markdown 并去除超链接标签
        md_content = markdownify.markdownify(
            str(soup),
            heading_style="ATX",
            code_language="",
            strip=['script', 'style', 'a']
        )
        
        md_content = re.sub(r'\n{3,}', '\n\n', md_content).strip()
        
        if not md_content:
            raise ContentExtractionError(f"清洗后未提取到有效正文内容 (标题: {title})")
            
        return {
            "title": title,
            "content": md_content,
            "scrape_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    def run(self):
        """执行完整的抓取流程"""
        self.logger.info("=== 开始执行抓取任务 ===")
        self.logger.info(f"目标 URL: {self.start_url}")
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            
            try:
                page.goto(self.start_url, wait_until="domcontentloaded")
            except PlaywrightTimeoutError:
                raise NetworkError(f"访问目标 URL 超时: {self.start_url}")

            self.logger.info(f"请在弹出的浏览器中确认登录状态，等待 {self.login_timeout/1000} 秒...")
            try:
                page.wait_for_timeout(self.login_timeout)
            except Exception:
                raise LoginTimeoutError("等待登录过程被中断")
                
            self.logger.info("正在解析左侧课程列表并展开目录...")
            try:
                expand_btns = page.locator('text="展开目录"').all()
                for btn in expand_btns:
                    if btn.is_visible():
                        btn.click()
                        time.sleep(1)
            except Exception as e:
                self.logger.warning(f"展开目录时发生异常: {e}")
                
            self._simulate_scroll(page)
            
            course_count = page.evaluate('''() => {
                const markers = Array.from(document.querySelectorAll('*')).filter(el => {
                    return el.innerText && el.innerText.includes('人学过') && el.children.length === 0;
                });
                let count = 0;
                markers.forEach(marker => {
                    let clickable = marker.closest('li') || marker.closest('.class-item') || marker.parentElement;
                    if (clickable) {
                        clickable.setAttribute('data-scraper-target', count);
                        count++;
                    }
                });
                return count;
            }''')
            
            if course_count == 0:
                self.logger.warning("未能识别左侧课程列表，可能是页面结构变更或未成功登录，将仅抓取当前文章。")
                course_count = 1
            else:
                self.logger.info(f"成功识别到 {course_count} 门课程！准备开始遍历...")
                
            with open(self.output_filepath, 'w', encoding='utf-8') as f:
                for i in range(course_count):
                    if course_count > 1:
                        self.logger.info(f"正在打开第 {i+1}/{course_count} 讲...")
                        try:
                            clicked = page.evaluate(f'''() => {{
                                const el = document.querySelector('[data-scraper-target="{i}"]');
                                if (el) {{ el.click(); return true; }}
                                return false;
                            }}''')
                            if not clicked:
                                self.logger.warning(f"节点 {i} 无法点击，跳过")
                                continue
                            self._polite_delay(2.0)
                            self._simulate_scroll(page)
                        except Exception as e:
                            self.logger.error(f"点击课程出错: {e}")
                            continue
                    else:
                        self._simulate_scroll(page)
                        
                    try:
                        extracted_data = self._extract_clean_content(page)
                    except ContentExtractionError as e:
                        self.logger.error(f"提取失败: {e}")
                        continue
                        
                    current_title = extracted_data["title"]
                    if current_title in self.extracted_titles or not current_title:
                        self.logger.warning(f"课程 '{current_title}' 已存在或标题为空，跳过防重")
                        continue
                        
                    self.extracted_titles.add(current_title)
                    self.scraped_data.append(extracted_data)
                    
                    # 写入汇总文档
                    f.write(f"{current_title}\n\n{extracted_data['content']}\n\n—END—\n\n")
                    self.logger.info(f"成功提取: {current_title} (字数: {len(extracted_data['content'])})")
                    
            browser.close()
            self.logger.info("=== 抓取任务完成 ===")
            self.logger.info(f"共提取不重复课程: {len(self.extracted_titles)} 门")
            self.logger.info(f"汇总文档已保存至: {self.output_filepath}")
            
        return self.scraped_data
