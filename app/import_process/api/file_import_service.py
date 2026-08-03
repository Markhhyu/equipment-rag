import os
import time
import uuid
from typing import List, Dict, Any
from datetime import datetime
import uvicorn
from contextlib import asynccontextmanager
# 第三方库
from fastapi import FastAPI, UploadFile, File, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
# 项目内部工具/配置/客户端
from app.clients.minio_utils import get_minio_client
from app.utils.path_util import PROJECT_ROOT
from app.utils.task_utils import (
    add_running_task,
    add_done_task,
    get_done_task_list,
    get_running_task_list,
    update_task_status,
    get_task_status,
)
from app.import_process.agent.state import get_default_state
from app.core.logger import logger  # 项目统一日志工具
from app.runtime.config import load_runtime_config
from app.runtime.run_store import get_run_store, run_owner
from app.security.auth import Principal, require_role
from app.security.config import load_security_config
from app.security.http import configure_http_security
from app.security.tenancy import safe_upload_filename, tenant_object_prefix
from app.observability.langfuse_monitor import flush_langfuse, trace_import
from app.observability.prometheus_metrics import install_prometheus, observe_run
from app.observability.rag_observability import score_import_result

# 服务关闭时主动发送SDK缓存中的Langfuse事件，减少容器停止时丢失最后一批Trace的概率。
@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    flush_langfuse()


# 初始化FastAPI应用实例
# 标题和描述会在Swagger文档(http://ip:port/docs)中展示
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


# --------------------------
# 静态页面路由：返回文件导入前端页面import.html
# 访问地址：http://localhost:8000/import.html
# --------------------------
@app.get("/import.html", response_class=FileResponse)
async def get_import_page():
    """返回文件导入前端页面：import.html"""
    # 拼接HTML文件绝对路径，基于项目根目录定位
    html_abs_path = PROJECT_ROOT / "app/import_process/page/import.html"
    # 日志记录页面访问的文件路径，方便排查文件不存在问题
    logger.info(f"前端页面访问，文件绝对路径：{html_abs_path}")

    # 校验文件是否存在，不存在则抛出404异常
    if not os.path.exists(html_abs_path):
        logger.error(f"前端页面文件不存在，路径：{html_abs_path}")
        raise HTTPException(status_code=404, detail="import.html page not found")

    # 以FileResponse返回HTML文件，浏览器自动渲染
    return FileResponse(
        path=html_abs_path,
        media_type="text/html"  # 显式指定媒体类型为HTML，确保浏览器正确解析
    )


# --------------------------
# 后台任务：LangGraph全流程执行
# 独立于主请求线程，由BackgroundTasks触发，避免阻塞接口响应
# --------------------------
def run_graph_task(
    task_id: str,
    local_dir: str,
    local_file_path: str,
    resume: bool = False,
    tenant_id: str = "local",
):
    """
    LangGraph全流程执行后台任务
    核心流程：初始化状态 → 流式执行图节点 → 实时更新任务状态 → 异常捕获
    任务状态更新：pending → processing → completed/failed
    节点进度更新：每完成一个节点，将节点名加入done_list，供前端轮询查看

    :param task_id: 全局唯一任务ID，关联单个文件的全流程处理
    :param local_dir: 该任务的本地文件存储目录（含临时文件/解析结果）
    :param local_file_path: 上传文件的本地绝对路径
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
        },
        max_attempts=runtime_config.max_attempts,
        tenant_id=tenant_id,
    )
    owner = run_owner()
    run_store.claim(task_id, owner, runtime_config.lease_seconds)

    try:
        from app.import_process.agent.main_graph import kb_import_app

        # 1. 更新任务全局状态为：处理中
        update_task_status(task_id, "processing")
        logger.info(f"[{task_id}] 开始执行LangGraph全流程，本地文件路径：{local_file_path}")

        # 2. 初始化LangGraph状态：加载默认状态 + 注入当前任务的核心参数
        init_state = get_default_state()
        init_state["task_id"] = task_id  # 任务ID关联
        init_state["tenant_id"] = tenant_id
        init_state["local_dir"] = local_dir  # 任务本地目录
        init_state["local_file_path"] = local_file_path  # 上传文件本地路径

        # 3. 建立文件导入根Trace，再在其中执行LangGraph。
        # 每个业务节点已经由observed_graph_node包装，会自动成为根Trace的子Span。
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
                for node_name, node_result in event.items():
                    logger.info(f"[{task_id}] LangGraph节点执行完成：{node_name}")
                    add_done_task(task_id, node_name)

            # 从LangGraph checkpoint读取最终状态，用于生成解析、切片、向量和入库质量报告。
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
                        "recommendations": quality_report["recommendations"],
                    }
                )

        # 4. 全流程执行完成，更新任务全局状态为：已完成
        update_task_status(task_id, "completed")
        run_store.complete(
            task_id,
            owner,
            {
                "task_id": task_id,
                "local_file_path": local_file_path,
                "done_list": get_done_task_list(task_id),
            },
        )
        logger.info(f"[{task_id}] LangGraph全流程执行完毕，任务完成")
        observe_run("import", time.perf_counter() - run_started, "completed", quality_report)

    except Exception as e:
        # 5. 捕获全流程异常，更新任务全局状态为：失败，并记录错误日志（含堆栈）
        update_task_status(task_id, "failed")
        try:
            run_store.fail(task_id, owner, str(e))
        except RuntimeError:
            logger.exception("持久化导入运行失败状态时发生异常")
        logger.error(f"[{task_id}] LangGraph全流程执行失败，异常信息：{str(e)}", exc_info=True)
        observe_run("import", time.perf_counter() - run_started, "failed")


# --------------------------
# 核心接口：文件上传接口
# 支持多文件上传，核心流程：接收文件 → 本地保存 → MinIO上传 → 启动后台任务
# 访问地址：http://localhost:8000/upload （POST请求，form-data格式传参）
# --------------------------
@app.post("/upload", summary="文件上传接口", description="支持多文件批量上传，自动触发知识库导入全流程")
async def upload_files(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    principal: Principal = Depends(require_role("import")),
):
    """
    文件上传核心接口
    1. 接收前端上传的多文件（PDF/MD为主）
    2. 按「日期/任务ID」分层保存到本地输出目录，避免文件冲突
    3. 将文件上传至MinIO对象存储，做持久化保存
    4. 为每个文件生成唯一TaskID，启动独立的LangGraph后台处理任务
    5. 实时更新任务状态，供前端轮询监控进度

    :param background_tasks: FastAPI后台任务对象，用于异步执行LangGraph流程
    :param files: 前端上传的文件列表（form-data格式）
    :return: 包含上传结果和所有任务ID的JSON响应
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

    # 本地文件按租户、日期和任务ID隔离。
    date_based_root_dir = os.path.join(
        PROJECT_ROOT / "output" / "tenants",
        principal.tenant_id,
        datetime.now().strftime("%Y%m%d"),
    )
    # 初始化任务ID列表，用于返回给前端（一个文件对应一个TaskID）
    task_ids = []

    # 2. 遍历处理每个上传的文件（多文件批量处理，各自独立生成TaskID）
    for file, filename in validated_uploads:
        # 生成全局唯一TaskID（UUID4），作为单个文件的全流程标识
        task_id = str(uuid.uuid4())
        task_ids.append(task_id)
        logger.info(
            f"[{task_id}] 开始处理上传文件，tenant_id={principal.tenant_id}，"
            f"文件名：{filename}，文件类型：{file.content_type}"
        )

        # 3. 标记「文件上传」阶段为「运行中」，前端轮询可查
        add_running_task(task_id, "upload_file")

        # 4. 构建该任务的本地独立目录：output/YYYYMMDD/TaskID，避免多文件重名冲突
        task_local_dir = os.path.join(date_based_root_dir, task_id)
        os.makedirs(task_local_dir, exist_ok=True)  # 目录不存在则创建，存在则不做处理
        # 构建上传文件的本地保存绝对路径
        local_file_abs_path = os.path.join(task_local_dir, filename)

        # 5. 分块写入并限制大小，避免单个请求耗尽磁盘。
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

        # 6. 将本地文件上传至MinIO对象存储，做持久化保存
        # 从环境变量获取MinIO的PDF存储目录配置
        minio_pdf_base_dir = os.getenv("MINIO_PDF_DIR", "pdf_files")  # 缺省值：pdf_files
        # 构建MinIO中的文件对象名：配置目录/YYYYMMDD/文件名（按日期分层，和本地一致）
        minio_object_name = tenant_object_prefix(
            principal.tenant_id,
            minio_pdf_base_dir,
            datetime.now().strftime("%Y%m%d"),
            task_id,
            filename,
        )
        try:
            # 获取MinIO客户端实例
            minio_client = get_minio_client()
            if minio_client is None:
                # MinIO客户端获取失败，抛出500服务异常
                raise HTTPException(status_code=500,
                                    detail="MinIO service connection failed, please check MinIO config")
            # 从环境变量获取MinIO的桶名配置
            minio_bucket_name = os.getenv("MINIO_BUCKET_NAME", "kb-import-bucket")  # 缺省值：kb-import-bucket

            # 本地文件上传至MinIO（同名文件会自动覆盖，保证文件最新）
            minio_client.fput_object(
                bucket_name=minio_bucket_name,
                object_name=minio_object_name,
                file_path=local_file_abs_path,
                content_type=file.content_type  # 传递文件原始MIME类型
            )
            logger.info(f"[{task_id}] 文件已成功上传至MinIO，桶名：{minio_bucket_name}，对象名：{minio_object_name}")
        except Exception as e:
            # MinIO上传失败，记录警告日志（不中断后续流程，本地文件仍可继续处理）
            logger.warning(f"[{task_id}] 文件上传MinIO失败，将继续执行本地处理流程，异常信息：{str(e)}", exc_info=True)

        # 7. 标记「文件上传」阶段为「已完成」，前端轮询可查
        add_done_task(task_id, "upload_file")

        # 8. 将LangGraph全流程处理加入FastAPI后台任务（异步执行，不阻塞当前接口响应）
        runtime_config = load_runtime_config()
        get_run_store().create(
            task_id,
            "import",
            {
                "local_dir": task_local_dir,
                "local_file_path": local_file_abs_path,
            },
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

    # 9. 所有文件处理完毕，返回上传成功信息和所有TaskID（前端基于TaskID轮询进度）
    logger.info(f"多文件上传处理完毕，共处理{len(files)}个文件，生成TaskID列表：{task_ids}")
    return {
        "code": 200,
        "message": f"Files uploaded successfully, total: {len(files)}",
        "task_ids": task_ids
    }


# --------------------------
# 核心接口：任务状态查询接口
# 前端轮询此接口获取单个任务的处理进度和状态
# 访问地址：http://localhost:8000/status/{task_id} （GET请求）
# --------------------------
@app.get("/status/{task_id}", summary="任务状态查询", description="根据TaskID查询单个文件的处理进度和全局状态")
async def get_task_progress(
    task_id: str,
    principal: Principal = Depends(require_role("import")),
):
    """
    任务状态查询接口
    前端轮询此接口（如每秒1次），获取任务的实时处理进度
    节点展示进度来自进程内状态，运行状态与恢复信息来自持久化运行注册表。

    :param task_id: 全局唯一任务ID（由/upload接口返回）
    :return: 包含任务全局状态、已完成节点、运行中节点的JSON响应
    """
    durable_run = get_run_store().get_for_tenant(task_id, principal.tenant_id)
    if durable_run is None or durable_run.kind != "import":
        raise HTTPException(status_code=404, detail="Import run not found")
    # 构造任务状态返回体
    task_status_info: Dict[str, Any] = {
        "code": 200,
        "task_id": task_id,
        "status": get_task_status(task_id),  # 任务全局状态：pending/processing/completed/failed
        "done_list": get_done_task_list(task_id),  # 已完成的节点/阶段列表
        "running_list": get_running_task_list(task_id)  # 正在运行的节点/阶段列表
    }
    task_status_info["durable_run"] = durable_run.to_public_dict()
    # 记录状态查询日志，方便追踪前端轮询情况
    logger.info(
        f"[{task_id}] 任务状态查询，当前状态：{task_status_info['status']}，已完成节点：{task_status_info['done_list']}")
    return task_status_info


@app.get("/runs/{run_id}", tags=["runtime"])
async def get_run(run_id: str, principal: Principal = Depends(require_role("import"))):
    run = get_run_store().get_for_tenant(run_id, principal.tenant_id)
    if run is None or run.kind != "import":
        raise HTTPException(status_code=404, detail="Import run not found")
    return run.to_public_dict()


@app.post("/runs/{run_id}/retry", status_code=202, tags=["runtime"])
async def retry_run(
    run_id: str,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_role("import")),
):
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


# --------------------------
# 服务启动入口
# 直接运行此脚本即可启动FastAPI服务，无需额外执行uvicorn命令
# --------------------------
if __name__ == "__main__":
    """服务启动入口：本地开发环境直接运行"""
    logger.info("File Import Service 服务启动中...")
    # 启动uvicorn服务，绑定本地IP和8000端口，关闭自动重载（生产环境建议用workers多进程）
    uvicorn.run(
        app=app,
        host="127.0.0.1",  # 仅本地访问，生产环境改为0.0.0.0（允许所有IP访问）
        port=8000  # 服务端口
    )
