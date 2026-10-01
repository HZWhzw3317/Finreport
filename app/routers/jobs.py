"""后台任务接口（下载 / 解析）。

前缀：``/api/jobs``

任务通过 FastAPI 的 ``BackgroundTasks`` 在响应返回后异步执行，
执行进度写入 ``JobLog`` 表，客户端轮询 ``GET /api/jobs/{id}`` 获取状态。
小规模场景下足够；如需横向扩展可替换为 Celery/Redis。
"""

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.enums import JobStatus
from app.models import JobLog, Report
from app.schemas import DownloadJobIn, JobOut, ParseJobIn
from app.services import pipeline

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


async def _resolve_reports(company_codes, report_types, years) -> list[Report]:
    """按条件筛选报告，供下载/解析任务复用。"""
    query = Report.all()
    if company_codes:
        query = query.filter(company__stock_code__in=company_codes)
    if report_types:
        query = query.filter(report_type__in=report_types)
    if years:
        query = query.filter(report_year__in=years)
    return await query


async def _run_download(job_id: int, payload: DownloadJobIn) -> None:
    """下载任务的执行体（在后台任务中运行）。"""
    job = await JobLog.get(id=job_id)
    job.status = JobStatus.running.value
    job.started_at = datetime.utcnow()
    await job.save()
    try:
        reports = await _resolve_reports(payload.company_codes, payload.report_types, payload.years)
        job.total = len(reports)
        await job.save()
        # 真正的下载逻辑在 pipeline 中，逐份下载并记录本地路径/哈希
        stats = await pipeline.download_reports([r.id for r in reports])
        job.success = stats["success"]
        job.failed = stats["failed"]
        job.status = JobStatus.done.value
    except Exception as exc:  # noqa: BLE001 - 汇总异常到任务日志
        job.status = JobStatus.failed.value
        job.message = str(exc)[:2000]
    finally:
        job.finished_at = datetime.utcnow()
        await job.save()


async def _run_parse(job_id: int, payload: ParseJobIn) -> None:
    """解析任务的执行体（在后台任务中运行）。"""
    job = await JobLog.get(id=job_id)
    job.status = JobStatus.running.value
    job.started_at = datetime.utcnow()
    await job.save()
    try:
        # 优先按报告ID精确解析，否则按条件筛选
        if payload.report_ids:
            reports = await Report.filter(id__in=payload.report_ids)
        else:
            reports = await _resolve_reports(
                payload.company_codes, payload.report_types, payload.years
            )
        job.total = len(reports)
        await job.save()
        for report in reports:
            # only_missing=True 时跳过已成功解析的报告
            if payload.only_missing and report.parse_status == "done":
                job.success += 1
                continue
            try:
                await pipeline.parse_report(report, force_llm=payload.force_llm)
                job.success += 1
            except Exception as exc:  # noqa: BLE001 - 单份失败不影响其余
                job.failed += 1
                job.message = f"{job.message or ''}\nreport {report.id}: {exc}"[:2000]
            await job.save()  # 每份处理完即持久化进度
        job.status = JobStatus.done.value
    except Exception as exc:  # noqa: BLE001
        job.status = JobStatus.failed.value
        job.message = str(exc)[:2000]
    finally:
        job.finished_at = datetime.utcnow()
        await job.save()


@router.post("/download", response_model=JobOut)
async def create_download_job(payload: DownloadJobIn, background: BackgroundTasks):
    """创建下载任务，立即返回任务记录，下载在后台执行。"""
    job = await JobLog.create(task_type="download", target=str(payload.model_dump()))
    background.add_task(_run_download, job.id, payload)
    return job


@router.post("/parse", response_model=JobOut)
async def create_parse_job(payload: ParseJobIn, background: BackgroundTasks):
    """创建解析任务，立即返回任务记录，解析在后台执行。"""
    job = await JobLog.create(task_type="parse", target=str(payload.model_dump()))
    background.add_task(_run_parse, job.id, payload)
    return job


@router.get("", response_model=list[JobOut])
async def list_jobs(limit: int = 50):
    """查询最近的任务列表。"""
    return await JobLog.all().limit(limit)


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: int):
    """查询单个任务的状态与进度。"""
    job = await JobLog.get_or_none(id=job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job
