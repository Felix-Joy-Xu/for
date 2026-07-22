# -*- coding: utf-8 -*-
"""探测 MCP/数据集/创空间页面是否有评论区及其 API。"""
import asyncio
from playwright.async_api import async_playwright

PAGES = [
    ("MCP", "https://www.modelscope.cn/mcp/servers/@modelcontextprotocol/fetch"),
    ("数据集", "https://www.modelscope.cn/datasets/InternScience/ResearchClawBench"),
    ("创空间", "https://www.modelscope.cn/studios/Coloring/mcp-playground"),
]


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        page = await ctx.new_page()

        for label, url in PAGES:
            seen = []

            async def on_response(resp):
                u = resp.url.lower()
                if "modelscope.cn/api" in u and resp.request.resource_type in ("xhr", "fetch"):
                    if any(k in u for k in ("comment", "discussion", "issue", "review", "rating")):
                        seen.append((resp.status, resp.url[:130]))

            page.on("response", on_response)
            print(f">>> [{label}] {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(5)
                # 找评论相关 Tab
                for text in ["评论", "讨论", "评价", "Comment", "Review"]:
                    try:
                        el = page.get_by_text(text, exact=False).first
                        if await el.count() > 0:
                            await el.click(timeout=2000)
                            print(f"  点击了: {text}")
                            await asyncio.sleep(4)
                            break
                    except Exception:
                        pass
                # 页面上是否有评论字样
                content = await page.content()
                for kw in ["评论", "评价", "讨论区"]:
                    if kw in content:
                        print(f"  页面含关键词: {kw}")
            except Exception as e:
                print("  页面错误:", str(e)[:80])
            page.remove_listener("response", on_response)
            print("  评论相关 XHR:", seen if seen else "无")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
