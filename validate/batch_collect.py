"""
期货社交媒体 — 多平台批量采集
==============================
平台适配器模式: 通过 --platform 切换小红书/微博/知乎。
  - 自适应延时 (正常请求后自适应缩短, 检测到限流后自动加长)
  - 增量保存 (每个关键词完成后立即写盘, 中断不丢数据)
  - 失败重试 + 退避
  - 进度条 + ETA
  - NER + 情感分析 enrich

使用:
  python batch_collect.py                                      # 默认 xhs, 30条/关键词
  python batch_collect.py --platform weibo                     # 微博
  python batch_collect.py --per-kw 50 --max-detail 20          # 每词50条, 深挖20条
  python batch_collect.py --keywords "螺纹" "铁矿"              # 自定义关键词
  python batch_collect.py --safe-mode                          # 安全模式(更慢更安全)
  python batch_collect.py --turbo                              # 极速模式
  python batch_collect.py --no-detail                          # 不深挖(微博推荐,速度快)
"""

import json, time, random, logging, argparse, sys, os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field, asdict

# 平台适配器
from platforms import get_adapter, list_platforms
from platforms.base import PlatformAdapter, CredentialError

# NER + 情感 (纯文本, 平台无关)
from ner import FuturesNER
from sentiment import SentimentAnalyzer

logger = logging.getLogger("batch.collect")

# 2026-08-26 子进程在 GBK 控制台打印 emoji(⚠️) 会 UnicodeEncodeError 崩溃,掩盖真实错误。
# 采集子进程(cwd=THINK2_DIR)由 scheduler/web_app 以 subprocess 拉起,stdout 为管道,
# reconfigure(errors="replace") 把不可编码字符替换为 "?",保证日志可读、退出码真实。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(errors="replace")


# ============================================================
# 配置
# ============================================================

# 采集控制
DEFAULT_PER_KW = 30          # 每关键词采集条数
MIN_DELAY_MS = 300           # 最小请求间隔(ms) — 实测API耗时~750ms, 300ms间隔安全
MAX_DELAY_MS = 1000          # 最大请求间隔(ms)
SAFE_MODE_MULTIPLIER = 2.5   # 安全模式延时倍率 (300*2.5=750ms 起步)
TURBO_MIN_DELAY_MS = 120     # 极速模式最小间隔
TURBO_MAX_DELAY_MS = 500     # 极速模式最大间隔
RATE_LIMIT_COOLDOWN = 30     # 触发限流后冷却秒数
BATCH_COOLDOWN = 1           # 每批关键词间休息秒数

# 【护栏 2026-09-10】全局时间预算(分钟): 触发后在下一个关键词边界优雅收尾。
# 背景: 东财股吧被 WAF 限流时, 每个关键词最坏 ~52s(搜索超时×2次重试+退避)
# + 30s(RATE_LIMIT_COOLDOWN), 30 关键词可烧 ~40min —— 调度器 15min 子进程
# 超时强杀, 表现为"挂起"。预算默认 12min, 给调度器 900s 超时留 3min 余量
# (增量写盘逐关键词落盘, 预算触发不丢已采数据)。
MAX_RUN_MINUTES = 12
# 【护栏 2026-09-10】连续空结果熔断: 连续 N 个关键词 0 命中 → 判定平台级
# 故障(限流/登录态失效), 提前收尾。正常行情下不同品种关键词连续 5 个全空
# 只可能是系统性问题, 继续翻关键词纯烧时间。
MAX_CONSECUTIVE_EMPTY = 5
MAX_DETAIL_PER_KW = 10       # 每关键词最多深挖条数

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 平台默认关键词 (xhs 保留旧列表, 微博/知乎待 config.py 扩展)
# 【品种池】2026-09-09 起全项目只保留 20 个活跃品种(ACTIVE_VARIETIES),关键词
# 列表收缩为"池内品种词 + 通用主题词";池外品种(螺纹钢/黄金/股指等)不再采集。
DEFAULT_KEYWORDS_XHS = [
    # 能化 13
    "原油期货分析", "PTA期货", "对二甲苯期货", "乙二醇期货",
    "燃料油期货", "低硫燃料油期货", "LPG期货",
    "橡胶期货", "20号胶期货", "丁二烯橡胶期货", "沥青期货", "纯苯期货", "苯乙烯期货",
    # 农产品 4
    "豆粕期货", "棉花期货", "红枣期货", "生猪期货",
    # 有色(新能源) 3
    "碳酸锂期货", "多晶硅期货", "工业硅期货",
    # 通用
    "期货实盘", "期货技术分析", "期货基本面",
    "期货日内交易", "期货波段策略",
]

DEFAULT_KEYWORDS_WEIBO = [
    "原油", "PTA", "对二甲苯", "乙二醇",
    "燃料油", "低硫燃料油", "LPG", "液化石油气",
    "橡胶", "20号胶", "丁二烯橡胶", "沥青", "纯苯", "苯乙烯",
    "豆粕", "棉花", "红枣", "生猪",
    "碳酸锂", "多晶硅", "工业硅",
    # 通用
    "期货实盘", "期货技术分析", "期货基本面",
]

DEFAULT_KEYWORDS_ZHIHU = [
    "期货", "商品期货",
    "原油", "沥青", "橡胶", "20号胶", "燃料油", "低硫燃料油",
    "苯乙烯", "乙二醇", "对二甲苯",
    "豆粕", "棉花", "红枣", "生猪",
    "碳酸锂", "多晶硅", "工业硅",
    "期货交易策略", "期货技术分析",
]

DEFAULT_KEYWORDS_XUEQIU = [
    "原油", "PTA", "对二甲苯", "乙二醇",
    "燃料油", "低硫燃料油", "LPG",
    "橡胶", "20号胶", "丁二烯橡胶", "沥青", "纯苯", "苯乙烯",
    "豆粕", "棉花", "红枣", "生猪",
    "碳酸锂", "多晶硅", "工业硅",
    # 通用
    "期货实盘", "期货技术分析", "期货基本面",
]
DEFAULT_KEYWORDS_EASTMONEY_GUBA = [  # 东财股吧平台默认关键词列表(带"期货"后缀,20 品种池+池内品种长尾)
    # 能化 13
    "原油期货", "PTA期货", "对二甲苯期货", "乙二醇期货",
    "燃料油期货", "低硫燃料油期货", "LPG期货",
    "橡胶期货", "20号胶期货", "丁二烯橡胶期货", "沥青期货", "纯苯期货", "苯乙烯期货",
    # 农产品 4
    "豆粕期货", "棉花期货", "红枣期货", "生猪期货",
    # 有色(新能源) 3
    "碳酸锂期货", "多晶硅期货", "工业硅期货",
    # 池内品种长尾扩充(股吧帖子长尾词;不用"红枣"裸词避免撞水果)
    "生猪价格", "能繁母猪", "红枣库存", "碳酸锂库存", "硅料价格",
    # 通用
    "期货实战", "期货交易心得", "期货技术分析", "商品期货",
]

# 抖音(2026-09-07 试点接入): 浏览器模拟逐视频采评论,速度慢,关键词精简为活跃品种核心词
# 【品种池】2026-09-09 收缩:试点词换成池内品种(甲醇/纯碱已出池→沥青/苯乙烯)
DEFAULT_KEYWORDS_DOUYIN = [
    "沥青期货", "苯乙烯期货", "碳酸锂期货",   # 试点 3 品种(跑通后扩)
    "期货交易", "期货实盘",                 # 通用兜底
]

DEFAULT_KEYWORDS = {
    "xhs": DEFAULT_KEYWORDS_XHS,
    "weibo": DEFAULT_KEYWORDS_WEIBO,
    "zhihu": DEFAULT_KEYWORDS_ZHIHU,
    "xueqiu": DEFAULT_KEYWORDS_XUEQIU,
    "eastmoney_guba": DEFAULT_KEYWORDS_EASTMONEY_GUBA,
    "douyin": DEFAULT_KEYWORDS_DOUYIN,
}

# 凭证失效时的重登录指引 (2026-09-01): 采集失败诊断块按平台输出下一步操作。
AUTH_GUIDANCE = {  # 【变量】平台名→重登录指引 (无对应登录脚本的平台给手动指引)
    "_default": "查看该平台登录方式并重新登录后再采集",
    "zhihu": "运行 python zhihu_login.py 重新登录(扫码/验证码)",
    "weibo": "运行 python weibo_login.py 重新登录更新 Cookie",
    "xueqiu": "更新 credentials/xueqiu_cookie.txt(需手动从浏览器复制新 Cookie)",
    "eastmoney_guba": "重新登录东方财富股吧并更新会话凭证",
    "xhs": "检查小红书登录状态并重新登录",
    "douyin": "运行 python douyin_login_profile.py 扫码重新登录(持久 profile: credentials/douyin_profile)",
}


# ============================================================
# 数据结构
# ============================================================

@dataclass
class CollectStats:
    """采集统计"""
    started: str = ""
    keywords_total: int = 0
    keywords_done: int = 0
    searched_total: int = 0
    detail_fetched: int = 0
    detail_failed: int = 0
    rate_limits_hit: int = 0
    auth_failures: int = 0  # 【变量】凭证类失败次数(登录态失效/过期), 用于结尾诊断提示
    errors: list = field(default_factory=list)


class RateLimiter:
    """自适应延时控制器 (平台无关)"""

    def __init__(self, safe_mode: bool = False, turbo_mode: bool = False):
        if turbo_mode:
            self.min_delay = TURBO_MIN_DELAY_MS / 1000
            self.max_delay = TURBO_MAX_DELAY_MS / 1000
            self.jitter_pct = 0.10
        elif safe_mode:
            self.min_delay = (MIN_DELAY_MS * SAFE_MODE_MULTIPLIER) / 1000
            self.max_delay = (MAX_DELAY_MS * SAFE_MODE_MULTIPLIER) / 1000
            self.jitter_pct = 0.25
        else:
            self.min_delay = MIN_DELAY_MS / 1000
            self.max_delay = MAX_DELAY_MS / 1000
            self.jitter_pct = 0.15
        self.current_delay = self.min_delay
        self.consecutive_failures = 0
        self.last_request_time = 0
        self.safe_mode = safe_mode
        self.turbo_mode = turbo_mode

    def wait(self):
        """等待适当的时间"""
        elapsed = time.time() - self.last_request_time
        wait_time = max(0, self.current_delay - elapsed)
        if wait_time > 0:
            jitter = random.uniform(-self.jitter_pct, self.jitter_pct) * wait_time
            time.sleep(wait_time + jitter)
        self.last_request_time = time.time()

    def report_success(self):
        """请求成功 → 逐渐恢复最小延时"""
        self.consecutive_failures = 0
        self.current_delay = max(self.min_delay, self.current_delay * 0.9)

    def report_failure(self, is_rate_limit: bool = False):
        """请求失败 → 增加延时"""
        self.consecutive_failures += 1
        if is_rate_limit:
            self.current_delay *= 2.0
            logger.warning(f"Rate limit detected! Delay increased to {self.current_delay:.1f}s")
            time.sleep(RATE_LIMIT_COOLDOWN)
        else:
            backoff = min(2.0 ** self.consecutive_failures, 5.0)
            self.current_delay = min(self.max_delay, self.current_delay * backoff)

    def cooldown(self, seconds: float = 10):
        """批次间冷却"""
        logger.info(f"Batch cooldown: {seconds}s...")
        time.sleep(seconds)


class MultiPlatformCollector:
    """多平台批量采集器 — 通过 adapter 注入解耦平台差异"""

    def __init__(
        self,
        platform: str = "xhs",
        safe_mode: bool = False,
        turbo_mode: bool = False,
    ):
        self.platform_name = platform
        self.adapter: PlatformAdapter = get_adapter(platform)
        self.limiter = RateLimiter(safe_mode=safe_mode, turbo_mode=turbo_mode)
        self.ner = FuturesNER()
        self.sentiment = SentimentAnalyzer()
        self.stats = CollectStats(started=datetime.now().isoformat())
        self.output_file: Optional[Path] = None
        self.seen_ids: set = set()  # (platform, note_id) 跨关键词去重

    def init_api(self):
        """初始化平台适配器"""
        try:
            self.adapter.init()
        except CredentialError as e:
            print(f"\n{'='*60}")
            print(f"  ⚠️  {self.adapter.display_name} 登录凭证缺失或已过期！")
            print(f"{'='*60}")
            print(f"\n  {e}\n")
            sys.exit(1)

        logger.info(f"{self.adapter.display_name} adapter initialized.")

    def collect_one_keyword(
        self, keyword: str, count: int, max_detail: int
    ) -> list[dict]:
        """
        采集一个关键词 (平台无关)。
        流程: search → [get_detail for each] → normalize → filter seen
        """
        notes = []
        logger.info(f"Searching: '{keyword}' (target {count})")

        # Step 1: 搜索
        self.limiter.wait()
        try:
            items = self.adapter.search(keyword, count)
        except Exception as e:
            logger.error(f"Search failed for '{keyword}': {e}")
            _cat = self.adapter.classify_error(e)
            self.limiter.report_failure(is_rate_limit=(_cat == "rate_limit"))
            if _cat == "auth":  # 【变量】凭证类失败 → 计数, 结尾诊断提示重新登录
                self.stats.auth_failures += 1
            return []

        if not items:
            logger.warning(f"Search '{keyword}' returned no results")
            self.limiter.report_failure()
            return []

        self.limiter.report_success()
        logger.info(f"  Found {len(items)} items for '{keyword}'")

        # Step 2: 逐条获取详情 + 归一化
        detail_count = 0
        detail_limit = max_detail if self.adapter.needs_detail_fetch else len(items)

        for item_idx, item in enumerate(items):
            if detail_count >= detail_limit:
                break

            nid = item.get("id", "") or item.get("mid", "")
            if not nid:
                continue

            # 详情获取
            detail = None
            fetch_ok = True
            if self.adapter.needs_detail_fetch:
                self.limiter.wait()
                try:
                    detail = self.adapter.get_detail(item)
                except Exception as e:
                    logger.warning(f"  Detail fetch failed for {str(nid)[:12]}...: {e}")
                    self.limiter.report_failure(
                        is_rate_limit=(self.adapter.classify_error(e) == "rate_limit")
                    )
                    self.stats.detail_failed += 1
                    continue

                if detail is None:
                    self.stats.detail_failed += 1
                    self.limiter.report_failure()
                    continue

                self.limiter.report_success()
                self.stats.detail_fetched += 1
                fetch_ok = True
            else:
                # 无需深挖 (如微博), 所有 item 都算成功
                fetch_ok = True

            if not fetch_ok:
                continue

            # 归一化为统一 Schema
            try:
                note_dict = self.adapter.normalize(item, detail, keyword)
            except Exception as e:
                logger.warning(f"  Normalize failed: {e}")
                continue

            if note_dict is None:
                continue

            # 去重
            pid = note_dict.get("platform", self.platform_name)
            note_id = note_dict.get("note_id", "")
            dedup_key = (pid, note_id)
            if dedup_key in self.seen_ids:
                logger.debug(f"  Duplicate skipped: {note_id[:20]}")
                continue
            self.seen_ids.add(dedup_key)

            detail_count += 1
            notes.append(note_dict)

            # 日志
            desc_len = len(note_dict.get("desc", "") or "")
            title_preview = (note_dict.get("title") or note_dict.get("desc") or "")[:40].replace('\n', ' ')
            try:
                print(f"  [{detail_count}/{detail_limit}] {str(note_id)[:12]}... "
                      f"L{note_dict.get('like_count',0)} C{note_dict.get('comment_count',0)} "
                      f"| {desc_len}c | {title_preview}")
            except UnicodeEncodeError:
                safe_title = title_preview.encode('ascii', errors='replace').decode('ascii')
                print(f"  [{detail_count}/{detail_limit}] {str(note_id)[:12]}... "
                      f"L{note_dict.get('like_count',0)} C{note_dict.get('comment_count',0)} "
                      f"| {desc_len}c | {safe_title}")

        self.stats.searched_total += len(items)
        return notes

    def _enrich_notes(self, notes: list[dict]) -> list[dict]:
        """NER + 情感分析 enrich (平台无关, 只吃文本)"""
        for note in notes:
            text = (note.get("title", "") + " " + note.get("desc", "")).strip()
            if not text:
                # 无文本 → 填默认值
                note.setdefault("varieties", [])
                note.setdefault("contracts", [])
                note.setdefault("variety_count", 0)
                note.setdefault("sentiment", "neutral")
                note.setdefault("sentiment_score", 0.0)
                note.setdefault("sentiment_confidence", 0.0)
                note.setdefault("variety_sentiments", [])
                continue

            # NER
            entities = self.ner.extract(text)
            note["varieties"] = entities["varieties"]
            note["contracts"] = entities["contracts"]
            note["variety_count"] = entities["variety_count"]

            # 整篇情感
            r = self.sentiment.analyze(text)
            note["sentiment"] = r["sentiment"]
            note["sentiment_score"] = r["score"]
            note["sentiment_confidence"] = r["confidence"]

            # 品种级情感
            var_sent = self.sentiment.analyze_aspects(text, entities["varieties"])
            note["variety_sentiments"] = var_sent

        return notes

    def run(
        self, keywords: list[str], per_kw: int = 30, max_detail: int = 10,
        no_enrich: bool = False, since: str | None = None,
        max_minutes: float = MAX_RUN_MINUTES,
    ) -> list[dict]:
        """主采集循环 (平台无关)。

        Args:
            since: 可选日期过滤 (YYYY-MM-DD)，只保留此日期及之后的帖子。
            max_minutes: 全局时间预算(分钟), 超时后在关键词边界优雅收尾。
        """
        self.init_api()
        self.stats.keywords_total = len(keywords)

        # 输出文件: batch_{platform}_{ts}.jsonl
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_file = OUTPUT_DIR / f"batch_{self.platform_name}_{ts}.jsonl"

        # Since date filter
        since_date = None
        if since:
            try:
                since_date = datetime.strptime(since, "%Y-%m-%d")
                print(f"  Time filter: only keeping posts since {since}")
            except ValueError:
                print(f"  WARNING: Invalid --since date '{since}', ignoring filter")
                since_date = None

        total_notes = 0
        detail_text = "no detail" if not self.adapter.needs_detail_fetch else f"detail {max_detail}"
        mode_str = "TURBO" if self.limiter.turbo_mode else "SAFE" if self.limiter.safe_mode else "FAST"

        print(f"\n{'='*60}")
        print(f"BATCH COLLECTION — {self.adapter.display_name}")
        print(f"  Keywords: {len(keywords)}")
        print(f"  Per keyword: search {per_kw}, {detail_text}")
        print(f"  Mode: {mode_str}")
        print(f"  Output: {self.output_file}")
        print(f"{'='*60}\n")

        start_time = time.time()
        deadline = start_time + max_minutes * 60  # 【变量】全局时间预算硬截止
        consecutive_empty = 0  # 【变量】连续 0 命中关键词计数(熔断用)

        for kw_idx, kw in enumerate(keywords):
            # 护栏 1: 时间预算 —— 超时在关键词边界优雅收尾(数据已逐关键词落盘)
            if time.time() > deadline:
                print(f"\n  [budget] Time budget {max_minutes:.0f}min reached, "
                      f"stopping early at keyword {kw_idx+1}/{len(keywords)}")
                break

            # 护栏 2: 连续空结果熔断 —— 平台级故障(限流/登录态失效)提前放弃
            if consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
                print(f"\n  [circuit-breaker] {consecutive_empty} consecutive keywords "
                      f"returned 0 notes (rate-limit or auth issue), stopping early")
                break

            print(f"\n--- [{kw_idx+1}/{len(keywords)}] '{kw}' ---")

            try:
                notes = self.collect_one_keyword(kw, count=per_kw, max_detail=max_detail)
            except Exception as e:
                logger.error(f"Keyword '{kw}' failed: {e}")
                self.stats.errors.append(f"{kw}: {e}")
                notes = []

            # NER + 情感 enrich
            if notes and not no_enrich:
                self._enrich_notes(notes)

            # Time filter (since_date)
            if since_date and notes:
                filtered = []
                skipped = 0
                for note in notes:
                    pt = note.get("publish_time", "")
                    if pt:
                        try:
                            note_date = datetime.strptime(pt[:10], "%Y-%m-%d")
                            if note_date >= since_date:
                                filtered.append(note)
                            else:
                                skipped += 1
                        except ValueError:
                            filtered.append(note)  # Keep if unparseable
                    else:
                        filtered.append(note)  # Keep if no publish_time
                if skipped:
                    print(f"  [filter] Kept {len(filtered)}/{len(notes)} notes (skipped {skipped} before {since})")
                notes = filtered

            # 增量写盘: 每个关键词完成后追加 (中断不丢数据)
            with open(self.output_file, "a", encoding="utf-8") as f:
                for note in notes:
                    f.write(json.dumps(note, ensure_ascii=False) + "\n")

            total_notes += len(notes)
            self.stats.keywords_done = kw_idx + 1
            consecutive_empty = 0 if notes else consecutive_empty + 1  # 熔断计数: 有命中即清零

            # 进度
            elapsed = time.time() - start_time
            avg_time_per_kw = elapsed / (kw_idx + 1) if kw_idx > 0 else 0
            eta = avg_time_per_kw * (len(keywords) - kw_idx - 1)
            print(f"\n  '{kw}' done: {len(notes)} notes enriched. "
                  f"Total: {total_notes}. ETA: {eta/60:.0f}min")

            # 批次间冷却 (无结果的 kw 跳过, 省时间)
            if kw_idx < len(keywords) - 1 and notes:
                self.limiter.cooldown(BATCH_COOLDOWN + random.uniform(0, 1))

        # 关闭平台资源
        try:
            self.adapter.close()
        except Exception:
            pass

        # 汇总
        elapsed = time.time() - start_time
        print(f"\n{'='*60}")
        print(f"BATCH COLLECTION COMPLETE — {self.adapter.display_name}")
        print(f"  Time: {elapsed/60:.0f}min")
        print(f"  Keywords: {len(keywords)} done")
        print(f"  Total notes: {total_notes} (unique: {len(self.seen_ids)})")
        print(f"  Detail success: {self.stats.detail_fetched}")
        print(f"  Detail failed: {self.stats.detail_failed}")
        print(f"  Rate limits hit: {self.stats.rate_limits_hit}")
        print(f"  Output: {self.output_file}")
        print(f"{'='*60}")

        # 采集失败诊断 (2026-09-01): 凭证失效或整批空采时, 给出下一步指引而不是静默结束。
        if self.stats.auth_failures:
            print(f"\n⚠️  采集失败: 登录凭证失效/过期 ({self.adapter.display_name})")
            print(f"    {self.stats.auth_failures} 个关键词请求被判定为凭证问题。")
            print(f"    处理: {AUTH_GUIDANCE.get(self.platform_name, AUTH_GUIDANCE['_default'])}")
        elif total_notes == 0:
            print(f"\n⚠️  未采集到任何数据 ({self.adapter.display_name})")
            print("    可能原因:")
            print("    1) 登录凭证过期/失效 → 重新登录后重试")
            print("    2) 网络/反爬限制 → 稍后重试或更换网络")
            print("    3) 关键词在本平台确实无内容 → 换关键词试试")

        return []


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="多平台期货社交媒体数据批量采集",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python batch_collect.py                                         # 默认 xhs, 40关键词
  python batch_collect.py --platform weibo                        # 微博采集
  python batch_collect.py --platform xhs --per-kw 30              # 小红书, 每词30条
  python batch_collect.py --keywords 铁矿石 螺纹钢 原油           # 自定义关键词
  python batch_collect.py --no-detail                             # 不深挖 (微博推荐)
  python batch_collect.py --safe-mode                             # 安全模式
  python batch_collect.py --turbo                                 # 极速模式
        """,
    )
    parser.add_argument("--platform", type=str, default="xhs",
                        choices=list_platforms(),
                        help="目标平台 (默认 xhs)")
    parser.add_argument("--keywords", nargs="+", default=None,
                        help="采集关键词 (默认使用预设列表)")
    parser.add_argument("--per-kw", type=int, default=30,
                        help="每关键词搜索条数 (默认30)")
    parser.add_argument("--max-detail", type=int, default=10,
                        help="每关键词深挖条数 (默认10, 微博自动忽略)")
    parser.add_argument("--safe-mode", action="store_true",
                        help="安全模式: 更长的请求间隔 (750ms起步)")
    parser.add_argument("--turbo", action="store_true",
                        help="极速模式: 最小延时(120ms), Cookie新鲜时使用")
    parser.add_argument("--no-detail", action="store_true",
                        help="不获取详情 (速度快, 但可能无正文)")
    parser.add_argument("--no-enrich", action="store_true",
                        help="跳过NER+情感分析")
    parser.add_argument("--since", type=str, default=None,
                        help="只保留此日期之后的帖子 (YYYY-MM-DD)")
    parser.add_argument("--max-minutes", type=float, default=MAX_RUN_MINUTES,
                        help=f"全局时间预算分钟数, 超时在关键词边界收尾 (默认 {MAX_RUN_MINUTES})")
    parser.add_argument("--output", type=str, default=None,
                        help="输出文件名 (默认自动生成)")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 关键词
    keywords = args.keywords if args.keywords else DEFAULT_KEYWORDS.get(
        args.platform, DEFAULT_KEYWORDS_XHS
    )
    max_detail = 0 if args.no_detail else args.max_detail
    platform_name = args.platform

    from platforms import ADAPTER_DISPLAY_NAMES
    display = ADAPTER_DISPLAY_NAMES.get(platform_name, platform_name)

    mode_str = "TURBO" if args.turbo else "SAFE" if args.safe_mode else "FAST"
    print("=" * 60)
    print(f"  Multi-Platform Futures Data Collection — {display}")
    print("=" * 60)
    print(f"  Platform:    {display}")
    print(f"  Keywords:    {len(keywords)}")
    print(f"  Per keyword: search {args.per_kw}, detail {max_detail}")
    print(f"  Mode:        {mode_str}")
    print(f"  NER+Sentiment: {'OFF' if args.no_enrich else 'ON'}")
    if not args.no_detail:
        deep_fetch = max_detail if getattr(get_adapter(platform_name), 'needs_detail_fetch', True) else 0
        est_calls = len(keywords) * deep_fetch if deep_fetch else len(keywords)
        print(f"  Est. detail API calls: ~{est_calls}")
    print()

    collector = MultiPlatformCollector(
        platform=platform_name,
        safe_mode=args.safe_mode,
        turbo_mode=args.turbo,
    )

    try:
        collector.run(
            keywords=keywords,
            per_kw=args.per_kw,
            max_detail=max_detail,
            no_enrich=args.no_enrich,
            since=args.since,
            max_minutes=args.max_minutes,
        )
    except KeyboardInterrupt:
        print("\n\nInterrupted! Partial results saved to:")
        print(f"  {collector.output_file}")
        print(f"  Collected: {collector.stats.keywords_done}/{len(keywords)} keywords")
        print(f"  Total notes so far: ~{collector.stats.detail_fetched}")
    except Exception as e:
        logger.error(f"Fatal: {e}", exc_info=args.verbose)
        if collector.output_file:
            print(f"\nPartial results saved to: {collector.output_file}")


if __name__ == "__main__":
    main()
