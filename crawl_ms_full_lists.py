# -*- coding: utf-8 -*-
"""魔搭全量清单爬虫 - 多板块 OpenAPI 前缀切片版（datasets / skills / studios）。

背景与策略与 crawl_ms_models_full.py 相同：
- openapi 每个查询有 page_number × page_size ≤ 3000 的深度上限；
- search 参数按名称前缀匹配（大小写不敏感，亦命中显示名/描述，为超集），
  每个搜索词独立享有 3000 窗口；
- BFS 前缀切片：结果数 > 3000 的词递归追加一个字符细分，按 id 去重。

板块差异（实测）：
- datasets: openapi/v1/datasets，列表键 datasets，总数键 total_count
- skills:   openapi/v1/skills，  列表键 skills，  总数键 total（不是 total_count！）
- studios:  openapi/v1/studios， 列表键 studios， 总数键 total_count

输出: modelscope_output/{kind}_full.jsonl（逐页追加，含 _search_term）
      modelscope_output/{kind}_full.json（结束时按 id/name 去重合并）
状态: modelscope_output/state_ms_{kind}_full.json（已完成的词，断点续爬）

环境变量：
- MS_FULL_SECTIONS=datasets,skills,studios 限定本轮板块（默认全部）
- MS_FULL_MAX_TERMS=N 限制每个板块本轮处理的叶子词数量（调试用）
- MS_FULL_BUDGET_MIN=N 本轮时间预算（分钟，默认 225）。预算用尽时主动
  保存断点并正常退出——与 MCP 爬虫共享 300 分钟 job 超时，给 Playwright
  安装与 MCP 采集留出余量，同时保证 job 成功结束、缓存得以保存。
"""
import json
import os
import sys
import time
import requests
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "modelscope_output"

SECTIONS = {
    "datasets": {
        "api": "https://modelscope.cn/openapi/v1/datasets",
        "items_key": "datasets",
        "id_key": "id",
    },
    "skills": {
        "api": "https://modelscope.cn/openapi/v1/skills",
        "items_key": "skills",
        "id_key": "id",
    },
    "studios": {
        "api": "https://modelscope.cn/openapi/v1/studios",
        "items_key": "studios",
        "id_key": "id",
    },
}

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
WINDOW = 3000          # page_number × page_size 上限
PAGE_SIZE = 50
DELAY = 0.15
ABORT_AFTER = 50
MAX_DEPTH = 6
MAX_TERMS = int(os.environ.get("MS_FULL_MAX_TERMS", "0") or 0)
ONLY = [s.strip() for s in os.environ.get(
    "MS_FULL_SECTIONS", "datasets,skills,studios").split(",") if s.strip()]
BUDGET_MIN = int(os.environ.get("MS_FULL_BUDGET_MIN", "225") or 0)
START_TS = time.time()


def time_up():
    """时间预算是否用尽。预算到点主动收尾，避免 job 超时取消导致缓存丢失。"""
    return BUDGET_MIN > 0 and (time.time() - START_TS) > BUDGET_MIN * 60


def total_from(d):
    """兼容 total_count / total 两种总数键。"""
    return d.get("total_count") or d.get("total") or 0


def get_page(api, term, page_number, page_size, retries=4):
    """请求一页。返回 (data_dict, ok)。ok=False 为网络/限流类失败。"""
    params = {"page_number": page_number, "page_size": page_size}
    if term:
        params["search"] = term
    for attempt in range(retries):
        try:
            r = requests.get(api, params=params, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                j = r.json()
                if j.get("success"):
                    return j.get("data") or {}, True
                code = str(j.get("code", ""))
                if "QuotaLimitExceed" in code:
                    return {"_quota": True}, True
                return {}, True
            if r.status_code in (429, 403):
                time.sleep(2 ** attempt * 2)
                continue
            if attempt < retries - 1:
                time.sleep(1 + attempt)
                continue
            return {}, False
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return {}, False
    return {}, False


def total_of(api, term):
    d, ok = get_page(api, term, 1, 1)
    if not ok:
        return None
    return total_from(d)


def crawl_term(kind, cfg, term, total, out_f):
    """翻页取完一个词的全部结果。返回 (新增写入数, ok)。"""
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    written = 0
    for p in range(1, pages + 1):
        if time_up():
            print(f"  [{kind}/{term}] 时间预算用尽，中止该词（已写 {written}）", flush=True)
            return written, False
        d, ok = get_page(cfg["api"], term, p, PAGE_SIZE)
        if not ok:
            return written, False
        if d.get("_quota"):
            print(f"  [{kind}/{term}] p{p} 触发配额，中止该词", flush=True)
            return written, False
        items = d.get(cfg["items_key"]) or []
        for it in items:
            it["_search_term"] = term
            out_f.write(json.dumps(it, ensure_ascii=False) + "\n")
            written += 1
        out_f.flush()
        if len(items) < PAGE_SIZE:
            break
        time.sleep(DELAY)
    return written, True


def crawl_section(kind):
    cfg = SECTIONS[kind]
    out_jsonl = OUT_DIR / f"{kind}_full.jsonl"
    out_json = OUT_DIR / f"{kind}_full.json"
    state_file = OUT_DIR / f"state_ms_{kind}_full.json"

    done_terms = set()
    if state_file.exists():
        with open(state_file, "r", encoding="utf-8") as f:
            done_terms = set(json.load(f))

    queue = [c for c in ALPHABET if c not in done_terms]
    print(f"[{kind}] 待探测词 {len(queue)}，已完成词 {len(done_terms)}", flush=True)

    consecutive_errors = 0
    terms_done_this_run = 0
    items_written = 0

    with open(out_jsonl, "a", encoding="utf-8") as out_f:
        while queue:
            if time_up():
                print(f"[{kind}] 时间预算 {BUDGET_MIN} 分钟用尽，主动收尾保存断点。", flush=True)
                break
            term = queue.pop(0)
            total = total_of(cfg["api"], term)
            if total is None:
                consecutive_errors += 1
                print(f"[{kind}/{term}] 计数失败（连续 {consecutive_errors}）", flush=True)
                queue.append(term)
                if consecutive_errors >= ABORT_AFTER:
                    print(f"[{kind}] 连续失败过多，中止。", flush=True)
                    return False
                time.sleep(5)
                continue

            if total == 0:
                done_terms.add(term)
                continue

            if total > WINDOW:
                if len(term) >= MAX_DEPTH:
                    print(f"[{kind}/{term}] total={total} 超窗口但已达最大深度，尽力翻页", flush=True)
                else:
                    children = [term + c for c in ALPHABET if term + c not in done_terms]
                    queue = children + queue
                    print(f"[{kind}/{term}] total={total} > {WINDOW}，细分为 {len(children)} 个子词", flush=True)
                    done_terms.add(term)
                    continue

            print(f"[{kind}/{term}] total={total}，开始翻页", flush=True)
            written, ok = crawl_term(kind, cfg, term, total, out_f)
            items_written += written
            if not ok:
                consecutive_errors += 1
                print(f"[{kind}/{term}] 翻页中断（已写 {written}），稍后重试", flush=True)
                queue.append(term)
                if consecutive_errors >= ABORT_AFTER:
                    print(f"[{kind}] 连续失败过多，中止。", flush=True)
                    return False
                time.sleep(5)
                continue

            consecutive_errors = 0
            done_terms.add(term)
            terms_done_this_run += 1
            if terms_done_this_run % 20 == 0:
                with open(state_file, "w", encoding="utf-8") as sf:
                    json.dump(sorted(done_terms), sf)
                print(f"=== [{kind}] 本轮完成 {terms_done_this_run} 词，累计 {items_written} 条，队列剩 {len(queue)} ===", flush=True)
            if MAX_TERMS and terms_done_this_run >= MAX_TERMS:
                print(f"[{kind}] 达到 MS_FULL_MAX_TERMS={MAX_TERMS}，提前结束（调试模式）", flush=True)
                break
            time.sleep(DELAY)

    with open(state_file, "w", encoding="utf-8") as sf:
        json.dump(sorted(done_terms), sf)

    # 去重合并
    items = {}
    id_key = cfg["id_key"]
    if out_jsonl.exists():
        with open(out_jsonl, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    it = json.loads(line)
                except Exception:
                    continue
                k = it.get(id_key) or it.get("name")
                if k is not None:
                    items[k] = it
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(list(items.values()), f, ensure_ascii=False)
    print(f"[{kind}] 完成。去重后 {len(items)} 个 → {out_json}", flush=True)
    return True


def main():
    OUT_DIR.mkdir(exist_ok=True)
    ok_all = True
    for kind in ONLY:
        if kind not in SECTIONS:
            print(f"未知板块 {kind}，跳过（可选：{list(SECTIONS)}）", flush=True)
            continue
        if time_up():
            print(f"时间预算用尽，{kind} 板块留待下轮续爬。", flush=True)
            break
        print(f"########## 板块 {kind} ##########", flush=True)
        if not crawl_section(kind):
            ok_all = False
            break
    if not ok_all:
        sys.exit(3)


if __name__ == "__main__":
    main()
