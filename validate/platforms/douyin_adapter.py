"""
抖音平台适配器 (Playwright + 评论区采集)
========================================
抖音反爬极强(a_bogus 签名 + TLS 指纹 + 验证码),纯 API 必败;
本适配器走 Playwright 浏览器模拟,是唯一现实路径(见 validator_douyin.py 结论)。

【形态创新】首个"评论"形态适配器:每条评论 = 一条独立 UNIFIED item
  (platform="douyin", note_id="dy:c:{cid}", note_type="comment", desc=评论正文,
   title=所属视频标题前60字),视频本身也收一条("dy:v:{aweme_id}")。
  评论直接进 NER+情感管线,与帖子平台同构,聚合/分析侧零改动。

技术路线:
  - init: 启动 Chromium(反检测),加载 credentials/douyin_login_state.json
    (douyin_login.py 扫码产出;无登录态时尝试 JS 移除全屏登录墙)
  - search: 搜索页 DOM 提取视频卡片 → 逐视频打开视频页滚动评论区提取评论
  - normalize: 视频/评论两种 item 按 _kind 分发

已验证的拦截形态(2026-09-07 免登录探测):
  - 搜索深链 headless/有头均落"验证码中间页"(标题含"验证码")
  - 首页全屏登录墙(id^="login-full-panel")挡交互 → 登录态是硬前提
"""

import re
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import quote

from .base import PlatformAdapter

logger = logging.getLogger("platforms.douyin")

CREDENTIALS_DIR = Path(__file__).parent.parent / "credentials"
STATE_FILE = CREDENTIALS_DIR / "douyin_login_state.json"

# 每个关键词最多深挖评论的视频数(逐视频开页+滚动,是采集耗时大头)
MAX_COMMENT_VIDEOS = 3
# 单视频评论滚动次数(每次 ~1.5s,滚动 8 次约拿 20-40 条)
COMMENT_SCROLLS = 8

_STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    delete navigator.__proto__.webdriver;
    window.navigator.chrome = { runtime: {} };
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
"""


def _parse_cn_count(text: str) -> int:
    """'1.2万' → 12000,'3' → 3,'886' → 886;解析失败 0。"""
    t = (text or "").strip().replace("+", "")
    m = re.search(r"([\d.]+)\s*([万亿])?", t)
    if not m:
        return 0
    try:
        num = float(m.group(1))
    except ValueError:
        return 0
    unit = m.group(2)
    if unit == "万":
        num *= 10_000
    elif unit == "亿":
        num *= 100_000_000
    return int(num)


def _parse_cn_time(text: str, now: datetime | None = None) -> str:
    """抖音相对时间('3天前'/'2小时前'/'昨天 12:30'/'09-01')→ 'YYYY-MM-DD HH:MM:SS'。"""
    t = (text or "").strip()
    if not t:
        return ""
    now = now or datetime.now()

    # 绝对日期形态优先
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%m-%d %H:%M", "%m-%d"):
        try:
            dt = datetime.strptime(t, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=now.year)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

    def _back(**kw) -> str:
        return (now - timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")

    m = re.match(r"(\d+)\s*秒前", t)
    if m:
        return _back(seconds=int(m.group(1)))
    m = re.match(r"(\d+)\s*分钟前", t)
    if m:
        return _back(minutes=int(m.group(1)))
    m = re.match(r"(\d+)\s*小时前", t)
    if m:
        return _back(hours=int(m.group(1)))
    if t.startswith("昨天"):
        return _back(days=1)
    m = re.match(r"(\d+)\s*天前", t)
    if m:
        return _back(days=int(m.group(1)))
    m = re.match(r"(\d+)\s*周前", t)
    if m:
        return _back(weeks=int(m.group(1)))
    m = re.match(r"(\d+)\s*个?月前", t)
    if m:
        return _back(days=int(m.group(1)) * 30)
    m = re.match(r"(\d+)\s*年前", t)
    if m:
        return _back(days=int(m.group(1)) * 365)
    return ""  # 无法解析 → 空(batch_collect 的 --since 过滤会跳过空时间行)


class DouyinAdapter(PlatformAdapter):
    """抖音数据采集适配器 (Playwright 浏览器模拟 + 评论区采集)"""

    name = "douyin"
    display_name = "抖音"
    id_prefix = "dy:"

    def __init__(self):
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._logged_in = False

    # ═══════════════════════════════════════════════════════════
    # 生命周期
    # ═══════════════════════════════════════════════════════════

    def init(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise Exception(
                "Playwright not installed. Run: pip install playwright && playwright install chromium"
            )

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-dev-shm-usage"],
        )
        ctx_kw = {
            "viewport": {"width": 1440, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
        }
        if STATE_FILE.exists():  # 登录态(douyin_login.py 扫码产出)直接整包加载
            ctx_kw["storage_state"] = str(STATE_FILE)
            self._logged_in = True
            logger.info("Loaded douyin login state from %s", STATE_FILE.name)
        else:
            logger.warning("No douyin login state — 匿名访问,大概率被验证码/登录墙拦截")
        self._context = self._browser.new_context(**ctx_kw)
        self._context.add_init_script(_STEALTH_JS)
        self._page = self._context.new_page()

        # 开首页暖场;匿名时全屏登录墙用 JS 移除(拿不到评论但至少能进搜索)
        try:
            self._page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)
            self._dismiss_login_wall()
        except Exception as e:
            logger.warning("Douyin homepage warmup: %s", e)

        logger.info("Douyin browser session ready (logged_in=%s)", self._logged_in)

    def _dismiss_login_wall(self) -> None:
        """移除全屏登录墙:先找关闭按钮,找不到就直接 JS 删面板节点。"""
        for sel in ('[data-e2e="login-close"]', '[class*="login-close"]', '[class*="close"] [class*="icon"]'):
            try:
                btn = self._page.query_selector(sel)
                if btn:
                    btn.click()
                    time.sleep(1)
                    return
            except Exception:
                continue
        try:
            self._page.evaluate(
                "document.querySelectorAll('[id^=\"login-full-panel\"], [class*=\"login-panel\"]')"
                ".forEach(e => e.remove());"
            )
        except Exception:
            pass

    def _check_captcha(self) -> bool:
        """当前页是否验证码中间页(标题最可靠,body 可能是 JS 空壳)。"""
        try:
            title = self._page.title() or ""
            return "验证码" in title
        except Exception:
            return False

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════
    # 搜索 (DOM 提取;评论在同一趟完成)
    # ═══════════════════════════════════════════════════════════

    @property
    def needs_detail_fetch(self) -> bool:
        return False  # 搜索内部已含视频页评论采集,无需 batch_collect 再深挖

    def get_detail(self, raw_item: Any) -> Optional[dict]:
        return None

    def search(self, keyword: str, count: int) -> list[Any]:
        """搜索页提视频 → 前 N 个视频开页采评论。返回 [_kind 标记的原始 dict] 列表。"""
        videos = self._search_videos(keyword)
        if not videos:
            if self._check_captcha():
                raise Exception("验证码拦截:请运行 python douyin_login.py 扫码后重试")
            return []

        items: list[dict] = []
        for v in videos[:MAX_COMMENT_VIDEOS]:
            try:
                comments = self._scrape_comments(v, keyword)
                time.sleep(1.5)
            except Exception as e:
                logger.warning("Comments for %s failed: %s", v["aweme_id"], str(e)[:100])
                comments = []
            # 评论优先放前(评论是本适配器的核心价值),视频兜底
            items = comments + [v] + items

        return items[:count]

    def _search_videos(self, keyword: str) -> list[dict]:
        """搜索页 → 视频卡片 [{aweme_id, desc, href, author, diggs}]。"""
        url = f"https://www.douyin.com/search/{quote(keyword)}?type=video"
        self._page.goto(url, timeout=30000, wait_until="domcontentloaded")
        time.sleep(4)
        if self._check_captcha():
            raise Exception("验证码拦截:请运行 python douyin_login.py 扫码后重试")
        # 懒加载滚动
        for _ in range(3):
            self._page.evaluate("window.scrollBy(0, 900)")
            time.sleep(1.2)

        cards = self._page.query_selector_all(
            '[data-e2e="scroll-list-item-item"], [data-e2e="search-list-item"]'
        )
        seen: set[str] = set()
        out: list[dict] = []
        for c in cards:
            try:
                a = c.query_selector('a[href*="/video/"], a[href*="/note/"]')
                if not a:
                    continue
                href = a.get_attribute("href") or ""
                m = re.search(r"/(?:video|note)/(\d+)", href)
                if not m or m.group(1) in seen:
                    continue
                seen.add(m.group(1))
                author_el = c.query_selector('[class*="author"], [data-e2e="video-author"]')
                desc = (c.inner_text() or "").strip()[:200]
                out.append({
                    "_kind": "video",
                    "aweme_id": m.group(1),
                    "href": href if href.startswith("http") else f"https://www.douyin.com{href}",
                    "desc": desc,
                    "author_name": (author_el.inner_text() or "").strip()[:50] if author_el else "",
                })
            except Exception:
                continue
        logger.info("  Douyin: %d videos for '%s'", len(out), keyword)
        return out

    def _scrape_comments(self, video: dict, keyword: str) -> list[dict]:
        """视频页评论区 → 评论 raw dict 列表。"""
        self._page.goto(video["href"], timeout=30000, wait_until="domcontentloaded")
        time.sleep(3.5)
        if self._check_captcha():
            raise Exception("视频页验证码拦截")

        # 等评论面板;无面板(未登录/评论关闭)就放弃该视频
        panel_ok = False
        for sel in ('[data-e2e="comment-list"]', '[class*="commentList"]'):
            try:
                self._page.wait_for_selector(sel, timeout=8000)
                panel_ok = True
                break
            except Exception:
                continue
        if not panel_ok:
            logger.info("  Douyin: no comment panel on %s (需登录或评论关闭)", video["aweme_id"])
            return []

        # 滚动评论区加载更多
        for _ in range(COMMENT_SCROLLS):
            self._page.evaluate("""
                const el = document.querySelector('[data-e2e="comment-list"]')
                    || document.querySelector('[class*="commentList"]') || document.scrollingElement;
                if (el) el.scrollBy(0, 1200); else window.scrollBy(0, 1200);
            """)
            time.sleep(1.5)

        # 逐条提取评论
        nodes = self._page.query_selector_all('[data-e2e="comment-item"]')
        out: list[dict] = []
        seen: set[str] = set()
        for n in nodes:
            try:
                text_el = n.query_selector('[data-e2e="comment-item-content"], p, span[class*="content"]')
                text = (text_el.inner_text() if text_el else n.inner_text() or "").strip()
                # 去掉条目内混入的"回复/赞/时间"等短噪音,正文太短或重复丢弃
                text = re.sub(r"(回复|展开|收起)$", "", text).strip()
                if len(text) < 4 or text[:30] in seen:
                    continue
                seen.add(text[:30])
                author_el = n.query_selector('a[data-e2e="comment-user"], [class*="author"] a, [class*="nick"]')
                like_el = n.query_selector('[data-e2e="comment-digg-count"], [class*="digg"]')
                time_el = n.query_selector('[class*="time"], [data-e2e="comment-time"]')
                cid = ""  # DOM 无稳定 cid,用内容哈希兜底(normalize 里合成)
                out.append({
                    "_kind": "comment",
                    "_cid": cid,
                    "text": text[:800],
                    "author_name": (author_el.inner_text() or "").strip()[:50] if author_el else "",
                    "like_text": (like_el.inner_text() or "").strip() if like_el else "",
                    "time_text": (time_el.inner_text() or "").strip() if time_el else "",
                    "video": video,
                })
            except Exception:
                continue
        logger.info("  Douyin: %d comments on %s", len(out), video["aweme_id"])
        return out

    # ═══════════════════════════════════════════════════════════
    # 归一化 (视频/评论双形态分发)
    # ═══════════════════════════════════════════════════════════

    def normalize(self, raw_item: Any, detail: Optional[dict], keyword: str) -> Optional[dict]:
        if not isinstance(raw_item, dict):
            return None
        if raw_item.get("_kind") == "comment":
            return self._normalize_comment(raw_item, keyword)
        return self._normalize_video(raw_item, keyword)

    def _normalize_video(self, v: dict, keyword: str) -> Optional[dict]:
        desc = (v.get("desc") or "").strip()
        if not desc:
            return None
        return {
            "platform": "douyin",
            "note_id": f"{self.id_prefix}v:{v['aweme_id']}",
            "title": desc[:60],
            "desc": desc[:2000],
            "author_name": v.get("author_name", ""),
            "author_id": "",
            "author_fans": 0,
            "like_count": 0,
            "comment_count": 0,
            "collect_count": 0,
            "share_count": 0,
            "tags": [],
            "note_type": "video",
            "publish_time": "",  # 搜索卡片无时间;评论才有相对时间
            "ip_location": "",
            "keyword": keyword,
            "url": v.get("href", ""),
            "desc_length": len(desc),
            "image_count": 0,
            "is_video": True,
            "image_urls": [],
        }

    def _normalize_comment(self, c: dict, keyword: str) -> Optional[dict]:
        text = (c.get("text") or "").strip()
        if len(text) < 4:
            return None
        video = c.get("video", {}) or {}
        vid = video.get("aweme_id", "")
        cid = c.get("_cid") or str(abs(hash(text[:50])) % 10**12)  # 无稳定 cid → 内容哈希(同文本去重)
        return {
            "platform": "douyin",
            "note_id": f"{self.id_prefix}c:{vid}:{cid}",
            "title": f"[抖音评论] {video.get('desc', '')[:50]}",
            "desc": text,
            "author_name": c.get("author_name", ""),
            "author_id": "",
            "author_fans": 0,
            "like_count": _parse_cn_count(c.get("like_text", "")),
            "comment_count": 0,
            "collect_count": 0,
            "share_count": 0,
            "tags": [],
            "note_type": "comment",
            "publish_time": _parse_cn_time(c.get("time_text", "")),
            "ip_location": "",
            "keyword": keyword,
            "url": video.get("href", ""),
            "desc_length": len(text),
            "image_count": 0,
            "is_video": False,
            "image_urls": [],
        }

    def classify_error(self, exc: Exception) -> str:
        msg = str(exc).lower()
        if "timeout" in msg:
            return "rate_limit"
        if "验证码" in str(exc):
            return "auth"
        return "other"

    @staticmethod
    def field_mapping() -> dict:
        from .base import FIELD_MAPPING_TABLE

        return FIELD_MAPPING_TABLE.get("douyin", {})
