"""
抖音平台适配器 (持久 profile + 页面 API 拦截, 2026-09-15)
==========================================================
抖音反爬极强(a_bogus 签名 + TLS 指纹 + 验证码),纯 API 必败;
本适配器走 Playwright 浏览器模拟,页面自己签名发请求,直接拦截响应解析。

【风控实证(2026-09-15 探测)】
  - storage_state 回放(只带 cookie)→ 搜索接口 200 但结果为空(风控软拦)
  - 无登录态 → 验证码中间页
  - 可用路径:launch_persistent_context 固定用户目录(douyin_login_profile.py
    扫码登录) + 综合搜索通道 /aweme/v1/web/general/search/single/
    (type=video 竖切通道 search/item 即使登录态也被软拦,勿用)
  - 评论:视频页 /aweme/v1/web/comment/list/ 响应拦截,滚动触发翻页

【形态创新】首个"评论"形态适配器:每条评论 = 一条独立 UNIFIED item
  (platform="douyin", note_id="dy:c:{cid}", note_type="comment", desc=评论正文,
   title=所属视频标题前60字),视频本身也收一条("dy:v:{aweme_id}")。
  评论直接进 NER+情感管线,与帖子平台同构,聚合/分析侧零改动。
"""

import re
import time
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from urllib.parse import quote

from .base import PlatformAdapter

logger = logging.getLogger("platforms.douyin")

PROFILE_DIR = Path(__file__).parent.parent / "credentials" / "douyin_profile"

# 每个关键词最多深挖评论的视频数(逐视频开页+滚动,是采集耗时大头)
MAX_COMMENT_VIDEOS = 6
# 单视频评论滚动次数(每次滚动触发一页评论 XHR,约 2s;API 拦截下每滚一页多一页评论)
COMMENT_SCROLLS = 15
# 搜索响应等待与最多捕获响应数(综合通道首屏+滚动会发多次)
SEARCH_WAIT_SECONDS = 20
SEARCH_MAX_RESPONSES = 6

_STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    delete navigator.__proto__.webdriver;
    window.navigator.chrome = { runtime: {} };
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
"""


def _ts_to_str(ts) -> str:
    """抖音 unix 秒 → 'YYYY-MM-DD HH:MM:SS'(本地时区);空/异常返回 ''。"""
    try:
        ts = int(ts)
        if ts <= 0:
            return ""
        return (datetime.fromtimestamp(ts, tz=timezone.utc)
                .astimezone().strftime("%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


class DouyinAdapter(PlatformAdapter):
    """抖音数据采集适配器 (持久 profile + 页面 API 拦截)"""

    name = "douyin"
    display_name = "抖音"
    id_prefix = "dy:"

    def __init__(self):
        self._playwright: Any = None
        self._context: Any = None  # launch_persistent_context 返回的即 context
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
        if not PROFILE_DIR.exists():
            raise Exception(
                f"抖音持久 profile 不存在: {PROFILE_DIR}\n"
                "请先运行 python douyin_login_profile.py 扫码登录"
            )
        # 持久 profile:登录态+指纹态一体(douyin_login_profile.py 同目录),不换壳
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,  # headless 会触发验证码,必须有头
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-dev-shm-usage"],
        )
        self._context.add_init_script(_STEALTH_JS)
        names = {c["name"] for c in self._context.cookies("https://www.douyin.com")}
        self._logged_in = bool({"sessionid", "sessionid_ss"} & names)
        if not self._logged_in:
            logger.warning("抖音 profile 无登录态 — 请运行 python douyin_login_profile.py 扫码")
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

        # 开首页暖场(让 msToken 等动态 cookie 先生成)
        try:
            self._page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                            timeout=30000)
            time.sleep(4)
        except Exception as e:
            logger.warning("Douyin homepage warmup: %s", e)

        logger.info("Douyin browser session ready (logged_in=%s)", self._logged_in)

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
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════
    # 搜索 (综合通道 API 拦截;评论在同一趟完成)
    # ═══════════════════════════════════════════════════════════

    @property
    def needs_detail_fetch(self) -> bool:
        return False  # 搜索内部已含视频页评论采集,无需 batch_collect 再深挖

    def get_detail(self, raw_item: Any) -> Optional[dict]:
        return None

    def search(self, keyword: str, count: int) -> list[Any]:
        """综合搜索拿视频 → 前 N 个视频开页拦评论。返回 [_kind 标记的原始 dict] 列表。"""
        videos = self._search_videos(keyword)
        if not videos:
            if self._check_captcha():
                raise Exception("验证码拦截:请运行 python douyin_login_profile.py 后重试")
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
        """综合搜索(默认 tab)→ 拦截 general/search/single 响应 → 视频列表。

        注意:必须用默认综合通道;?type=video 的 search/item 通道登录态下
        也被风控软拦(200 + 空列表,2026-09-15 实证)。
        """
        payloads: list[dict] = []

        def handler(r):
            if ("/aweme/v1/web/general/search/single/" in r.url
                    and len(payloads) < SEARCH_MAX_RESPONSES):
                try:
                    payloads.append(r.json())
                except Exception:
                    pass

        self._page.on("response", handler)
        try:
            self._page.goto(f"https://www.douyin.com/search/{quote(keyword)}",
                            timeout=30000, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning("Douyin search goto: %s", str(e)[:100])
        deadline = time.time() + SEARCH_WAIT_SECONDS
        while time.time() < deadline and not payloads:
            time.sleep(0.5)
        # 滚动触发下一批响应(更多结果)
        for _ in range(2):
            self._page.evaluate("window.scrollBy(0, 1200)")
            time.sleep(1.5)
        self._page.remove_listener("response", handler)

        if self._check_captcha():
            raise Exception("验证码拦截:请运行 python douyin_login_profile.py 后重试")

        seen: set[str] = set()
        out: list[dict] = []
        for d in payloads:
            for item in d.get("data") or []:
                aw = item.get("aweme_info") or (item if item.get("aweme_id") else None)
                if not aw or not aw.get("aweme_id") or aw["aweme_id"] in seen:
                    continue
                seen.add(aw["aweme_id"])
                st = aw.get("statistics") or {}
                out.append({
                    "_kind": "video",
                    "id": aw["aweme_id"],  # batch_collect 去重/主键读取此字段
                    "aweme_id": aw["aweme_id"],
                    "href": f"https://www.douyin.com/video/{aw['aweme_id']}",
                    "desc": (aw.get("desc") or "").strip(),
                    "author_name": ((aw.get("author") or {}).get("nickname") or "")[:50],
                    "like_count": int(st.get("digg_count") or 0),
                    "comment_count_hint": int(st.get("comment_count") or 0),
                    "create_time": _ts_to_str(aw.get("create_time")),
                })
        logger.info("  Douyin: %d videos for '%s'", len(out), keyword)
        return out

    def _scrape_comments(self, video: dict, keyword: str) -> list[dict]:
        """视频页 → 拦截 comment/list 响应(滚动触发翻页)→ 评论列表。"""
        comments: list[dict] = []
        seen: set[str] = set()

        def handler(r):
            if "/aweme/v1/web/comment/list/" not in r.url:
                return
            try:
                d = r.json()
                for c in d.get("comments") or []:
                    cid = c.get("cid")
                    text = (c.get("text") or "").strip()
                    if not cid or not text or cid in seen:
                        continue
                    seen.add(cid)
                    comments.append({
                        "_kind": "comment",
                        "id": f"{video['aweme_id']}:{cid}",  # batch_collect 主键
                        "_cid": str(cid),
                        "text": text[:800],
                        "author_name": ((c.get("user") or {}).get("nickname") or "")[:50],
                        "like_count": int(c.get("digg_count") or 0),
                        "publish_time": _ts_to_str(c.get("create_time")),
                        "ip_location": (c.get("ip_label") or "")[:30],
                        "video": video,
                    })
            except Exception:
                pass

        self._page.on("response", handler)
        try:
            self._page.goto(video["href"], timeout=30000, wait_until="domcontentloaded")
            time.sleep(3.5)
            if self._check_captcha():
                raise Exception("视频页验证码拦截")
            # 滚动评论区触发翻页加载
            for _ in range(COMMENT_SCROLLS):
                self._page.evaluate("""
                    const el = document.querySelector('[data-e2e="comment-list"]')
                        || document.scrollingElement;
                    if (el) el.scrollBy(0, 1500); else window.scrollBy(0, 1500);
                """)
                time.sleep(2)
        finally:
            self._page.remove_listener("response", handler)
        logger.info("  Douyin: %d comments on %s", len(comments), video["aweme_id"])
        return comments

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
            "like_count": int(v.get("like_count") or 0),
            "comment_count": int(v.get("comment_count_hint") or 0),
            "collect_count": 0,
            "share_count": 0,
            "tags": [],
            "note_type": "video",
            "publish_time": v.get("create_time", ""),
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
        cid = c.get("_cid") or str(abs(hash(text[:50])) % 10**12)  # 无 cid 时内容哈希兜底
        return {
            "platform": "douyin",
            "note_id": f"{self.id_prefix}c:{vid}:{cid}",
            "title": f"[抖音评论] {video.get('desc', '')[:50]}",
            "desc": text,
            "author_name": c.get("author_name", ""),
            "author_id": "",
            "author_fans": 0,
            "like_count": int(c.get("like_count") or 0),
            "comment_count": 0,
            "collect_count": 0,
            "share_count": 0,
            "tags": [],
            "note_type": "comment",
            "publish_time": c.get("publish_time", ""),
            "ip_location": c.get("ip_location", ""),
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
