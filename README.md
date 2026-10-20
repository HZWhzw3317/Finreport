# finreport

A 股上市公司财报 PDF 批量提取入库系统。数据来自巨潮资讯网，混合「规则解析 + 大模型视觉兜底」抽取三大报表结构化数据，存入 MySQL 并通过 FastAPI 提供查询接口。

## 功能

- 自动拉取巨潮全量股票列表，按股票代码/年份查询年报、半年报、一季报、三季报公告
- 批量下载财报 PDF，限速 + 重试，SHA256 去重
- 定位并解析合并/母公司 **资产负债表、利润表、现金流量表**
- 科目名归一化到标准科目字典，勾稽校验（资产=负债+权益 等）
- 规则解析不达标时，调用 `deepseek-flash`（视觉）渲染页面兜底，合并结果并标注置信度
- 幂等入库，支持 job 管理、待人工复核标记

## 评测结果（2023 年报，50 家）

独立评测库 `finreport_bench` + 独立 PDF 目录 `data/pdfs_bench/`，不写入生产库。
样本为 50 家 A 股（主板 29 / 创业板 11 / 科创板 6 / 北交所 4，含 5 家金融），
固定随机种子，名单见 [`benchmarks/companies_2023.txt`](benchmarks/companies_2023.txt)。

| 指标 | 结果 |
| --- | ---: |
| 年报 / PDF 下载 | 50 份 / 100% |
| 报表 / 科目 | 293 张 / 7,558 条 |
| 规则独立完成率 | **64.16%**（188/293） |
| LLM 兜底参与率 | **35.84%**（105/293） |
| 最终通过率 | **90.00%**（45/50） |
| 平均置信度 | **0.97**（中位数 1.0） |
| 核心科目覆盖率 | **91.13%** |
| 勾稽通过率（BS / IS / CF） | **100% / 100% / 100%** |
| 单份年报平均解析 | 37.3s（复跑，含缓存） |

方法与完整指标见 [`benchmarks/README.md`](benchmarks/README.md) 和
[`benchmarks/report_2023_50.md`](benchmarks/report_2023_50.md)。
5 份报告因特殊版式或数据缺失进入人工复核，被正确标记为 `needs_review`，未误判为通过。

## 环境安装

```bash
conda env create -f environment.yaml
conda activate finreport
cp .env.example .env   # 按需修改数据库与 LLM 配置
```

若 `conda env create` 求解较慢，可先建基础环境再 pip 安装：

```bash
conda create -n finreport python=3.11 pip -y
conda activate finreport
pip install -r requirements.txt
```

## 初始化数据库

```bash
python scripts/create_database.py  # 建库（不存在时，utf8mb4）
python scripts/init_db.py          # 建表
python scripts/seed_accounts.py    # 写入标准科目字典
```

生产建议使用 aerich 迁移：

```bash
aerich init -t app.db.TORTOISE_ORM
aerich init-db
aerich migrate
aerich upgrade
```

## 使用（CLI，端到端）

```bash
# 1. 拉取巨潮全量股票列表（幂等，只需一次）
python scripts/crawl.py --bootstrap

# 2. 同步公告元数据并下载 2023 年报
python scripts/crawl.py --codes 000001,002594,600519 --years 2023 --sync --download

# 3. 解析入库（规则失败自动走大模型兜底）
python scripts/crawl.py --codes 000001,002594,600519 --years 2023 --parse

# 仅解析已下载 PDF
python scripts/parse.py --report-id 12
python scripts/parse.py --codes 000001 --years 2023 --no-llm     # 关闭兜底
python scripts/parse.py --codes 000001 --years 2023 --force-llm  # 强制大模型
```

### 可复现评测

```bash
# 独立库 finreport_bench + data/pdfs_bench，不影响生产库
python scripts/benchmark.py run --limit 50 --year 2023 --financial 5 --recreate

# 只重算指标
python scripts/benchmark.py metrics --year 2023 --limit 50
```

## 启动服务

必须先激活本项目环境（`finreport`），否则 `uvicorn` 可能命中其它 Python 导致 `No module named 'tortoise'`。

```bash
conda activate finreport
uvicorn app.main:app --reload --port 8000
```

若未在 conda 环境内，也可用 `conda run` 或该环境的解释器直接启动：

```bash
# 方式一：conda run（无需手动激活）
conda run -n finreport uvicorn app.main:app --reload --port 8000

# 方式二：直接用该环境的 python（将 <PYTHON> 换成你的环境解释器路径）
<PYTHON> -m uvicorn app.main:app --reload --port 8000
```

### 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/companies` | 公司列表 |
| POST | `/api/companies/bootstrap` | 拉取巨潮全量股票列表 |
| POST | `/api/companies/import` | 按代码导入公司 |
| GET | `/api/reports?company_code=&report_type=&year=` | 报告列表 |
| GET | `/api/reports/{id}` | 报告详情 |
| GET | `/api/reports/{id}/statements?type=bs&scope=consolidated` | 报表列表 |
| GET | `/api/statements/{id}/items` | 报表科目明细 |
| POST | `/api/jobs/download` | 创建下载任务 |
| POST | `/api/jobs/parse` | 创建解析任务 |
| GET | `/api/jobs/{id}` | 任务状态 |

示例：

```bash
curl -X POST http://127.0.0.1:8000/api/jobs/parse \
  -H "Content-Type: application/json" \
  -d '{"company_codes":["000001"],"report_types":["annual"],"years":[2023],"force_llm":false}'
```

## LLM 兜底

触发条件：定位失败 / 条目过少 / 核心科目缺失 / 勾稽校验失败 / 单位异常等（见 `app/services/extractor/validator.py`）。
兜底流程：PyMuPDF 渲染报表页为图片 → 调用 `deepseek-flash` 输出 JSON → 归一化 → 重新校验 → 合并/替换规则结果。

相关配置见 `.env.example`。

## 标准科目

内置覆盖三大报表核心科目，定义在 `app/services/extractor/accounts.py`，可通过 `account_dict` 表扩展同义词。

## 测试

```bash
pytest -q
```

## 目录结构

```
app/
  main.py            FastAPI 入口
  config.py          配置
  db.py              Tortoise 配置
  models.py          ORM 模型
  schemas.py         Pydantic 出入参
  routers/           接口
  services/
    crawler/cninfo.py      巨潮采集
    extractor/             定位/规则解析/归一化/校验/LLM兜底
    pipeline.py            编排
scripts/             初始化、CLI 与评测脚本
data/pdfs/           PDF 归档
benchmarks/         评测样本、指标报告与方法说明
tests/               单元测试
```
