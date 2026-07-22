# -*- coding: utf-8 -*-
"""探测 MCP 广场列表页的清单 API。

打开 https://www.modelscope.cn/mcp ，记录所有 XHR/fetch 响应，
凡响应体像 JSON 列表（含 mcp server 条目特征字段）就保存样本，
同时尝试滚动加载更多，找出分页参数规律。
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

OUT = Path(__file__).resolve().parent / "modelscope_output" / "_mcp_probe"
OUT.mkdir(parents=True, exist_ok=True)

URL = "https://www.modelscope.cn/mcp"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            viewport={"width": 1400, "height": 2000},
        )
        page = await ctx.new_page()
        hits = []

        async def on_response(resp):
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            u = resp.url
            if "modelscope" not in u:
                return
            try:
                ct = resp.headers.get("content-type", "")
                if "json" not in ct:
                    return
                body = await resp.body()
                txt = body.decode("utf-8", errors="ignore")
                if len(txt) < 50:
                    return
                j = json.loads(txt)
                s = json.dumps(j, ensure_ascii=False)
                # 特征：条目里有 server / mcp / tools 字样或是列表
                if any(k in s.lower() for k in ("mcpserver", "mcp_server", '"servers"', '"tools"', '"slug"')) or (
                    '"name"' in s and '"id"' in s and len(s) > 2000
                ):
                    idx = len(hits)
                    fname = OUT / f"hit_{idx}.json"
                    fname.write_text(txt, encoding="utf-8")
                    hits.append((resp.status, u[:160], len(txt), fname.name))
                    print(f"HIT {resp.status} {u[:140]} ({len(txt)}B)", flush=True)
            except Exception:
                pass

        page.on("response", on_response)
        print(f">>> {URL}", flush=True)
        await page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(8)

        # 滚动几次触发分页
        for i in range(5):
            await page.mouse.wheel(0, 1500)
            await asyncio.sleep(2.5)

        # 再等等懒加载
        await asyncio.sleep(4)

        print("\n=== 命中接口 ===", flush=True)
        for h in hits:
            print(h, flush=True)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
