# -*- coding: utf-8 -*-
"""探测魔搭 /models 列表页的分页请求：方法、URL、请求体、翻页时变化。"""
import asyncio
from playwright.async_api import async_playwright


async def main():
    seen = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        page = await ctx.new_page()

        async def on_request(req):
            if "modelscope.cn/api" in req.url and req.resource_type in ("xhr", "fetch"):
                if any(k in req.url.lower() for k in ("model", "search", "dolphin", "list")):
                    seen.append((req.method, req.url, (req.post_data or "")[:300]))

        page.on("request", on_request)

        print(">>> https://www.modelscope.cn/models")
        await page.goto("https://www.modelscope.cn/models", wait_until="domcontentloaded", timeout=40000)
        await asyncio.sleep(6)

        # 点第 2 页 / 下一页
        for sel in ["下一页", "2"]:
            try:
                el = page.get_by_text(sel, exact=True).last
                if await el.count() > 0:
                    await el.click(timeout=2500)
                    print("点击了:", sel)
                    await asyncio.sleep(4)
                    break
            except Exception:
                pass

        await browser.close()

    print("\n=== 捕获到的列表请求 ===")
    for m, u, body in seen:
        print(m, u[:130])
        if body:
            print("   body:", body)


if __name__ == "__main__":
    asyncio.run(main())
