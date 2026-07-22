#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补充 HF 治理文本（从 hf-mirror.com 抓取清单所列全部文档）"""
import os
import json
import requests
import time
from datetime import datetime, timezone
from bs4 import BeautifulSoup

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "modelscope_output")
HF_DIR = os.path.join(OUTPUT_DIR, "hf_governance")
os.makedirs(HF_DIR, exist_ok=True)

# HF 治理文档清单（与魔搭 governance_*.txt 编码框架对齐）
# 注意：/terms 与 /dmca 在 HF 上会被解析为用户名（Terms、DMCA 用户），因此使用正确的官方路径。
# 先尝试 hf-mirror.com；若被返回跳转页，则回退到 huggingface.co 原站。
HF_TARGETS = [
    ("terms_of_service", "https://hf-mirror.com/terms-of-service", "Terms of Service"),
    ("privacy_policy", "https://hf-mirror.com/privacy", "Privacy Policy"),
    ("acceptable_use", "https://hf-mirror.com/content-policy", "Acceptable Use Policy"),
    ("community_guidelines", "https://hf-mirror.com/code-of-conduct", "Code of Conduct / Community Guidelines"),
    # HF 没有独立的公开 DMCA 页面，相关条款在 Terms of Service 中
    ("dmca_takedown", "https://hf-mirror.com/terms-of-service", "DMCA / Takedown Policy (Terms of Service)"),
    ("model_card_guide", "https://hf-mirror.com/docs/hub/model-cards", "Model Card Guide"),
    ("gated_model_access", "https://hf-mirror.com/docs/hub/models-gated", "Gated Model Access Policy"),
    ("dataset_governance", "https://hf-mirror.com/docs/hub/datasets-gated", "Dataset Governance Terms"),
    ("safety_scanning", "https://hf-mirror.com/docs/hub/security-pickle", "Safety Scanning Policy (Pickle)"),
    ("malware_scanning", "https://hf-mirror.com/docs/hub/security-malware", "Safety Scanning Policy (Malware)"),
]


def is_redirect_page(html: str) -> bool:
    """判断 hf-mirror 是否返回了‘正在前往 Hugging Face 原站’的跳转页。"""
    return "正在前往 Hugging Face 原站" in html or "检测到您的 IP" in html


def fetch_governance(url: str, timeout: int = 30) -> requests.Response:
    """抓取治理文档；若遇到 mirror 跳转页，则回退到 huggingface.co 原站。"""
    r = session.get(url, timeout=timeout)
    if r.status_code == 200 and is_redirect_page(r.text):
        # 构造 huggingface.co 原站 URL
        parsed = requests.utils.urlparse(url)
        origin_url = f"https://huggingface.co{parsed.path}"
        if parsed.query:
            origin_url += f"?{parsed.query}"
        print(f"  [mirror redirect] fallback to {origin_url}")
        r = session.get(origin_url, timeout=timeout)
        if r.status_code == 200:
            r._origin_url = origin_url
    return r

METADATA_FILE = os.path.join(HF_DIR, "governance_metadata.json")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml",
})

metadata = []
for name, url, label in HF_TARGETS:
    print(f"\n--- {label} ---")
    try:
        r = fetch_governance(url, timeout=30)
        actual_url = getattr(r, "_origin_url", None) or url
        print(f"  status: {r.status_code}, bytes: {len(r.text)}, url: {actual_url}")
        if r.status_code != 200 or len(r.text) < 500:
            continue

        # 优先提取正文
        try:
            soup = BeautifulSoup(r.text, "lxml")
            # 去掉头尾的 nav/footer
            for tag in soup.select("script, style, nav, footer, header, aside"):
                tag.decompose()
            main = (soup.find("main") or soup.select_one("[class*='content']") 
                    or soup.find("body"))
            text = main.get_text(separator="\n", strip=True) if main else soup.get_text(separator="\n", strip=True)
            # 清理空行
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            text = "\n".join(lines)
        except:
            text = r.text

        path = os.path.join(HF_DIR, f"hf_{name}.txt")
        clean_path = os.path.join(HF_DIR, f"hf_{name}_clean.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {label}\n# Source: {actual_url}\n# CrawledAt: {datetime.now(timezone.utc).isoformat()}\n# Length: {len(text)} chars\n{'='*80}\n\n{r.text}")
        with open(clean_path, "w", encoding="utf-8") as f:
            f.write(f"# {label}\n# Source: {actual_url}\n# CrawledAt: {datetime.now(timezone.utc).isoformat()}\n# Length: {len(text)} chars\n{'='*80}\n\n{text}")
        print(f"  Saved: hf_{name}.txt ({len(r.text)} bytes) / clean ({len(text)} chars)")
        metadata.append({
            "name": name,
            "label": label,
            "url": actual_url,
            "crawled_at": datetime.now(timezone.utc).isoformat(),
            "raw_size": len(r.text),
            "clean_size": len(text),
            "status": "success",
        })
        # 预览
        for line in lines[:3]:
            print(f"    | {line[:80]}")
    except Exception as e:
        print(f"  ERR: {str(e)[:80]}")
        metadata.append({
            "name": name,
            "label": label,
            "url": url,
            "crawled_at": datetime.now(timezone.utc).isoformat(),
            "status": "error",
            "error": str(e)[:200],
        })
    time.sleep(0.5)

# 保存元数据
with open(METADATA_FILE, "w", encoding="utf-8") as f:
    json.dump(metadata, f, ensure_ascii=False, indent=2)

# 汇总
print(f"\n{'='*60}")
print(f"HF 治理文本完成")
print(f"{'='*60}")
for fn in sorted(os.listdir(HF_DIR)):
    p = os.path.join(HF_DIR, fn)
    if os.path.isfile(p):
        sz = os.path.getsize(p)
        print(f"  {fn:40s} {sz:>6,} bytes")