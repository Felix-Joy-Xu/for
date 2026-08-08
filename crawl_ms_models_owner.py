# -*- coding: utf-8 -*-
"""魔搭全量模型爬虫 - Owner 切分版。

背景：
- BFS 前缀切片（crawl_ms_models_full.py）只能覆盖 ~13.7 万模型，
  因为 search 是 contains 匹配，前缀切片存在空洞。
- openapi/v1/models 支持 owner 过滤参数，每个 owner 独立享 3000 窗口。
- 策略：从现有数据提取所有 owner，逐个 owner 完整翻页爬取。
  模型数 >3000 的 owner 用 owner+search 组合切片。

输出: modelscope_output/models_full.jsonl（分片追加）
      modelscope_output/models_full.json（去重合并）
状态: modelscope_output/state_ms_models_owner.json（已完成 owner）
"""
import json
import os
import sys
import time
import threading
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "modelscope_output"
OUTPUT_JSONL = OUT_DIR / "models_full.jsonl"
OUTPUT_JSON = OUT_DIR / "models_full.json"
STATE_FILE = OUT_DIR / "state_ms_models_owner.json"

PART_MAX_SIZE = 50 * 1024 * 1024

API = "https://modelscope.cn/openapi/v1/models"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
WINDOW = 3000
PAGE_SIZE = 50
DELAY = 0.05
ABORT_AFTER = 50
MAX_TERMS = int(os.environ.get("MS_OWNER_MAX", "0") or 0)
BUDGET_MIN = int(os.environ.get("MS_OWNER_BUDGET_MIN", "270") or 0)
WORKERS = int(os.environ.get("MS_OWNER_WORKERS", "3") or 1)
START_TS = time.time()

write_lock = threading.Lock()


def time_up():
    return BUDGET_MIN > 0 and (time.time() - START_TS) > BUDGET_MIN * 60


def get_page(owner, term, page_number, page_size, retries=4):
    params = {"page_number": page_number, "page_size": page_size}
    if owner:
        params["owner"] = owner
    if term:
        params["search"] = term
    for attempt in range(retries):
        try:
            r = requests.get(API, params=params, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                j = r.json()
                if j.get("success"):
                    return j.get("data") or {}, True
                code = j.get("code", "")
                if "QuotaLimitExceed" in str(code):
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


def fetch_owner(owner):
    """翻页取完一个 owner 的全部模型（owner 内 search 切片处理 >3000）。
    返回 (owner, items, total, ok)。"""
    items = []
    # 先计数
    d0, ok0 = get_page(owner, None, 1, 1)
    if not ok0:
        return owner, [], None, False
    total = d0.get("total_count") or 0
    if total == 0:
        return owner, [], 0, True

    if total <= WINDOW:
        # 直接翻页取完
        pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        for p in range(1, pages + 1):
            if time_up():
                break
            d, ok = get_page(owner, None, p, PAGE_SIZE)
            if not ok:
                return owner, items, total, False
            if d.get("_quota"):
                break
            batch = d.get("models") or []
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            time.sleep(DELAY)
        return owner, items, total, True
    else:
        # >3000：owner+search 前缀切片
        queue = [c for c in ALPHABET]
        seen_sub = set()
        while queue:
            if time_up():
                break
            term = queue.pop(0)
            if term in seen_sub:
                continue
            d1, ok1 = get_page(owner, term, 1, 1)
            if not ok1:
                time.sleep(3)
                continue
            t1 = d1.get("total_count") or 0
            if t1 == 0:
                seen_sub.add(term)
                continue
            if t1 > WINDOW:
                if len(term) >= 6:
                    # 尽力取 3000
                    for p in range(1, (WINDOW // PAGE_SIZE) + 1):
                        d, ok = get_page(owner, term, p, PAGE_SIZE)
                        if not ok or d.get("_quota"):
                            break
                        batch = d.get("models") or []
                        items.extend(batch)
                        if len(batch) < PAGE_SIZE:
                            break
                        time.sleep(DELAY)
                    seen_sub.add(term)
                else:
                    children = [term + c for c in ALPHABET if term + c not in seen_sub]
                    queue = children + queue
                    seen_sub.add(term)
                continue
            # t1 <= 3000，翻页取完
            pages = (t1 + PAGE_SIZE - 1) // PAGE_SIZE
            for p in range(1, pages + 1):
                d, ok = get_page(owner, term, p, PAGE_SIZE)
                if not ok or d.get("_quota"):
                    break
                batch = d.get("models") or []
                items.extend(batch)
                if len(batch) < PAGE_SIZE:
                    break
                time.sleep(DELAY)
            seen_sub.add(term)
        return owner, items, total, True


def get_active_part_path(base_path, max_size=PART_MAX_SIZE):
    base, ext = os.path.splitext(base_path)
    part_num = 1
    while True:
        part_path = f"{base}_part{part_num}{ext}"
        if not os.path.exists(part_path):
            break
        part_num += 1
    if part_num == 1:
        if os.path.exists(base_path):
            if os.path.getsize(base_path) >= max_size:
                part1_path = f"{base}_part1{ext}"
                try:
                    os.rename(base_path, part1_path)
                    return f"{base}_part2{ext}"
                except Exception:
                    return part1_path
            return base_path
        return base_path
    else:
        latest = f"{base}_part{part_num - 1}{ext}"
        if os.path.getsize(latest) >= max_size:
            return f"{base}_part{part_num}{ext}"
        return latest


def all_jsonl_parts(base_path):
    base, ext = os.path.splitext(base_path)
    paths = []
    if os.path.exists(base_path):
        paths.append(base_path)
    part_num = 1
    while True:
        part_path = f"{base}_part{part_num}{ext}"
        if not os.path.exists(part_path):
            break
        paths.append(part_path)
        part_num += 1
    return paths


def write_batch(owner, items):
    if not items:
        return 0
    with write_lock:
        active = get_active_part_path(str(OUTPUT_JSONL))
        with open(active, "a", encoding="utf-8") as f:
            for it in items:
                it["_owner"] = owner
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
    return len(items)


def extract_owners():
    """从现有分片 jsonl 提取所有 owner"""
    owners = set()
    for part in all_jsonl_parts(str(OUTPUT_JSONL)):
        try:
            with open(part, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        it = json.loads(line)
                        mid = it.get("id", "")
                        if "/" in mid:
                            owners.add(mid.split("/", 1)[0])
                        o = it.get("_owner")
                        if o:
                            owners.add(o)
                    except Exception:
                        pass
        except Exception:
            pass
    return sorted(owners)


def main():
    OUTPUT_JSONL.parent.mkdir(exist_ok=True)
    done_owners = set()
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            done_owners = set(json.load(f))

    owners = extract_owners()
    print(f"提取到 {len(owners)} 个 owner，已完成 {len(done_owners)}", flush=True)
    todo = [o for o in owners if o not in done_owners]
    if MAX_TERMS > 0:
        todo = todo[:MAX_TERMS]
    print(f"待爬 {len(todo)} 个 owner，并发 {WORKERS}", flush=True)

    consecutive_errors = 0
    written = 0

    while todo:
        if time_up():
            print(f"时间预算用尽，保存断点。", flush=True)
            break
        batch = []
        with write_lock:
            while todo and len(batch) < WORKERS * 2:
                batch.append(todo.pop(0))

        futures = {}
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            for o in batch:
                futures[executor.submit(fetch_owner, o)] = o
            for fut in as_completed(futures):
                if time_up():
                    break
                o = futures[fut]
                try:
                    owner, items, total, ok = fut.result()
                except Exception as e:
                    print(f"[{o}] 异常: {str(e)[:60]}", flush=True)
                    ok = False
                    items = []
                if not ok:
                    consecutive_errors += 1
                    with write_lock:
                        todo.append(o)
                    if consecutive_errors >= ABORT_AFTER:
                        print("连续失败过多，中止。", flush=True)
                        sys.exit(3)
                    time.sleep(3)
                    continue
                consecutive_errors = 0
                n = write_batch(owner, items)
                written += n
                with write_lock:
                    done_owners.add(owner)
                if len(done_owners) % 100 == 0:
                    with write_lock:
                        with open(STATE_FILE, "w", encoding="utf-8") as sf:
                            json.dump(sorted(done_owners), sf)
                    print(f"=== 完成 {len(done_owners)} owner，本轮 +{written} 条 ===", flush=True)
                time.sleep(DELAY)

    with write_lock:
        with open(STATE_FILE, "w", encoding="utf-8") as sf:
            json.dump(sorted(done_owners), sf)

    # 去重合并
    models = {}
    for part in all_jsonl_parts(str(OUTPUT_JSONL)):
        with open(part, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    it = json.loads(line)
                    mid = it.get("id")
                    if mid:
                        models[mid] = it
                except Exception:
                    pass
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(list(models.values()), f, ensure_ascii=False)
    print(f"完成。去重后模型 {len(models)} 个 → {OUTPUT_JSON}", flush=True)

    # 写进度供自动续跑判断
    try:
        prog_path = BASE_DIR / "progress_models.json"
        prog = {}
        if prog_path.exists():
            with open(prog_path, "r", encoding="utf-8") as f:
                prog = json.load(f)
        prog["models"] = len(models)
        prog["deltaThisRun"] = written
        prog["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(prog_path, "w", encoding="utf-8") as f:
            json.dump(prog, f, ensure_ascii=False, indent=1)
        print(f"progress_models.json 已更新: models={len(models)}, deltaThisRun={written}", flush=True)
    except Exception as e:
        print(f"progress 更新失败: {e}", flush=True)


if __name__ == "__main__":
    main()
