"""
平台适配器注册表
===============
通过 get_adapter(name) 获取平台适配器实例。

支持的平台:
    xhs    — 小红书 (Spider_XHS API)
    weibo  — 微博 (m.weibo.cn 移动端 API)
    zhihu  — 知乎 (Playwright/CDP 浏览器)
    xueqiu — 雪球 (xueqiu.com API, Cookie 认证)
    eastmoney_guba — 东方财富股吧 (Playwright APIRequestContext, WAF TLS 指纹门)
    douyin — 抖音 (Playwright 浏览器模拟;视频+评论双形态,评论=独立 item)
"""

from .base import PlatformAdapter
from .xhs_adapter import XHSAdapter
from .weibo_adapter import WeiboAdapter
from .zhihu_adapter import ZhihuAdapter
from .xueqiu_adapter import XueqiuAdapter
from .eastmoney_guba_adapter import EastmoneyGubaAdapter
from .douyin_adapter import DouyinAdapter

# 注册表
ADAPTERS: dict[str, type[PlatformAdapter]] = {
    "xhs": XHSAdapter,
    "weibo": WeiboAdapter,
    "zhihu": ZhihuAdapter,
    "xueqiu": XueqiuAdapter,
    "eastmoney_guba": EastmoneyGubaAdapter,
    "douyin": DouyinAdapter,
}

ADAPTER_DISPLAY_NAMES = {
    "xhs": "小红书",
    "weibo": "微博",
    "zhihu": "知乎",
    "xueqiu": "雪球",
    "eastmoney_guba": "东财股吧",
    "douyin": "抖音",
}


def get_adapter(name: str) -> PlatformAdapter:
    """获取平台适配器实例。

    Args:
        name: 平台名 ("xhs" | "weibo" | "zhihu")

    Returns:
        PlatformAdapter 实例

    Raises:
        ValueError: 未知平台名
    """
    name = name.lower().strip()
    if name not in ADAPTERS:
        available = ", ".join(ADAPTERS.keys())
        raise ValueError(f"Unknown platform '{name}'. Available: {available}")
    return ADAPTERS[name]()


def list_platforms() -> list[str]:
    """列出所有已注册平台"""
    return list(ADAPTERS.keys())
