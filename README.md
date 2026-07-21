# for-modelscope

多平台数据采集爬虫合集，服务于 **《AI 时代中美开源社区治理比较研究》** 项目。

以魔搭社区（ModelScope）全量采集为主，覆盖 GitHub / HuggingFace 跨平台对照、中文技术社区与招聘市场数据。

## 核心模块：魔搭社区采集

| 脚本 | 功能 | 方式 |
|---|---|---|
| `crawl_phase2.py` / `crawl_phase2_sdk.py` | 全量模型、数据集、机构、任务标签 | API + SDK |
| `crawl_datasets_full.py` / `crawl_datasets_expand.py` / `crawl_datasets_by_org.py` | 数据集列表（全量 / 扩展 / 按机构） | API |
| `crawl_studios.py` | 创空间（Studio） | API |
| `crawl_skills_mcp.py` | Skills 与 MCP 服务 | API |
| `crawl_model_cards.py` | 模型卡片（README） | API |
| `crawl_ms_commits.py` | 模型提交历史（分支 / 版本） | API 直采，5 线程 |
| `crawl_ms_trees.py` | 模型文件树与依赖关系 | API |
| `crawl_ms_network.py` | 用户 / 组织主页资料 | Playwright |
| `crawl_ms_comments_api.py` | **模型评论汇总（当前使用）** | API 直采，5 线程，约 1,400+ 条/分钟 |
| `crawl_ms_comments.py` | 模型评论（旧版，已弃用） | Playwright，约 24 秒/条 |
| `crawl_ms_leaderboard.py` | 排行榜 | API |
| `extract_contributors.py` | 模型贡献者 | API |

跨平台对照：`crawl_hf_*.py`（HuggingFace）、`crawl_gh_*.py`（GitHub 治理与 Issues）。

## 数据产出（`modelscope_output/`，不入库）

| 文件 | 内容 | 规模 |
|---|---|---|
| `models_all.json/csv` | 全量模型元数据 | 63,565 |
| `datasets_all` / `studios_all` / `skills_all` / `mcps_all` | 数据集 / 创空间 / Skills / MCP | 全量 |
| `ms_comments_all.jsonl` | 模型评论汇总（评分、评论数、讨论数、Issue/PR 计数） | 全量进行中 |
| `ms_commit_history.jsonl` | 提交历史 | 63,561 |
| `ms_model_dependencies.jsonl` | 文件树与依赖 | 63,561 |
| `ms_users_profiles.jsonl` | 用户/组织资料 | 504 |
| `contributors_all` / `ms_leaderboards.jsonl` | 贡献者 / 排行榜 | 全量 |
| `state_*.json` | 断点续爬状态（重跑自动跳过已完成项） | — |

## 其他平台

- **GitHub**：`github_crawler.py`、`github_mechanism-*.py`（PR 生命周期 / 贡献者流动 / 问责机制）、`run_github_*.py`（讨论区）
- **中文社区**：`crawl_zhihu*.py`、`crawl_juejin.py`、`crawl_reddit*.py`、`crawl_twitter.py`、`crawl_csdn.py`
- **招聘市场**：`boss_*.py`（Boss 直聘 JD）、`crawler_alibaba*.py`、`crawler_tencent*.py`、`fetch_bytedance_*.py`（校招）
- **子项目**：`crawler-framework/`、`hiring_crawler/`、`bytedance_crawler/`、`github_mechanism_collectors/`、`canvas_autoplay_extension/`

## 运行方式

```bash
# 环境：Python 3.13+，依赖 requests（旧版浏览器爬虫另需 playwright）
pip install requests playwright

# 密钥配置（二选一）
# 1. 在本目录创建 _secrets.py（已被 .gitignore 排除），内容示例：
#    MODELSCOPE_TOKEN = "ms-xxxx"
#    MODELSCOPE_TOKENS = ["ms-xxxx", ...]
#    GITHUB_TOKEN = "ghp_xxxx"
# 2. 或设置同名环境变量（MODELSCOPE_TOKENS 用英文逗号分隔）

# 启动评论爬虫（后台分离进程 + 断点续爬）
python _launch_comments.py

# 其他爬虫直接运行，中断后重跑会自动从断点继续
python crawl_ms_commits.py
```

## 合规说明

- 所有爬虫控制请求频率并带限流退避，仅采集公开页面/API 数据
- 数据仅用于学术研究，请遵守各平台 robots 协议与使用条款
- 密钥与 Cookie 一律通过 `_secrets.py` 或环境变量注入，请勿提交到仓库
