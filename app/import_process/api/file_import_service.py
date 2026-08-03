import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List

import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.clients.image_asset_mongo_utils import get_image_asset_tool
from app.clients.minio_utils import get_minio_client
from app.core.logger import logger
from app.import_process.agent.state import get_default_state
from app.observability.langfuse_monitor import flush_langfuse, trace_import
from app.observability.prometheus_metrics import install_prometheus, observe_run
from app.observability.rag_observability import score_import_result
from app.runtime.config import load_runtime_config
from app.runtime.run_store import get_run_store, run_owner
from app.security.auth import Principal, require_role
from app.security.config import load_security_config
from app.security.http import configure_http_security
from app.security.tenancy import safe_upload_filename, tenant_object_prefix
from app.tasks.image_enrichment_worker import start_image_enrichment_worker, stop_image_enrichment_worker
from app.utils.path_util import PROJECT_ROOT
from app.utils.task_utils import (
    add_done_task,
    add_running_task,
    get_done_task_list,
    get_running_task_list,
    get_task_status,
    update_task_status,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """
    管理导入服务生命周期。

    服务启动后自动创建图片视觉增强后台线程，持续消费MongoDB中的pending图片任务；
    服务停止时先通知线程退出，再刷新Langfuse缓存，避免容器重启时丢失最后一批任务日志和追踪数据。
    """
    logger.info("文件导入服务启动，准备启动图片视觉增强后台任务")
    start_image_enrichment_worker()
    try:
        yield
    finally:
        logger.info("文件导入服务正在停止，准备关闭图片视觉增强后台任务")
        stop_image_enrichment_worker()
        flush_langfuse()


# 初始化FastAPI应用实例。标题和描述会显示在Swagger文档中。
app = FastAPI(
    title="File Import Service",
    description="Web service for uploading files to Knowledge Base (PDF/MD → 解析 → 切分 → 向量化 → Milvus/KG入库)",
    lifespan=lifespan,
)

configure_http_security(app)
# Prometheus只采集数值指标，不会采集上传文件内容。启动后访问 /metrics 可查看原始指标。
install_prometheus(app, "import-api")


@app.get("/health", tags=["system"])
async def health():
    """容器编排与负载均衡器使用的存活探针。"""
    return {"status": "ok", "service": "import-api"}


@app.get("/import.html", response_class=FileResponse)
async def get_import_page():
    """返回文件导入前端页面。"""
    html_abs_path = PROJECT_ROOT / "app/import_process/page/import.html"
    logger.info(f"前端页面访问，文件绝对路径：{html_abs_path}")

    if not os.path.exists(html_abs_path):
        logger.error(f"前端页面文件不存在，路径：{html_abs_path}")
        raise HTTPException(status_code=404, detail="import.html page not found")

    return FileResponse(path=html_abs_path, media_type="text/html")


def run_graph_task(
    task_id: str,
    local_dir: str,
    local_file_path: str,
    resume: bool = False,
    tenant_id: str = "local",
):
    """
    执行单个文件的LangGraph知识库导入流程。

    主流程只负责解析、图片资产保存、文档切片、向量化和Milvus入库；
    图片视觉描述由常驻后台任务异步处理，不再阻塞该导入任务完成。

    :param task_id: 单个文件的全流程任务编号，同时作为图片资产的document_id。
    :param local_dir: 当前任务的本地工作目录。
    :param local_file_path: 上传文件的本地绝对路径。
    :param resume: 是否从LangGraph检查点恢复。
    :param tenant_id: 当前任务所属租户编号。
    """
    run_started = time.perf_counter()
    runtime_config = load_runtime_config()
    run_store = get_run_store()
    run_store.create(
        task_id,
        "import",
        {"local_dir": local_dir, "local_file_path": local_file_path},
        max_attempts=runtime_config.max_attempts,
        tenant_id=tenant_id,
    )
    owner = run_owner()
    run_store.claim(task_id, owner, runtime_config.lease_seconds)

    try:
        from app.import_process.agent.main_graph import kb_import_app

        update_task_status(task_id, "processing")
        logger.info(f"[{task_id}] 开始执行LangGraph全流程，本地文件路径：{local_file_path}")

        init_state = get_default_state()
        init_state["task_id"] = task_id
        init_state["tenant_id"] = tenant_id
        init_state["local_dir"] = local_dir
        init_state["local_file_path"] = local_file_path

        graph_input = None if resume else init_state
        graph_config = {
            "configurable": {"thread_id": task_id},
            "metadata": {"task_id": task_id, "kind": "import", "tenant_id": tenant_id},
        }
        with trace_import(task_id, tenant_id, os.path.basename(local_file_path)) as (observation, handler):
            if handler is not None:
                graph_config["callbacks"] = [handler]

            for event in kb_import_app.stream(graph_input, config=graph_config):
                run_store.heartbeat(task_id, owner, runtime_config.lease_seconds)
                for node_name in event:
                    logger.info(f"[{task_id}] LangGraph节点执行完成：{node_name}")
                    add_done_task(task_id, node_name)

            final_state = dict(kb_import_app.get_state(graph_config).values or {})
            quality_report = score_import_result(final_state)
            if observation is not None:
                observation.update(
                    output={
                        "status": "completed",
                        "quality_proxy_score": quality_report["quality_proxy_score"],
                        "parser": quality_report["parser"],
                        "chunks": quality_report["chunks"],
                        "embeddings": quality_report["embeddings"],
                        "storage": quality_report["storage"],
                        "image_enrichment": final_state.get("image_enrichment_summary") or {},
                        "recommendations": quality_report["recommendations"],
                    }
                )

        update_task_status(task_id, "completed")
        run_store.complete(
            task_id,
            owner,
            {
                "task_id": task_id,
                "local_file_path": local_file_path,
                "done_list": get_done_task_list(task_id),
                "image_enrichment": final_state.get("image_enrichment_summary") or {},
            },
        )
        logger.info(f"[{task_id}] LangGraph全流程执行完毕，文本知识库已可使用，图片视觉增强将在后台继续执行")
        observe_run("import", time.perf_counter() - run_started, "completed", quality_report)

    except Exception as exc:
        update_task_status(task_id, "failed")
        try:
            run_store.fail(task_id, owner, str(exc))
        except RuntimeError:
            logger.exception("持久化导入运行失败状态时发生异常")
        logger.error(f"[{task_id}] LangGraph全流程执行失败，异常信息：{exc}", exc_info=True)
        observe_run("import", time.perf_counter() - run_started, "failed")


@app.post("/upload", summary="文件上传接口", description="支持多文件批量上传，自动触发知识库导入全流程")
async def upload_files(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    principal: Principal = Depends(require_role("import")),
):
    """
    接收并保存上传文件，然后为每个文件启动独立导入任务。

    文件会同时保存到本地任务目录和MinIO。MinIO上传异常不会阻止本地解析，
    但文档图片后续需要依赖MinIO执行异步视觉增强，因此异常会在日志中明确记录。
    """
    security_config = load_security_config()
    validated_uploads: list[tuple[UploadFile, str]] = []
    for upload in files:
        try:
            filename = safe_upload_filename(upload.filename, security_config.allowed_upload_extensions)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if upload.size is not None and upload.size > security_config.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Uploaded file exceeds MAX_UPLOAD_BYTES")
        validated_uploads.append((upload, filename))

    date_based_root_dir = os.path.join(
        PROJECT_ROOT / "output" / "tenants",
        principal.tenant_id,
        datetime.now().strftime("%Y%m%d"),
    )
    task_ids = []

    for file, filename in validated_uploads:
        task_id = str(uuid.uuid4())
        task_ids.append(task_id)
        logger.info(
            f"[{task_id}] 开始处理上传文件，tenant_id={principal.tenant_id}，"
            f"文件名：{filename}，文件类型：{file.content_type}"
        )

        add_running_task(task_id, "upload_file")
        task_local_dir = os.path.join(date_based_root_dir, task_id)
        os.makedirs(task_local_dir, exist_ok=True)
        local_file_abs_path = os.path.join(task_local_dir, filename)

        total_bytes = 0
        with open(local_file_abs_path, "wb") as file_buffer:
            while chunk := file.file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > security_config.max_upload_bytes:
                    file_buffer.close()
                    os.remove(local_file_abs_path)
                    raise HTTPException(status_code=413, detail="Uploaded file exceeds MAX_UPLOAD_BYTES")
                file_buffer.write(chunk)
        logger.info(f"[{task_id}] 文件已保存至本地，路径：{local_file_abs_path}")

        minio_pdf_base_dir = os.getenv("MINIO_PDF_DIR", "pdf_files")
        minio_object_name = tenant_object_prefix(
            principal.tenant_id,
            minio_pdf_base_dir,
            datetime.now().strftime("%Y%m%d"),
            task_id,
            filename,
        )
        try:
            minio_client = get_minio_client()
            if minio_client is None:
                raise HTTPException(status_code=500, detail="MinIO service connection failed, please check MinIO config")
            minio_bucket_name = os.getenv("MINIO_BUCKET_NAME", "kb-import-bucket")
            minio_client.fput_object(
                bucket_name=minio_bucket_name,
                object_name=minio_object_name,
                file_path=local_file_abs_path,
                content_type=file.content_type,
            )
            logger.info(f"[{task_id}] 文件已成功上传至MinIO，桶名：{minio_bucket_name}，对象名：{minio_object_name}")
        except Exception as exc:
            logger.warning(f"[{task_id}] 文件上传MinIO失败，将继续执行本地处理流程，异常信息：{exc}", exc_info=True)

        add_done_task(task_id, "upload_file")
        runtime_config = load_runtime_config()
        get_run_store().create(
            task_id,
            "import",
            {"local_dir": task_local_dir, "local_file_path": local_file_abs_path},
            max_attempts=runtime_config.max_attempts,
            tenant_id=principal.tenant_id,
        )
        background_tasks.add_task(
            run_graph_task,
            task_id,
            task_local_dir,
            local_file_abs_path,
            False,
            principal.tenant_id,
        )
        logger.info(f"[{task_id}] 已将LangGraph全流程加入后台任务，任务已启动")

    logger.info(f"多文件上传处理完毕，共处理{len(files)}个文件，生成TaskID列表：{task_ids}")
    return {
        "code": 200,
        "message": f"Files uploaded successfully, total: {len(files)}",
        "task_ids": task_ids,
    }


def _get_image_enrichment_progress(task_id: str) -> Dict[str, Any]:
    """
    查询指定导入任务的图片视觉增强进度。

    图片资产尚未生成、MongoDB临时不可用或旧任务没有图片记录时返回安全默认值，
    不会因为附加进度查询失败而影响原有导入任务状态接口。
    """
    try:
        return get_image_asset_tool().get_document_progress(task_id)
    except Exception as exc:
        logger.warning(f"[{task_id}] 查询图片视觉增强进度失败：{exc}")
        return {
            "document_id": task_id,
            "total": 0,
            "finished": 0,
            "pending": 0,
            "processing": 0,
            "completed": 0,
            "skipped": 0,
            "failed": 0,
            "is_finished": False,
            "available": False,
        }


@app.get("/status/{task_id}", summary="任务状态查询", description="查询文本知识库导入进度和图片异步增强进度")
async def get_task_progress(
    task_id: str,
    principal: Principal = Depends(require_role("import")),
):
    """
    返回导入主流程和图片视觉增强两个层级的状态。

    status=completed 表示正文、切片、向量和Milvus入库已经完成，知识库可以立即问答；
    image_enrichment中pending或processing大于0表示图片仍在后台增强，但不会阻止普通文本问答。
    """
    durable_run = get_run_store().get_for_tenant(task_id, principal.tenant_id)
    if durable_run is None or durable_run.kind != "import":
        raise HTTPException(status_code=404, detail="Import run not found")

    task_status_info: Dict[str, Any] = {
        "code": 200,
        "task_id": task_id,
        "status": get_task_status(task_id),
        "done_list": get_done_task_list(task_id),
        "running_list": get_running_task_list(task_id),
        "durable_run": durable_run.to_public_dict(),
        "image_enrichment": _get_image_enrichment_progress(task_id),
    }
    task_status_info["knowledge_base_ready"] = task_status_info["status"] == "completed"
    logger.info(
        f"[{task_id}] 任务状态查询，主流程={task_status_info['status']}，"
        f"图片增强={task_status_info['image_enrichment']}"
    )
    return task_status_info


@app.get("/runs/{run_id}", tags=["runtime"])
async def get_run(run_id: str, principal: Principal = Depends(require_role("import"))):
    """查询持久化的导入运行记录。"""
    run = get_run_store().get_for_tenant(run_id, principal.tenant_id)
    if run is None or run.kind != "import":
        raise HTTPException(status_code=404, detail="Import run not found")
    result = run.to_public_dict()
    result["image_enrichment"] = _get_image_enrichment_progress(run_id)
    return result


@app.post("/runs/{run_id}/retry", status_code=202, tags=["runtime"])
async def retry_run(
    run_id: str,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_role("import")),
):
    """请求恢复失败或中断的知识库导入任务。"""
    run_store = get_run_store()
    run = run_store.get_for_tenant(run_id, principal.tenant_id)
    if run is None or run.kind != "import":
        raise HTTPException(status_code=404, detail="Import run not found")
    try:
        pending = run_store.request_retry(run_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    background_tasks.add_task(
        run_graph_task,
        run_id,
        str(run.input["local_dir"]),
        str(run.input["local_file_path"]),
        True,
        principal.tenant_id,
    )
    return pending.to_public_dict()


if __name__ == "__main__":
    """本地开发环境直接运行该文件时启动导入服务。"""
    logger.info("File Import Service 服务启动中...")
    uvicorn.run(app=app, host="127.0.0.1", port=8000)
