import hashlib
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List

import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.modules.knowledge.application.image_assets import get_image_asset_tool
from app.modules.knowledge.application.registry import get_document_registry
from app.modules.knowledge.domain.document import DocumentStatus
from app.modules.ingestion.api.knowledge_routes import router as knowledge_router
from app.platform.storage.minio import get_minio_client, minio_object_uri
from app.platform.observability.logging import logger
from app.modules.knowledge.domain.trust import normalize_trust_level
from app.modules.ingestion.graph.state import get_default_state
from app.platform.observability.langfuse_monitor import flush_langfuse, trace_import
from app.platform.observability.prometheus_metrics import install_prometheus, observe_run
from app.platform.observability.rag_observability import score_import_result
from app.platform.runtime.config import load_runtime_config
from app.platform.runtime.run_store import get_run_store, run_owner
from app.platform.security.auth import Principal, require_role
from app.platform.security.config import load_security_config
from app.platform.security.http import configure_http_security
from app.platform.security.tenancy import safe_upload_filename, tenant_object_prefix
from app.workers.image_enrichment import start_image_enrichment_worker, stop_image_enrichment_worker
from app.shared.paths import PROJECT_ROOT
from app.platform.runtime.task_progress import (
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

FRONTEND_DIST_DIR = PROJECT_ROOT / "frontend" / "dist"
FRONTEND_ASSETS_DIR = FRONTEND_DIST_DIR / "assets"
if FRONTEND_ASSETS_DIR.exists():
    # 导入页和接口保持同源，生产环境启用API Key后不需要额外放宽CORS。
    app.mount("/assets", StaticFiles(directory=FRONTEND_ASSETS_DIR), name="frontend-assets")


@app.get("/health", tags=["system"])
async def health():
    """容器编排与负载均衡器使用的存活探针。"""
    return {"status": "ok", "service": "import-api"}


@app.get("/import.html", response_class=FileResponse)
async def get_import_page():
    """优先返回Vue构建页面；本地尚未构建时保留旧页面作为降级入口。"""
    built_html_path = FRONTEND_DIST_DIR / "import.html"
    if built_html_path.exists():
        return FileResponse(path=built_html_path, media_type="text/html")
    html_abs_path = PROJECT_ROOT / "app/import_process/page/import.html"
    logger.info(f"前端页面访问，文件绝对路径：{html_abs_path}")

    if not os.path.exists(html_abs_path):
        logger.error(f"前端页面文件不存在，路径：{html_abs_path}")
        raise HTTPException(status_code=404, detail="import.html page not found")

    return FileResponse(path=html_abs_path, media_type="text/html")


@app.get("/knowledge.html", response_class=FileResponse)
async def get_knowledge_page():
    """返回知识库治理页面；治理页只提供构建后的Vue版本。"""
    built_html_path = FRONTEND_DIST_DIR / "knowledge.html"
    if not built_html_path.exists():
        raise HTTPException(status_code=404, detail="请先在frontend目录执行npm run build")
    return FileResponse(path=built_html_path, media_type="text/html")


def run_graph_task(
    task_id: str,
    local_dir: str,
    local_file_path: str,
    resume: bool = False,
    tenant_id: str = "local",
    document_id: str = "",
    revision_id: str = "",
    version_label: str = "",
    trust_level: str = "manufacturer_manual",
    device_model: str = "",
    equipment_version: str = "",
    software_version: str = "",
    firmware_version: str = "",
    hardware_revision: str = "",
    site_id: str = "",
    asset_ids: list[str] | None = None,
    actor: str = "system",
):
    """
    执行单个文件的LangGraph知识库导入流程。

    主流程只负责解析、图片资产保存、文档切片、向量化和Milvus入库；
    图片视觉描述由常驻后台任务异步处理，不再阻塞该导入任务完成。

    :param task_id: 单个文件的全流程任务编号；新治理模型中同时作为不可变revision_id。
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
        {
            "local_dir": local_dir,
            "local_file_path": local_file_path,
            "document_id": document_id or task_id,
            "revision_id": revision_id or task_id,
            "version_label": version_label or "legacy-v1",
            "trust_level": normalize_trust_level(trust_level),
            "device_model": device_model,
            "equipment_version": equipment_version,
            "software_version": software_version,
            "firmware_version": firmware_version,
            "hardware_revision": hardware_revision,
            "site_id": site_id,
            "asset_ids": asset_ids or [],
        },
        max_attempts=runtime_config.max_attempts,
        tenant_id=tenant_id,
    )
    owner = run_owner()
    run_store.claim(task_id, owner, runtime_config.lease_seconds)

    try:
        from app.modules.ingestion.graph.main_graph import kb_import_app

        update_task_status(task_id, "processing")
        logger.info(f"[{task_id}] 开始执行LangGraph全流程，本地文件路径：{local_file_path}")

        init_state = get_default_state()
        init_state["task_id"] = task_id
        init_state["tenant_id"] = tenant_id
        init_state["local_dir"] = local_dir
        init_state["local_file_path"] = local_file_path
        init_state["document_id"] = document_id or task_id
        init_state["revision_id"] = revision_id or task_id
        init_state["version_label"] = version_label or "legacy-v1"
        init_state["trust_level"] = normalize_trust_level(trust_level)
        init_state["device_model"] = device_model
        init_state["equipment_version"] = equipment_version
        init_state["software_version"] = software_version
        init_state["firmware_version"] = firmware_version
        init_state["hardware_revision"] = hardware_revision
        init_state["site_id"] = site_id
        init_state["asset_ids"] = asset_ids or []

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

        chunks = final_state.get("chunks") or []
        item_names = list(
            dict.fromkeys(
                str(chunk.get("item_name") or "").strip()
                for chunk in chunks
                if isinstance(chunk, dict) and str(chunk.get("item_name") or "").strip()
            )
        )
        image_summary = final_state.get("image_enrichment_summary") or {}
        get_document_registry().mark_import_succeeded(
            tenant_id,
            revision_id or task_id,
            chunk_count=len(chunks),
            image_count=int(image_summary.get("total") or 0),
            item_names=item_names,
            actor=actor,
        )

        update_task_status(task_id, "completed")
        run_store.complete(
            task_id,
            owner,
            {
                "task_id": task_id,
                "document_id": document_id or task_id,
                "revision_id": revision_id or task_id,
                "version_label": version_label or "legacy-v1",
                "trust_level": normalize_trust_level(trust_level),
                "local_file_path": local_file_path,
                "done_list": get_done_task_list(task_id),
                "image_enrichment": final_state.get("image_enrichment_summary") or {},
            },
        )
        logger.info(f"[{task_id}] LangGraph全流程执行完毕；是否参与查询由知识版本发布状态决定")
        observe_run("import", time.perf_counter() - run_started, "completed", quality_report)

    except Exception as exc:
        update_task_status(task_id, "failed")
        try:
            get_document_registry().mark_import_failed(tenant_id, revision_id or task_id, str(exc), actor=actor)
        except Exception:
            logger.exception(f"[{task_id}] 更新知识版本失败状态时发生异常")
        try:
            run_store.fail(task_id, owner, str(exc))
        except RuntimeError:
            logger.exception("持久化导入运行失败状态时发生异常")
        logger.error(f"[{task_id}] LangGraph全流程执行失败，异常信息：{exc}", exc_info=True)
        observe_run("import", time.perf_counter() - run_started, "failed")


async def _enqueue_import(
    *,
    upload: UploadFile,
    filename: str,
    background_tasks: BackgroundTasks,
    principal: Principal,
    document_id: str | None,
    title: str,
    version_label: str,
    publish_after_import: bool,
    trust_level: str = "manufacturer_manual",
    device_model: str = "",
    equipment_version: str = "",
    software_version: str = "",
    firmware_version: str = "",
    hardware_revision: str = "",
    site_id: str = "",
    asset_ids: list[str] | None = None,
) -> dict[str, str]:
    """保存一个上传文件、登记不可变版本并启动LangGraph导入任务。"""
    security_config = load_security_config()
    task_id = str(uuid.uuid4())
    stable_document_id = str(document_id or task_id).strip()
    # 旧上传入口会显式传入legacy-v1；治理入口留空时应生成可读时间版本，不能误标为旧数据。
    stable_version_label = str(version_label or datetime.now().strftime("v%Y%m%d-%H%M%S")).strip()
    date_based_root_dir = os.path.join(
        PROJECT_ROOT / "output" / "tenants",
        principal.tenant_id,
        datetime.now().strftime("%Y%m%d"),
    )
    task_local_dir = os.path.join(date_based_root_dir, task_id)
    os.makedirs(task_local_dir, exist_ok=True)
    local_file_abs_path = os.path.join(task_local_dir, filename)

    add_running_task(task_id, "upload_file")
    total_bytes = 0
    digest = hashlib.sha256()
    with open(local_file_abs_path, "wb") as file_buffer:
        while chunk := upload.file.read(1024 * 1024):
            total_bytes += len(chunk)
            if total_bytes > security_config.max_upload_bytes:
                file_buffer.close()
                os.remove(local_file_abs_path)
                raise HTTPException(status_code=413, detail="Uploaded file exceeds MAX_UPLOAD_BYTES")
            digest.update(chunk)
            file_buffer.write(chunk)

    minio_pdf_base_dir = os.getenv("MINIO_PDF_DIR", "pdf_files")
    minio_object_name = tenant_object_prefix(
        principal.tenant_id,
        minio_pdf_base_dir,
        stable_document_id,
        task_id,
        filename,
    )
    source_object_uri = ""
    try:
        minio_client = get_minio_client()
        if minio_client is None:
            raise RuntimeError("MinIO service connection failed, please check MinIO config")
        minio_bucket_name = os.getenv("MINIO_BUCKET_NAME", "kb-import-bucket")
        minio_client.fput_object(
            bucket_name=minio_bucket_name,
            object_name=minio_object_name,
            file_path=local_file_abs_path,
            content_type=upload.content_type,
        )
        source_object_uri = minio_object_uri(minio_bucket_name, minio_object_name)
    except Exception as exc:
        logger.warning(f"[{task_id}] 文件上传MinIO失败，将继续执行本地处理流程，异常信息：{exc}", exc_info=True)

    get_document_registry().register_import(
        tenant_id=principal.tenant_id,
        document_id=stable_document_id,
        revision_id=task_id,
        filename=filename,
        title=title or os.path.splitext(filename)[0],
        version_label=stable_version_label,
        trust_level=normalize_trust_level(trust_level),
        source_object_uri=source_object_uri,
        content_hash=digest.hexdigest(),
        file_size=total_bytes,
        publish_requested=publish_after_import,
        device_model=device_model,
        equipment_version=equipment_version,
        software_version=software_version,
        firmware_version=firmware_version,
        hardware_revision=hardware_revision,
        site_id=site_id,
        asset_ids=asset_ids or [],
        actor=principal.key_id,
    )
    add_done_task(task_id, "upload_file")

    runtime_config = load_runtime_config()
    run_input = {
        "local_dir": task_local_dir,
        "local_file_path": local_file_abs_path,
        "document_id": stable_document_id,
        "revision_id": task_id,
        "version_label": stable_version_label,
        "trust_level": normalize_trust_level(trust_level),
        "device_model": device_model,
        "equipment_version": equipment_version,
        "software_version": software_version,
        "firmware_version": firmware_version,
        "hardware_revision": hardware_revision,
        "site_id": site_id,
        "asset_ids": asset_ids or [],
    }
    get_run_store().create(
        task_id,
        "import",
        run_input,
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
        stable_document_id,
        task_id,
        stable_version_label,
        normalize_trust_level(trust_level),
        device_model,
        equipment_version,
        software_version,
        firmware_version,
        hardware_revision,
        site_id,
        asset_ids or [],
        principal.key_id,
    )
    logger.info(
        f"[{task_id}] 已登记知识版本并启动导入，document_id={stable_document_id}，"
        f"version={stable_version_label}，publish_after_import={publish_after_import}"
    )
    return {
        "task_id": task_id,
        "document_id": stable_document_id,
        "revision_id": task_id,
        "version_label": stable_version_label,
    }


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

    task_ids = []
    documents = []
    for upload, filename in validated_uploads:
        result = await _enqueue_import(
            upload=upload,
            filename=filename,
            background_tasks=background_tasks,
            principal=principal,
            document_id=None,
            title=os.path.splitext(filename)[0],
            version_label="legacy-v1",
            # 旧上传入口保持“导入完成即可问答”，避免升级改变原有使用习惯。
            publish_after_import=True,
        )
        task_ids.append(result["task_id"])
        documents.append(result)
        await upload.close()

    logger.info(f"多文件上传处理完毕，共处理{len(files)}个文件，生成TaskID列表：{task_ids}")
    return {
        "code": 200,
        "message": f"Files uploaded successfully, total: {len(files)}",
        "task_ids": task_ids,
        "documents": documents,
    }


@app.post("/knowledge/documents/import", status_code=202, tags=["knowledge-governance"])
async def import_managed_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    document_id: str | None = Form(default=None),
    title: str = Form(default=""),
    version_label: str = Form(default=""),
    trust_level: str = Form(default="manufacturer_manual"),
    device_model: str = Form(default=""),
    equipment_version: str = Form(default=""),
    software_version: str = Form(default=""),
    firmware_version: str = Form(default=""),
    hardware_revision: str = Form(default=""),
    site_id: str = Form(default=""),
    asset_ids: str = Form(default=""),
    publish_after_import: bool = Form(default=False),
    principal: Principal = Depends(require_role("admin")),
):
    """导入一个受治理版本；默认进入草稿，人工发布后才参与查询。"""
    security_config = load_security_config()
    try:
        filename = safe_upload_filename(file.filename, security_config.allowed_upload_extensions)
        if file.size is not None and file.size > security_config.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Uploaded file exceeds MAX_UPLOAD_BYTES")
        result = await _enqueue_import(
            upload=file,
            filename=filename,
            background_tasks=background_tasks,
            principal=principal,
            document_id=document_id,
            title=title,
            version_label=version_label,
            trust_level=trust_level,
            device_model=device_model,
            equipment_version=equipment_version,
            software_version=software_version,
            firmware_version=firmware_version,
            hardware_revision=hardware_revision,
            site_id=site_id,
            asset_ids=[value.strip() for value in re.split(r"[,，\n]", asset_ids) if value.strip()],
            publish_after_import=publish_after_import,
        )
        return {"message": "版本已登记，正在后台导入", **result}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await file.close()


def _get_image_enrichment_progress(task_id: str) -> Dict[str, Any]:
    """
    查询指定导入任务的图片视觉增强进度。

    图片资产尚未生成、MongoDB临时不可用或旧任务没有图片记录时返回安全默认值，
    不会因为附加进度查询失败而影响原有导入任务状态接口。
    """
    try:
        return get_image_asset_tool().get_revision_progress(task_id)
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

    status=completed 表示正文、切片、向量和Milvus入库已经完成；只有已发布版本才可以参与问答；
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
    document_id = str(durable_run.input.get("document_id") or task_id)
    revision_id = str(durable_run.input.get("revision_id") or task_id)
    document = get_document_registry().get_document(principal.tenant_id, document_id)
    # 升级前的历史运行没有注册表记录，但对应旧Chunk仍按兼容策略放行；不能把旧任务误报为不可用。
    queryable = bool(
        task_status_info["status"] == "completed"
        and (
            document is None
            or (
                document.get("status") == DocumentStatus.ACTIVE.value
                and document.get("active_revision_id") == revision_id
            )
        )
    )
    task_status_info["governance"] = {
        "document_id": document_id,
        "revision_id": revision_id,
        "version_label": str(durable_run.input.get("version_label") or "legacy-v1"),
        "managed": document is not None,
        "queryable": queryable,
    }
    task_status_info["knowledge_base_ready"] = queryable
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
        str(run.input.get("document_id") or run_id),
        str(run.input.get("revision_id") or run_id),
        str(run.input.get("version_label") or "legacy-v1"),
        str(run.input.get("trust_level") or "manufacturer_manual"),
        str(run.input.get("device_model") or ""),
        str(run.input.get("equipment_version") or ""),
        str(run.input.get("software_version") or ""),
        str(run.input.get("firmware_version") or ""),
        str(run.input.get("hardware_revision") or ""),
        str(run.input.get("site_id") or ""),
        [str(value) for value in (run.input.get("asset_ids") or []) if str(value).strip()],
        principal.key_id,
    )
    return pending.to_public_dict()


app.include_router(knowledge_router)


if __name__ == "__main__":
    """本地开发环境直接运行该文件时启动导入服务。"""
    logger.info("File Import Service 服务启动中...")
    uvicorn.run(app=app, host="127.0.0.1", port=8000)
