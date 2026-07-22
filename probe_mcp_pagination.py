# -*- coding: utf-8 -*-
"""在页面上下文里验证 dolphin/mcpServers 的翻页参数。"""
import asyncio
import json
from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        )
        page = await ctx.new_page()
        await page.goto("https://www.modelscope.cn/mcp", wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(6)

        variants = [
            ("GET ?PageNumber=2&PageSize=30",
             "fetch('/api/v1/dolphin/mcpServers?PageNumber=2&PageSize=30').then(r=>r.text())"),
            ("GET ?page_number=2&page_size=30",
             "fetch('/api/v1/dolphin/mcpServers?page_number=2&page_size=30').then(r=>r.text())"),
            ("PUT {PageNumber:2,PageSize:30}",
             "fetch('/api/v1/dolphin/mcpServers',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({PageNumber:2,PageSize:30})}).then(r=>r.text())"),
            ("POST {PageNumber:2,PageSize:30}",
             "fetch('/api/v1/dolphin/mcpServers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({PageNumber:2,PageSize:30})}).then(r=>r.text())"),
            ("PUT {page_number:2,page_size:30}",
             "fetch('/api/v1/dolphin/mcpServers',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({page_number:2,page_size:30})}).then(r=>r.text())"),
        ]
        for label, expr in variants:
            try:
                txt = await page.evaluate(expr)
                is_json = txt.lstrip().startswith("{")
                head = txt[:100].replace("\n", " ")
                print(f"{label}: {'JSON' if is_json else 'HTML'} | {head}", flush=True)
                if is_json:
                    j = json.loads(txt)
                    ms = j.get("Data", {}).get("McpServer", {})
                    servers = ms.get("McpServers", [])
                    first = servers[0]["Publisher"] if servers else "?"
                    print(f"   -> Total={ms.get('TotalCount')} 本页={len(servers)} 首条={first}", flush=True)
            except Exception as e:
                print(f"{label}: ERR {e}", flush=True)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
