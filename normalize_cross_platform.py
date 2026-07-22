#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
四平台数据归一化
================
在 HF/Gitee 采集完成后运行，统一四平台字段口径：
  - 许可证归一化映射表
  - 时间统一为 ISO 8601 UTC
  - 作者标识统一为 "平台:用户名"
  - 机构账号打标（企业 / 高校 / 个人 / 基金会）

输入：
  - modelscope_output/hf_models_all.csv
  - modelscope_output/models_all.csv
  - modelscope_output/cross_platform_match_full.csv

输出：
  - modelscope_output/*_norm.csv（归一化后的清单）
  - modelscope_output/license_mapping.json
  - modelscope_output/author_entity_tags.json
"""
import argparse
import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

# ============================================================================
# 配置
# ============================================================================
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "modelscope_output"

LICENSE_MAP = {
    # apache-2.0 变体
    "apache-2.0": "apache-2.0",
    "apache license 2.0": "apache-2.0",
    "apache-2.0 license": "apache-2.0",
    "apache 2.0": "apache-2.0",
    "apache software license": "apache-2.0",
    # mit 变体
    "mit": "mit",
    "mit license": "mit",
    # gpl 变体
    "gpl-3.0": "gpl-3.0",
    "gnu general public license v3.0": "gpl-3.0",
    "gpl-2.0": "gpl-2.0",
    "gnu general public license v2.0": "gpl-2.0",
    # bsd
    "bsd-3-clause": "bsd-3-clause",
    "bsd 3-clause": "bsd-3-clause",
    "bsd-2-clause": "bsd-2-clause",
    # creative commons
    "cc-by-4.0": "cc-by-4.0",
    "cc-by-sa-4.0": "cc-by-sa-4.0",
    "cc0-1.0": "cc0-1.0",
    # llama / 特殊商业许可
    "llama3.1": "llama3.1",
    "llama3.2": "llama3.2",
    "llama2": "llama2",
    "openrail": "openrail",
    "bigscience-openrail-m": "openrail",
    # 其他
    "other": "other",
    "unknown": "unknown",
    "": "unknown",
}

# 机构类型关键词（用于 owner/author 打标）
ENTERPRISE_KEYWORDS = [
    "inc", "corp", "ltd", "company", "tech", "ai", "cloud", "group", "network",
    "baidu", "tencent", "alibaba", "huawei", "bytedance", "xiaomi", "ant", "meituan",
    "jd", "netease", "didi", "kuaishou", "oppo", "vivo", "oneplus", "realme",
    "google", "microsoft", "meta", "openai", "anthropic", "amazon", "apple",
    "nvidia", "intel", "salesforce", "ibm", "oracle", "adobe",
]

UNIVERSITY_KEYWORDS = [
    "university", "college", "institute", "school", "academy", "edu",
    "tsinghua", "peking", "fudan", "zhejiang", "sjtu", "ustc", "hust",
    "stanford", "mit", "berkeley", "cmu", "eth", "epfl", "oxford", "cambridge",
]

FOUNDATION_KEYWORDS = [
    "foundation", "openatom", "apache", "linux", "eclipse", "mozilla",
    "openeuler", "openharmony", "opengauss", "openlookeng",
]


# ============================================================================
# 工具函数
# ============================================================================
def normalize_license(raw: str) -> str:
    if not raw:
        return "unknown"
    key = re.sub(r"[-_\s]+", " ", str(raw).lower().strip())
    key = re.sub(r"\s+", " ", key).strip()
    if key in LICENSE_MAP:
        return LICENSE_MAP[key]
    # 尝试去除 "license" 后缀
    key2 = key.replace(" license", "").strip()
    if key2 in LICENSE_MAP:
        return LICENSE_MAP[key2]
    return key if key else "unknown"


def normalize_time(raw: str) -> str:
    """统一转为 ISO 8601 UTC。支持 HF/魔搭/Gitee/GitHub 常见格式。"""
    if not raw:
        return ""
    s = str(raw).strip()
    # 尝试常见格式
    formats = [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            continue
    # 魔搭 Unix 时间戳（秒）
    try:
        ts = int(s)
        if ts > 1e12:
            ts = ts / 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError):
        pass
    return s


def tag_entity(name: str) -> str:
    """对 owner/author 进行机构类型打标。"""
    if not name:
        return "unknown"
    low = str(name).lower()
    for k in FOUNDATION_KEYWORDS:
        if k in low:
            return "foundation"
    for k in UNIVERSITY_KEYWORDS:
        if k in low:
            return "university"
    for k in ENTERPRISE_KEYWORDS:
        if k in low:
            return "enterprise"
    # 个人账号常见特征：短名、无组织词
    if len(low) <= 20 and "-" not in low and "_" not in low and low.isalnum():
        return "individual"
    return "community"  # 默认社区/其他


def build_author_id(platform: str, username: str) -> str:
    return f"{platform}:{username}" if username else ""


# ============================================================================
# 归一化函数
# ============================================================================
def normalize_csv(input_path: Path, output_path: Path, platform: str,
                  license_col: str = "License", time_cols: list = None,
                  owner_col: str = "Owner", author_cols: list = None):
    """对单个 CSV 做归一化。"""
    if not input_path.exists():
        print(f"[warn] {input_path} 不存在，跳过")
        return

    time_cols = time_cols or []
    author_cols = author_cols or []

    with open(input_path, "r", encoding="utf-8-sig", newline="") as f_in, \
         open(output_path, "w", encoding="utf-8-sig", newline="") as f_out:
        reader = csv.DictReader(f_in)
        fieldnames = reader.fieldnames or []
        # 新增归一化字段
        new_fields = [
            f"{license_col}_norm",
            f"{owner_col}_entity_type",
        ] + [f"{c}_norm" for c in time_cols] + [f"{c}_author_id" for c in author_cols]
        writer = csv.DictWriter(f_out, fieldnames=fieldnames + new_fields, extrasaction="ignore")
        writer.writeheader()

        for row in reader:
            # license
            row[f"{license_col}_norm"] = normalize_license(row.get(license_col, ""))
            # time
            for c in time_cols:
                if c in row:
                    row[f"{c}_norm"] = normalize_time(row[c])
            # owner entity type
            if owner_col in row:
                row[f"{owner_col}_entity_type"] = tag_entity(row.get(owner_col, ""))
            # author id
            for c in author_cols:
                if c in row:
                    row[f"{c}_author_id"] = build_author_id(platform, row.get(c, ""))
            writer.writerow(row)

    print(f"[norm] {input_path} -> {output_path}")


def build_license_mapping(input_dir: Path, output_path: Path):
    """扫描 *_norm.csv 中的 License 字段，生成许可证映射表。"""
    mapping = {}
    for csv_path in input_dir.glob("*_norm.csv"):
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                raw = row.get("License") or row.get("license", "")
                norm = row.get("License_norm") or row.get("license_norm", "")
                if raw and norm:
                    key = str(raw).strip().lower()
                    mapping[key] = norm

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)
    print(f"[save] license_mapping: {output_path}")


# ============================================================================
# 主流程
# ============================================================================
def main():
    parser = argparse.ArgumentParser(description="四平台数据归一化")
    parser.add_argument("--skip-license-map", action="store_true", help="不重新生成许可证映射表")
    args = parser.parse_args()

    # HF models
    normalize_csv(
        OUTPUT_DIR / "hf_models_all.csv",
        OUTPUT_DIR / "hf_models_all_norm.csv",
        platform="hf",
        license_col="License",
        time_cols=["CreatedAt", "UpdatedAt"],
        owner_col="Owner",
    )

    # 魔搭 models
    normalize_csv(
        OUTPUT_DIR / "models_all.csv",
        OUTPUT_DIR / "models_all_norm.csv",
        platform="ms",
        license_col="License",
        time_cols=["CreatedAt", "UpdatedAt"],
        owner_col="Owner",
    )

    # 魔搭 datasets
    if (OUTPUT_DIR / "datasets_all.csv").exists():
        normalize_csv(
            OUTPUT_DIR / "datasets_all.csv",
            OUTPUT_DIR / "datasets_all_norm.csv",
            platform="ms",
            license_col="License",
            time_cols=["CreatedAt", "UpdatedAt"],
            owner_col="Owner",
        )

    if not args.skip_license_map:
        build_license_mapping(OUTPUT_DIR, OUTPUT_DIR / "license_mapping.json")

    print("[done] 归一化完成")


if __name__ == "__main__":
    main()
