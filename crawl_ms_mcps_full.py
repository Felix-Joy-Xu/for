# -*- coding: utf-8 -*-
"""魔搭 MCP 广场全量清单爬虫 - Playwright 版。

背景：
- MCP 清单接口 PUT /api/v1/dolphin/mcpServers 有 WAF JS 挑战，
  裸 requests（即使带登录 cookie）也只能拿到挑战页 HTML。
- 但在浏览器里打开 /mcp 页面通过挑战后，页面上下文里的 fetch 可正常翻页。
- 全站 TotalCount = 9,781（2026-07-22 实测），旧清单 mcps_all.json 仅 1,489（15%）。

策略：playwright 打开 /mcp → 在页面上下文 PUT 翻页（PageSize=30）→ 逐条写 jsonl。
断点：state_ms_mcp_full.json 记录已完成的页码；输出按 Publisher（@org/name）去重。

输出: modelscope_output/mcp_full.jsonl / mcp_full.json
环境变量 MS_MCP_MAX_PAGES=N 限制本轮页数（调试用）。
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "modelscope_output"
OUT_JSONL = OUT_DIR / "mcp_full.jsonl"
OUT_JSON = OUT_DIR / "mcp_full.json"
STATE_FILE = OUT_DIR / "state_ms_mcp_full.json"

PAGE_SIZE = 30
DELAY = 0.3
MAX_PAGES = int(os.environ.get("MS_MCP_MAX_PAGES", "0") or 0)

FETCH_JS = """
async (pn) => {
    const r = await fetch('/api/v1/dolphin/mcpServers', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({PageNumber: pn, PageSize: %d})
    });
    return await r.text();
}
""" % PAGE_SIZE


async def fetch_page(page, pn, retries=4):
    """在页面上下文里取一页。返回 (data_dict, ok)。"""
    for attempt in range(retries):
        try:
            txt = await page.evaluate(FETCH_JS, pn)
            j = json.loads(txt)
            if j.get("Success") or j.get("Code") == 200:
                return (j.get("Data") or {}).get("McpServer") or {}, True
        except Exception:
            pass
        await asyncio.sleep(2 ** attempt)
    return {}, False


async def main():
    done_pages = set()
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            done_pages = set(json.load(f))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        )
        page = await ctx.new_page()
        print("打开 MCP 广场页（过 WAF 挑战）...", flush=True)
        await page.goto("https://www.modelscope.cn/mcp",
                        wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(6)

        # 先取第 1 页拿总数
        d, ok = await fetch_page(page, 1)
        if not ok:
            print("第 1 页获取失败，WAF 可能未通过。", flush=True)
            await browser.close()
            sys.exit(3)
        total = d.get("TotalCount") or 0
        pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        print(f"全站 MCP 总数 {total}，共 {pages} 页，已完成 {len(done_pages)} 页", flush=True)

        written = 0
        consecutive_errors = 0
        pages_this_run = 0

        with open(OUT_JSONL, "a", encoding="utf-8") as out_f:
            for pn in range(1, pages + 1):
                if pn in done_pages:
                    continue
                d, ok = await fetch_page(page, pn)
                if not ok:
                    consecutive_errors += 1
                    print(f"p{pn} 失败（连续 {consecutive_errors}）", flush=True)
                    if consecutive_errors >= 20:
                        print("连续失败过多，中止（断点已保存）。", flush=True)
                        await browser.close()
                        sys.exit(3)
                    await asyncio.sleep(5)
                    continue

                servers = d.get("McpServers") or []
                for s in servers:
                    sid = s.get("Publisher") or (
                        (s.get("Path") or "") + "/" + (s.get("Name") or "")).strip("/")
                    if not sid:
                        continue
                    rec = {
                        "id": sid,
                        "name": s.get("Name"),
                        "chinese_name": s.get("ChineseName"),
                        "description": s.get("AbstractCN") or s.get("Abstract"),
                        "category": s.get("Category"),
                        "tags": s.get("Tags"),
                        "stars": s.get("Stars"),
                        "view_count": s.get("ViewCount"),
                        "call_volume": s.get("CallVolume"),
                        "tools": s.get("Tools"),
                        "verified": s.get("Verifed"),
                        "hosted": s.get("Hosted"),
                        "from_site": s.get("FromSite"),
                        "from_site_url": s.get("FromSiteUrl"),
                        "license": s.get("License"),
                        "created_at": s.get("GmtCreated"),
                        "updated_at": s.get("GmtUpdated"),
                    }
                    out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    written += 1
                out_f.flush()

                consecutive_errors = 0
                done_pages.add(pn)
                pages_this_run += 1
                if pages_this_run % 20 == 0:
                    with open(STATE_FILE, "w", encoding="utf-8") as sf:
                        json.dump(sorted(done_pages), sf)
                    print(f"=== 进度 {len(done_pages)}/{pages} 页，本轮 +{written} 条 ===", flush=True)
                if MAX_PAGES and pages_this_run >= MAX_PAGES:
                    print(f"达到 MS_MCP_MAX_PAGES={MAX_PAGES}，提前结束（调试模式）", flush=True)
                    break
                await asyncio.sleep(DELAY)

        await browser.close()

    with open(STATE_FILE, "w", encoding="utf-8") as sf:
        json.dump(sorted(done_pages), sf)

    # 去重合并
    items = {}
    if OUT_JSONL.exists():
        with open(OUT_JSONL, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    it = json.loads(line)
                except Exception:
                    continue
                if it.get("id"):
                    items[it["id"]] = it
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(list(items.values()), f, ensure_ascii=False)
    print(f"完成。去重后 MCP {len(items)} 个 → {OUT_JSON}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
