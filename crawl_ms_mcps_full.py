# -*- coding: utf-8 -*-
"""魔搭 MCP 全量爬虫 - Query BFS 前缀切片版（会话管理，防 WAF/限流挂起）。

背景：
- PUT /api/v1/dolphin/mcpServers 存在 PageNumber x PageSize <= 300 窗口限制，
  单纯翻页最多 300 条。
- 接口支持 Query（名字 contains）参数，每个 Query 独立享 300 窗口。
- 策略：BFS 前缀切片。从 36 个单字符（a-z0-9）开始，total > 300 的词
  递归追加字符细分。按 id 去重，全量约 9845 个。
- 反爬：WAF 限流时 fetch 可能永久挂起；每词 JS 内超时兜底，
  连续失败自动重建浏览器会话（新 context + 重新过 WAF）。

输出: modelscope_output/mcp_full.jsonl（逐词追加，含 _query）
      modelscope_output/mcp_full.json（结束时去重合并）
状态: modelscope_output/state_ms_mcp_full.json（已完成的词，断点续爬）
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

ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
WINDOW = 300
PAGE_SIZE = 300
DELAY = 0.25
MAX_DEPTH = 6
RECONNECT_AFTER = 5      # 连续失败 N 次后重建会话
MAX_TERMS = int(os.environ.get("MS_MCP_MAX_TERMS", "0") or 0)

FETCH_JS = """
async (o) => {
    const timeout = new Promise((_, rej) => setTimeout(() => rej(new Error('JS_TIMEOUT')), 20000));
    const doFetch = (async () => {
        const r = await fetch('/api/v1/dolphin/mcpServers', {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({PageSize: o.ps, PageNumber: o.pn, Query: o.q, Criterion: []})
        });
        const j = await r.json();
        const d = j.Data || {};
        const s = d.McpServer || {};
        const arr = s.McpServers || [];
        return {n: arr.length, total: s.TotalCount, items: arr};
    })();
    try {
        return await Promise.race([doFetch, timeout]);
    } catch (e) {
        return {n: -1, total: null, items: [], err: String(e).slice(0, 40)};
    }
}
"""


async def fetch_query(page, query, retries=3):
    """用 ps300 p1 取一个词的结果。返回 (items, total, ok)。"""
    for attempt in range(retries):
        try:
            r = await page.evaluate(FETCH_JS, {"ps": PAGE_SIZE, "pn": 1, "q": query})
            total = r.get("total")
            items = r.get("items") or []
            if r.get("n") == -1 or total is None:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            return items, total, True
        except Exception as e:
            if attempt < retries - 1:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            print(f"[{query}] fetch 异常: {str(e)[:60]}", flush=True)
    return [], 0, False


def slim(rec):
    """裁剪 MCP 记录为需要的字段"""
    return {
        "id": rec.get("Publisher") or ((rec.get("Path") or "") + "/" + (rec.get("Name") or "")).strip("/"),
        "name": rec.get("Name"),
        "chinese_name": rec.get("ChineseName"),
        "description": rec.get("AbstractCN") or rec.get("Abstract"),
        "category": rec.get("Category"),
        "tags": rec.get("Tags"),
        "stars": rec.get("Stars"),
        "view_count": rec.get("ViewCount"),
        "call_volume": rec.get("CallVolume"),
        "tools": rec.get("Tools"),
        "verified": rec.get("Verifed"),
        "hosted": rec.get("Hosted"),
        "from_site": rec.get("FromSite"),
        "from_site_url": rec.get("FromSiteUrl"),
        "license": rec.get("License"),
        "created_at": rec.get("GmtCreated"),
        "updated_at": rec.get("GmtUpdated"),
    }


async def new_session(browser):
    """创建一个新浏览器上下文并过 WAF。返回 page 或 None。"""
    for _ in range(3):
        try:
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            )
            page = await ctx.new_page()
            await page.goto("https://www.modelscope.cn/mcp", wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(6)
            _, total, ok = await fetch_query(page, "a")
            if ok:
                print("  新会话 OK，接口可用", flush=True)
                return page
            await ctx.close()
        except Exception as e:
            print(f"  建会话异常: {str(e)[:60]}", flush=True)
        await asyncio.sleep(10)
    return None


async def crawl_all(browser, page, out_f, done_terms, max_terms=0):
    """BFS 前缀切片主循环。连续失败重建会话。返回 (written, ok)。"""
    queue = [c for c in ALPHABET if c not in done_terms]
    consecutive_errors = 0
    terms_done = 0
    written = 0

    while queue:
        term = queue.pop(0)
        items, total, ok = await fetch_query(page, term)
        if not ok:
            consecutive_errors += 1
            print(f"[{term}] 请求失败（连续 {consecutive_errors}）", flush=True)
            queue.append(term)
            if consecutive_errors >= RECONNECT_AFTER:
                print("=== 连续失败，重建会话 ===", flush=True)
                newp = await new_session(browser)
                if newp is None:
                    print("会话重建失败，中止。", flush=True)
                    return written, False
                try:
                    await page.context.close()
                except Exception:
                    pass
                page = newp
                consecutive_errors = 0
            await asyncio.sleep(3)
            continue

        if total == 0:
            done_terms.add(term)
            continue

        if total > WINDOW:
            if len(term) >= MAX_DEPTH:
                print(f"[{term}] total={total}>窗口但已达最大深度，取 {len(items)} 条", flush=True)
            else:
                children = [term + c for c in ALPHABET if term + c not in done_terms]
                queue = children + queue
                if len(term) <= 3:
                    print(f"[{term}] total={total} > {WINDOW}，细分为 {len(children)} 个子词", flush=True)
                done_terms.add(term)
                continue

        for it in items:
            rec = slim(it)
            rec["_query"] = term
            out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1
        out_f.flush()

        consecutive_errors = 0
        done_terms.add(term)
        terms_done += 1
        if terms_done % 50 == 0:
            with open(STATE_FILE, "w", encoding="utf-8") as sf:
                json.dump(sorted(done_terms), sf)
            print(f"=== 完成 {terms_done} 词，本轮 {written} 条，队列剩 {len(queue)} ===", flush=True)
        if max_terms and terms_done >= max_terms:
            print(f"达到 MS_MCP_MAX_TERMS={max_terms}，提前结束（调试模式）", flush=True)
            break
        await asyncio.sleep(DELAY)

    return written, True


async def main():
    done_terms = set()
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            done_terms = set(json.load(f))
    print(f"已完成词 {len(done_terms)}", flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await new_session(browser)
        if page is None:
            print("初始会话建立失败。", flush=True)
            await browser.close()
            sys.exit(3)
        print(f"接口可用。开始 BFS 爬取（队列 {36 - len(done_terms)} 个单字符词）", flush=True)

        written = 0
        with open(OUT_JSONL, "a", encoding="utf-8") as out_f:
            written, ok = await crawl_all(browser, page, out_f, done_terms)
        try:
            await browser.close()
        except Exception:
            pass

    with open(STATE_FILE, "w", encoding="utf-8") as sf:
        json.dump(sorted(done_terms), sf)

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
    if not ok:
        sys.exit(3)


if __name__ == "__main__":
    asyncio.run(main())
