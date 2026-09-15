"""
期货价格获取: akshare → 品种日线数据
输出: output/trends/{variety}_price.json
"""

import json, os, sys
from pathlib import Path
from datetime import datetime

OUTPUT_DIR = Path(__file__).parent / "output" / "trends"

# ═══════════════════════════════════════════════════════════════════════
# 换月跳空处理(后复权)——与 AgentSense 主仓库 price_fetcher.py 同口径同步。
# 新浪主连(如 RB0)是"简单拼接、未复权"序列,换月时会有伪跳空(含盘中拼接,
# 日内波动可超涨跌停)。后复权在换月点把历史价格缩放,使序列连续;最近一根
# bar 因子=1(当前价不变)。检测用"收盘到收盘缺口"超阈值(默认 8%),因回测
# 用收盘结算、且真实行情几乎不可能收盘缺口超 8%(宁漏勿误)。
# ═══════════════════════════════════════════════════════════════════════
ROLLOVER_GAP_THRESHOLD_PCT = 8.0  # 【变量】换月跳空判定阈值(%):无真实日历回退时,收盘缺口超过它即视为换月

# 真实换月日历(AgentSense/scripts/build_rollover_calendar.py 生成)。
# 换月日期是"查证"出来的,不是启发式猜测;有日历的品种优先用日历。
_rollover_calendar_cache = None  # 【变量】换月日历内存缓存(懒加载,None=未加载)


def _load_rollover_calendar():
    """加载真实换月日历(懒加载+缓存)。失败返回空 dict。"""
    global _rollover_calendar_cache
    if _rollover_calendar_cache is not None:
        return _rollover_calendar_cache
    cal_path = OUTPUT_DIR / "_rollover_calendar.json"
    try:
        if cal_path.exists():
            with open(cal_path, "r", encoding="utf-8") as f:
                _rollover_calendar_cache = json.load(f)
        else:
            _rollover_calendar_cache = {}
    except Exception:
        _rollover_calendar_cache = {}
    return _rollover_calendar_cache


def _detect_rollover_dates(prices, threshold_pct=ROLLOVER_GAP_THRESHOLD_PCT):
    """识别主力连续序列中的换月点(收盘缺口超阈值)。返回 bar 下标列表。"""
    roll = []
    for i in range(1, len(prices)):
        prev_close = float(prices[i - 1]["close"])
        if prev_close <= 0:
            continue
        gap = (float(prices[i]["close"]) / prev_close - 1.0) * 100.0
        if abs(gap) >= threshold_pct:
            roll.append(i)
    return roll


def _backward_adjust(prices, threshold_pct=ROLLOVER_GAP_THRESHOLD_PCT, calendar_dates=None):
    """对主力连续序列做后复权(原地修改 prices)。返回 (prices, roll_idx)。

    calendar_dates: 真实换月日期集合;传入则只用这些日期作为换月点(查证过),
    不再依赖 8% 阈值。为 None 时回退到 8% 收盘缺口启发式。
    """
    if calendar_dates is not None:
        roll_idx = {i for i, p in enumerate(prices) if p["date"] in calendar_dates}
    else:
        roll_idx = set(_detect_rollover_dates(prices, threshold_pct))
    n = len(prices)
    adj = [1.0] * n
    for i in range(n - 2, -1, -1):
        if (i + 1) in roll_idx:
            prev_close = float(prices[i]["close"])
            cur_close = float(prices[i + 1]["close"])
            adj[i] = adj[i + 1] * (cur_close / prev_close) if prev_close > 0 else adj[i + 1]
        else:
            adj[i] = adj[i + 1]
    for i in range(n):
        a = adj[i]
        prices[i]["open"] = round(float(prices[i]["open"]) * a, 2)
        prices[i]["high"] = round(float(prices[i]["high"]) * a, 2)
        prices[i]["low"] = round(float(prices[i]["low"]) * a, 2)
        prices[i]["close"] = round(float(prices[i]["close"]) * a, 2)
        if i > 0:
            prev_close = prices[i - 1]["close"]
            prices[i]["change_pct"] = (
                round((prices[i]["close"] / prev_close - 1.0) * 100.0, 2)
                if prev_close
                else 0
            )
        else:
            prices[i]["change_pct"] = 0
    return prices, sorted(roll_idx)


# 品种名 → akshare symbol
VARIETY_SYMBOLS = {
    "螺纹钢": "RB0", "铁矿石": "I0", "焦炭": "J0", "焦煤": "JM0",
    "热卷": "HC0", "硅铁": "SF0", "锰硅": "SM0",
    "沪铜": "CU0", "沪铝": "AL0", "沪锌": "ZN0", "沪镍": "NI0",
    "沪铅": "PB0", "沪锡": "SN0", "黄金": "AU0", "白银": "AG0",
    "原油": "SC0", "PTA": "TA0", "甲醇": "MA0", "PVC": "V0",
    "PP": "PP0", "塑料": "L0", "橡胶": "RU0", "沥青": "BU0",
    "尿素": "UR0", "纯碱": "SA0", "玻璃": "FG0", "乙二醇": "EG0",
    "苯乙烯": "EB0", "短纤": "PF0",
    "豆粕": "M0", "豆油": "Y0", "棕榈油": "P0", "菜粕": "RM0",
    "菜油": "OI0", "白糖": "SR0", "棉花": "CF0", "玉米": "C0",
    "淀粉": "CS0", "鸡蛋": "JD0", "生猪": "LH0", "苹果": "AP0",
    "红枣": "CJ0", "花生": "PK0", "碳酸锂": "LC0", "工业硅": "SI0",
    "氧化铝": "AO0",
    # 2026-09-01 扩能化整组 12 品种(新增 4 个主连代码)
    "燃料油": "FU0", "低硫燃料油": "LU0", "20号胶": "NR0", "对二甲苯": "PX0",
    # 2026-09-01 扩 19 非金融品种(新增 2 个主连代码)
    "烧碱": "SH0", "线材": "WR0",
    # 2026-09-09 品种池收缩补入(池内此前缺映射的品种:LPG/丁二烯橡胶/纯苯/多晶硅)
    "LPG": "PG0", "丁二烯橡胶": "BR0", "纯苯": "BZ0", "多晶硅": "PS0",
}


def fetch_all(sentiment_index_path: str = None) -> dict:
    """获取所有有情绪数据的品种价格"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 读取情绪索引确定要拉哪些品种
    varieties = list(VARIETY_SYMBOLS.keys())
    if sentiment_index_path and os.path.exists(sentiment_index_path):
        with open(sentiment_index_path, "r", encoding="utf-8") as f:
            idx = json.load(f)
        varieties = [v for v in idx.keys() if v in VARIETY_SYMBOLS]
        print(f"Fetching prices for {len(varieties)} varieties with sentiment data")
    else:
        print(f"Fetching prices for all {len(varieties)} varieties")

    try:
        import akshare as ak
    except ImportError:
        print("ERROR: pip install akshare")
        return {}

    result = {}
    for i, variety in enumerate(varieties):
        symbol = VARIETY_SYMBOLS.get(variety)
        if not symbol:
            continue

        try:
            df = ak.futures_main_sina(symbol=symbol)
            # 标准化列名
            df.columns = ["date", "open", "high", "low", "close", "volume", "position", "settle"]
            # 只保留最近180天
            df = df.tail(180)
            # 转list
            prices = []
            for _, row in df.iterrows():
                prices.append({
                    "date": str(row["date"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row["volume"]),
                })

            # 计算涨跌幅
            for j in range(1, len(prices)):
                prev_close = prices[j - 1]["close"]
                if prev_close > 0:
                    prices[j]["change_pct"] = round(
                        (prices[j]["close"] - prev_close) / prev_close * 100, 2
                    )
                else:
                    prices[j]["change_pct"] = 0
            if prices:
                prices[0]["change_pct"] = 0

            latest = prices[-1] if prices else None

            # 换月跳空后复权(与主仓库 price_fetcher 同口径),消除主力连续伪缺口
            # 优先用真实换月日历(查证的主力切换日);不在日历时回退 8% 启发式。
            cal_dates = {r["date"] for r in _load_rollover_calendar().get(variety, {}).get("rollover_dates", [])}
            if prices:
                _, roll_idx = _backward_adjust(prices, calendar_dates=cal_dates or None)  # 【调用函数】后复权(原地修改 prices)
                roll_dates = [prices[i]["date"] for i in roll_idx]  # 【变量】换月日期清单
            else:
                roll_dates = []

            result[variety] = {
                "prices": prices,
                "latest": latest,
                "date_range": f"{prices[0]['date']} ~ {prices[-1]['date']}" if prices else "N/A",
                "adjusted": bool(roll_dates),  # 【变量】后复权标记
                "rollover_dates": roll_dates,  # 【变量】换月日期(真实日历查证 or 8% 启发式)
                "rollover_method": "calendar" if cal_dates else "heuristic",  # 【变量】换月来源
            }

            # 写文件
            out_path = OUTPUT_DIR / f"{variety}_price.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result[variety], f, ensure_ascii=False)

            print(f"  [{i + 1}/{len(varieties)}] {variety}({symbol}): {len(prices)} days")

        except Exception as e:
            print(f"  [{i + 1}/{len(varieties)}] {variety}({symbol}): ERROR - {e}")

    print(f"\nSaved to {OUTPUT_DIR}")
    return result


if __name__ == "__main__":
    idx_path = str(OUTPUT_DIR / "_index.json") if (OUTPUT_DIR / "_index.json").exists() else None
    fetch_all(idx_path)
