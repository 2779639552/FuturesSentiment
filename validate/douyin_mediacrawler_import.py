"""
douyin_mediacrawler_import.py — MediaCrawler 输出 → 思路2统一 JSONL
====================================================================
把 MediaCrawler(project4/MediaCrawler)采集的抖音视频+评论 JSONL 转换成
思路2统一 Schema(batch_douyin_*.jsonl),供 trend_aggregator /
generate_tradingagents_sentiment 等下游管线直接消费。

【数据来源】MediaCrawler 的 JSONL 输出(结构化 API 字段,非 DOM 猜测):
    data/douyin/*contents*.jsonl — 视频条目(aweme_id/title/create_time/互动数)
    data/douyin/*comments*.jsonl — 评论条目(comment_id/content/create_time/点赞)
【形态约定】与 platforms/douyin_adapter.py 一致:
    视频条目 note_id="dy:v:{aweme_id}"  note_type="video"
    评论条目 note_id="dy:c:{comment_id}" note_type="comment"(desc=评论正文)
【enrich】复用 batch_collect 的 NER(FuturesNER)+ 规则情感(SentimentAnalyzer),
    产出 varieties/contracts/sentiment 等字段 —— 与其他平台 JSONL 同构。
【去重】跨增量: 扫描 output/ 已有 batch_douyin_*.jsonl 收集 (platform, note_id),
    已见过的条目跳过(与 batch_collect 的 seen_ids 同口径)。

用法:
    python douyin_mediacrawler_import.py                 # 导入全部新数据
    python douyin_mediacrawler_import.py --mc-dir PATH   # 指定 MediaCrawler data 目录
    python douyin_mediacrawler_import.py --limit 200     # 最多导入条数(试跑用)
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

VALIDATE_DIR = Path(__file__).parent
OUTPUT_DIR = VALIDATE_DIR / "output"
DEFAULT_MC_DATA_DIR = Path(r"C:\Users\19168\Desktop\project4\MediaCrawler\data\douyin")

# ── 统一 Schema 必需字段(与 platforms/base.py UNIFIED_SCHEMA_FIELDS 对齐) ──


def _ts_to_str(ts) -> str:
    """unix 秒级时间戳 → 'YYYY-MM-DD HH:MM:SS';解析失败返回空串。"""
    try:
        ts = int(str(ts).strip() or 0)
        if ts <= 0:
            return ""
        if ts > 10_000_000_000:  # 毫秒时间戳兜底
            ts //= 1000
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return ""


def _to_int(val) -> int:
    try:
        return int(str(val).strip() or 0)
    except ValueError:
        return 0


def _video_item(rec: dict) -> dict | None:
    """MediaCrawler contents 行 → 视频统一条目。"""
    aweme_id = str(rec.get("aweme_id") or "").strip()
    desc = (rec.get("desc") or rec.get("title") or "").strip()
    if not aweme_id or len(desc) < 4:
        return None
    return {
        "platform": "douyin",
        "note_id": f"dy:v:{aweme_id}",
        "title": desc[:60],
        "desc": desc[:2000],
        "author_name": rec.get("nickname", ""),
        "author_id": rec.get("creator_hash", ""),
        "author_fans": 0,
        "like_count": _to_int(rec.get("liked_count")),
        "comment_count": _to_int(rec.get("comment_count")),
        "collect_count": _to_int(rec.get("collected_count")),
        "share_count": _to_int(rec.get("share_count")),
        "tags": [],
        "note_type": "video",
        "publish_time": _ts_to_str(rec.get("create_time")),
        "ip_location": "",
        "keyword": rec.get("source_keyword", ""),
        "url": rec.get("aweme_url") or f"https://www.douyin.com/video/{aweme_id}",
        "desc_length": len(desc),
        "image_count": 0,
        "is_video": True,
        "image_urls": [],
    }


def _comment_item(rec: dict, video_by_id: dict) -> dict | None:
    """MediaCrawler comments 行 → 评论统一条目(核心价值所在)。"""
    cid = str(rec.get("comment_id") or "").strip()
    text = (rec.get("content") or "").strip()
    if not cid or len(text) < 4:
        return None
    aweme_id = str(rec.get("aweme_id") or "").strip()
    video = video_by_id.get(aweme_id, {})
    is_sub = str(rec.get("parent_comment_id", "0")) not in ("0", "", "None")
    return {
        "platform": "douyin",
        "note_id": f"dy:c:{cid}",
        "title": f"[抖音评论{'·回复' if is_sub else ''}] {video.get('desc', '')[:50]}",
        "desc": text[:2000],
        "author_name": rec.get("nickname", ""),
        "author_id": rec.get("creator_hash", ""),
        "author_fans": 0,
        "like_count": _to_int(rec.get("like_count")),
        "comment_count": _to_int(rec.get("sub_comment_count")),
        "collect_count": 0,
        "share_count": 0,
        "tags": [],
        "note_type": "comment",
        "publish_time": _ts_to_str(rec.get("create_time")),
        "ip_location": "",
        "keyword": video.get("keyword", ""),
        "url": video.get("url") or f"https://www.douyin.com/video/{aweme_id}",
        "desc_length": len(text),
        "image_count": 0,
        "is_video": False,
        "image_urls": [],
    }


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def _load_seen_ids() -> set:
    """扫已有 batch_douyin_*.jsonl,收集已入库 note_id(增量导入防重)。"""
    seen = set()
    for path in OUTPUT_DIR.glob("batch_douyin_*.jsonl"):
        for rec in _read_jsonl(path):
            if rec.get("platform") == "douyin":
                seen.add(rec.get("note_id", ""))
    return seen


def _enrich(notes: list[dict]) -> list[dict]:
    """NER + 规则情感 enrich(与 batch_collect._enrich_notes 同逻辑,平台无关)。"""
    from ner import FuturesNER  # 【调用包】品种识别(50品种×多别名)
    from sentiment import SentimentAnalyzer  # 【调用包】规则情感引擎(7级分类)

    ner, sent = FuturesNER(), SentimentAnalyzer()
    for note in notes:
        text = (note.get("title", "") + " " + note.get("desc", "")).strip()
        entities = ner.extract(text)
        note["varieties"] = entities["varieties"]
        note["contracts"] = entities["contracts"]
        note["variety_count"] = entities["variety_count"]
        r = sent.analyze(text)
        note["sentiment"] = r["sentiment"]
        note["sentiment_score"] = r["score"]
        note["sentiment_confidence"] = r["confidence"]
        note["variety_sentiments"] = sent.analyze_aspects(text, entities["varieties"])
    return notes


def main():
    ap = argparse.ArgumentParser(description="MediaCrawler 抖音数据 → 思路2统一 JSONL")
    ap.add_argument("--mc-dir", default=str(DEFAULT_MC_DATA_DIR),
                    help="MediaCrawler 输出目录(含 *contents*.jsonl / *comments*.jsonl)")
    ap.add_argument("--limit", type=int, default=0, help="最多导入条数(0=不限,试跑用)")
    ap.add_argument("--no-enrich", action="store_true", help="跳过 NER+情感 enrich(调试用)")
    args = ap.parse_args()

    mc_dir = Path(args.mc_dir)
    contents_files = sorted(mc_dir.glob("*contents*.jsonl"))
    comments_files = sorted(mc_dir.glob("*comments*.jsonl"))
    if not comments_files and not contents_files:
        print(f"ERROR: {mc_dir} 下没有 MediaCrawler 输出(*contents*/​*comments*.jsonl)")
        return 1

    # 1) 视频条目(供评论挂接标题/链接)
    video_by_id: dict[str, dict] = {}
    videos: list[dict] = []
    for path in contents_files:
        for rec in _read_jsonl(path):
            item = _video_item(rec)
            if item:
                video_by_id[item["note_id"][len("dy:v:"):]] = item
                videos.append(item)

    # 2) 评论条目(核心价值)
    comments: list[dict] = []
    for path in comments_files:
        for rec in _read_jsonl(path):
            item = _comment_item(rec, video_by_id)
            if item:
                comments.append(item)

    # 3) 增量去重(评论优先保留,视频兜底)
    seen = _load_seen_ids()
    fresh = []
    for item in comments + videos:
        if item["note_id"] in seen:
            continue
        seen.add(item["note_id"])
        fresh.append(item)
    if args.limit and len(fresh) > args.limit:
        fresh = fresh[:args.limit]

    if not fresh:
        print("没有新增条目(全部已导入过)。")
        return 0

    # 4) NER + 情感 enrich
    if not args.no_enrich:
        fresh = _enrich(fresh)

    # 5) 写 batch_douyin_*.jsonl(与 batch_collect 输出同目录同命名,下游无缝消费)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"batch_douyin_{ts}.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for rec in fresh:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    n_comment = sum(1 for r in fresh if r["note_type"] == "comment")
    n_video = len(fresh) - n_comment
    print(f"OK: 导出 {len(fresh)} 条(评论 {n_comment} + 视频 {n_video}) → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
