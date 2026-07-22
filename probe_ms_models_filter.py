# -*- coding: utf-8 -*-
"""探测 /models 页点击任务筛选后的请求体参数。"""
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
            if "dolphin" in req.url and req.resource_type in ("xhr", "fetch"):
                seen.append((req.method, req.url, (req.post_data or "")[:400]))

        page.on("request", on_request)

        await page.goto("https://www.modelscope.cn/models", wait_until="domcontentloaded", timeout=40000)
        await asyncio.sleep(6)

        # 尝试点击左侧任务筛选（如"自然语言处理"/"文本分类"等）
        for text in ["自然语言处理", "文本分类", "计算机视觉", "语音识别"]:
            try:
                el = page.get_by_text(text, exact=False).first
                if await el.count() > 0:
                    await el.click(timeout=2500)
                    print("点击了筛选:", text)
                    await asyncio.sleep(4)
                    break
            except Exception:
                pass

        # 再试试点排序（按下载量/最新）
        for text in ["下载量", "最新", "最多下载"]:
            try:
                el = page.get_by_text(text, exact=False).first
                if await el.count() > 0:
                    await el.click(timeout=2500)
                    print("点击了排序:", text)
                    await asyncio.sleep(4)
                    break
            except Exception:
                pass

        await browser.close()

    print("\n=== dolphin 请求 ===")
    for m, u, body in seen:
        print(m, u[:110])
        if body:
            print("   body:", body)


if __name__ == "__main__":
    asyncio.run(main())
