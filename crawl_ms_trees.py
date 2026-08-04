import os
import json
import time
from modelscope.hub.api import HubApi
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = Path(__file__).resolve().parent
OUT = BASE_DIR / "modelscope_output"
MODELS_FILE = OUT / "models_full.json" if (OUT / "models_full.json").exists() else OUT / "models_all.json"
OUTPUT_FILE = OUT / "ms_model_dependencies.jsonl"
STATE_FILE = OUT / "state_ms_trees.json"

ABORT_AFTER = 30          # 连续网络错误达到此数则中止
SAVE_EVERY = 50           # 每完成 50 个写一次状态文件


def fetch_file_tree(api, model_id):
    """获取模型文件树结构和配置文件"""
    result = {"model_id": model_id, "status": "success", "crawled_at": time.time()}
    try:
        files = api.get_model_files(model_id=model_id, recursive=True) or []
        # SDK 字段为 Path/Size（历史上误用 Name 导致全 null）
        result["files"] = [
            {"Name": f.get("Path") or f.get("Name"), "Size": f.get("Size")}
            for f in files if isinstance(f, dict)
        ]

        # 判断依赖框架（过滤 None，避免 in 抛 TypeError）
        file_names = [f["Name"] for f in result["files"] if f.get("Name")]
        file_names_lower = [n.lower() for n in file_names]
        result["has_requirements"] = any("requirements.txt" == n for n in file_names_lower)
        result["has_pytorch"] = any(n.endswith((".bin", ".pt", ".pth")) for n in file_names_lower)
        result["has_safetensors"] = any(n.endswith(".safetensors") for n in file_names_lower)
        result["has_gguf"] = any(n.endswith(".gguf") for n in file_names_lower)
        result["file_count"] = len(result["files"])
        result["total_size"] = sum(int(f["Size"] or 0) for f in result["files"])
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)[:300]
    return result


def main():
    if not MODELS_FILE.exists():
        print(f"File not found: {MODELS_FILE}", flush=True)
        return

    with open(MODELS_FILE, "r", encoding="utf-8") as f:
        models = json.load(f)

    models.sort(key=lambda x: x.get("Downloads") or x.get("downloads") or 0, reverse=True)

    completed = set()
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            completed = set(json.load(f))

    todo = [m.get("Id") or m.get("id") for m in models
            if (m.get("Id") or m.get("id")) and (m.get("Id") or m.get("id")) not in completed]
    print(f"Total: {len(models)}, Completed: {len(completed)}, Remaining: {len(todo)}", flush=True)

    api = HubApi()
    consecutive_errors = 0
    done_this_run = 0

    with open(OUTPUT_FILE, "a", encoding="utf-8") as out_f:
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_to_model = {executor.submit(fetch_file_tree, api, m_id): m_id for m_id in todo}
            for future in as_completed(future_to_model):
                m_id = future_to_model[future]
                try:
                    res = future.result()
                except Exception as e:
                    res = {"model_id": m_id, "status": "error",
                           "error": str(e)[:300], "crawled_at": time.time()}

                out_f.write(json.dumps(res, ensure_ascii=False) + "\n")
                out_f.flush()

                if res.get("status") == "success":
                    completed.add(m_id)
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
                    if consecutive_errors >= ABORT_AFTER:
                        print("连续错误过多，中止。", flush=True)
                        break

                done_this_run += 1
                if done_this_run % SAVE_EVERY == 0:
                    with open(STATE_FILE, "w", encoding="utf-8") as sf:
                        json.dump(list(completed), sf)
                    print(f"Progress: +{done_this_run} this run, total done {len(completed)}", flush=True)

    with open(STATE_FILE, "w", encoding="utf-8") as sf:
        json.dump(list(completed), sf)
    print(f"Finished. +{done_this_run} this run, total done {len(completed)}", flush=True)


if __name__ == "__main__":
    main()
