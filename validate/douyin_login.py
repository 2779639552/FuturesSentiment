"""
抖音扫码登录 (Playwright, 自动检测)
==================================
用法: python douyin_login.py

1. 打开 Chromium 浏览器 → 访问 douyin.com
2. 手机抖音扫码登录
3. 脚本自动检测登录成功 → 保存 storage_state → 关闭浏览器

抖音登录态持久化: cookies + localStorage (storage_state)。
登录成功标志: 存在 sessionid / sessionid_ss cookie(douyin.com 域)。

保存后可用:
  python probe_douyin_comments.py --use-login-state   # 带登录态重测
  python batch_collect.py --platform douyin --per-kw 10
"""

import sys, time, json
from pathlib import Path

CREDENTIALS_DIR = Path(__file__).parent / "credentials"
CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = CREDENTIALS_DIR / "douyin_login_state.json"
LOGIN_TIMEOUT = 240


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Need: pip install playwright && playwright install chromium")
        sys.exit(1)

    print("=" * 60)
    print("  Douyin Login Tool (Auto-detect)")
    print("=" * 60)
    print()
    print("Opening browser...")
    print("Please scan QR code with the Douyin mobile app to login")
    print(f"Waiting up to {LOGIN_TIMEOUT}s...")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded")

        # Auto-detect login by checking for sessionid cookie (douyin.com domain)
        detected = False
        start = time.time()
        while time.time() - start < LOGIN_TIMEOUT:
            cookies = context.cookies()
            cookie_names = {c["name"] for c in cookies}
            if "sessionid" in cookie_names or "sessionid_ss" in cookie_names:
                detected = True
                break
            time.sleep(2)

        if not detected:
            print(f"\nTimeout! Login not detected within {LOGIN_TIMEOUT}s.")
            print("Cookies found:")
            for c in context.cookies()[:15]:
                print(f"  {c['name']} = {str(c['value'])[:40]}...")
            browser.close()
            sys.exit(1)

        # Save storage_state
        state = context.storage_state()
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nDone! Login state saved to: {STATE_FILE}")
        print(f"  Cookies: {len(state.get('cookies', []))}")
        browser.close()

    print()
    print("Now run:")
    print("  python probe_douyin_comments.py --use-login-state")
    print("  python batch_collect.py --platform douyin --per-kw 10")


if __name__ == "__main__":
    main()
