# finreport

A 股上市公司财报 PDF 批量提取入库系统。数据来自巨潮资讯网，混合「规则解析 + 大模型视觉兜底」抽取三大报表结构化数据，存入 MySQL 并通过 FastAPI 提供查询接口。

## 功能

- 自动拉取巨潮全量股票列表，按股票代码/年份查询年报、半年报、一季报、三季报公告
- 批量下载财报 PDF，限速 + 重试，SHA256 去重
- 定位并解析合并/母公司 **资产负债表、利润表、现金流量表**
- 科目名归一化到标准科目字典，勾稽校验（资产=负债+权益 等）
- 规则解析不达标时，调用 `deepseek-flash`（视觉）渲染页面兜底，合并结果并标注置信度
- 幂等入库，支持 job 管理、待人工复核标记

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

## 启动服务

必须先激活本项目环境（`finreport`），否则 `uvicorn` 可能命中其它 Python 导致 `No module named 'tortoise'`。

```bash
conda activate finreport
uvicorn app.main:app --reload --port 8000
```

若未初始化 conda 到当前终端，可直接用该环境的解释器：

```bash
C:\Users\hzw999\anaconda3\envs\finreport\python.exe -m uvicorn app.main:app --reload --port 8000
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
scripts/             初始化与 CLI
data/pdfs/           PDF 归档
tests/               单元测试
```
