"""Knowledge document governance HTTP routes."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException

from app.modules.knowledge.application.legacy_migration import migrate_legacy_group, scan_legacy_knowledge
from app.modules.knowledge.application.registry import get_document_registry
from app.modules.knowledge.domain.document import DocumentStatus, legacy_document_identity
from app.platform.config.milvus_config import milvus_config
from app.platform.security.auth import Principal, require_role
from app.platform.vector_store.milvus import get_milvus_client


router = APIRouter(tags=["knowledge-governance"])


@router.get("/knowledge/documents")
async def list_knowledge_documents(
    status: str = "",
    query: str = "",
    skip: int = 0,
    limit: int = 50,
    principal: Principal = Depends(require_role("admin")),
):
    """分页查询当前租户的知识文档；默认按最近更新时间倒序。"""
    if status and status not in {value.value for value in DocumentStatus}:
        raise HTTPException(status_code=422, detail="不支持的文档状态")
    return get_document_registry().list_documents(
        principal.tenant_id,
        status=status,
        query=query,
        skip=skip,
        limit=limit,
    )


def _legacy_migration_context(tenant_id: str):
    """连接Milvus并返回旧知识迁移所需上下文。"""
    client = get_milvus_client()
    collection_name = milvus_config.chunks_collection
    if client is None or not collection_name:
        raise RuntimeError("Milvus未配置或当前不可连接")
    if not client.has_collection(collection_name=collection_name):
        return client, collection_name, {}
    return client, collection_name, scan_legacy_knowledge(client, collection_name, tenant_id)


@router.post("/knowledge/legacy/register")
async def register_legacy_knowledge(
    principal: Principal = Depends(require_role("admin")),
):
    """补齐旧切片版本元数据，校验成功后登记并发布legacy-v1。"""
    try:
        client, collection_name, groups = _legacy_migration_context(principal.tenant_id)
        registry = get_document_registry()
        registered = 0
        migrated = 0
        migrated_chunks = 0
        rekeyed_chunks = 0
        unchanged = 0
        skipped = 0
        for file_title, summary in groups.items():
            document_id, revision_id = legacy_document_identity(principal.tenant_id, file_title)
            migration = migrate_legacy_group(
                client,
                collection_name,
                principal.tenant_id,
                file_title,
                summary,
            )
            if migration["migrated_count"]:
                migrated += 1
                migrated_chunks += migration["migrated_count"]
                rekeyed_chunks += migration["rekeyed_count"]
            else:
                unchanged += 1

            existing = registry.get_document(principal.tenant_id, document_id)
            if existing is not None:
                versions = {
                    str(version.get("revision_id") or ""): version
                    for version in existing.get("versions") or []
                }
                version = versions.get(revision_id)
                if version is None:
                    raise RuntimeError(f"文档{document_id}已存在但缺少版本{revision_id}")
                if int(version.get("chunk_count") or 0) != int(summary["chunk_count"]):
                    raise RuntimeError(
                        f"版本{revision_id}登记数量与Milvus不一致："
                        f"登记{int(version.get('chunk_count') or 0)}条，实际{int(summary['chunk_count'])}条"
                    )
                skipped += 1
                continue
            registry.register_import(
                tenant_id=principal.tenant_id,
                document_id=document_id,
                revision_id=revision_id,
                filename=file_title,
                title=os.path.splitext(file_title)[0],
                version_label="legacy-v1",
                publish_requested=False,
                actor=principal.key_id,
            )
            registry.mark_import_succeeded(
                principal.tenant_id,
                revision_id,
                chunk_count=int(migration["chunk_count"]),
                image_count=0,
                item_names=list(summary["item_names"]),
                actor=principal.key_id,
            )
            registry.publish_version(
                principal.tenant_id,
                document_id,
                revision_id,
                actor=principal.key_id,
                action="register_legacy",
            )
            registered += 1
        return {
            "message": "旧知识库版本迁移和登记完成",
            "discovered": len(groups),
            "registered": registered,
            "migrated": migrated,
            "migrated_chunks": migrated_chunks,
            "rekeyed_chunks": rekeyed_chunks,
            "unchanged": unchanged,
            "skipped": skipped,
        }
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/knowledge/documents/{document_id}")
async def get_knowledge_document(
    document_id: str,
    principal: Principal = Depends(require_role("admin")),
):
    """读取文档及全部历史版本。"""
    document = get_document_registry().get_document(principal.tenant_id, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Knowledge document not found")
    return document


def _lifecycle_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="文档或版本不存在")
    return HTTPException(status_code=409, detail=str(exc))


@router.post("/knowledge/documents/{document_id}/versions/{revision_id}/publish")
async def publish_knowledge_version(
    document_id: str,
    revision_id: str,
    principal: Principal = Depends(require_role("admin")),
):
    """发布指定版本；仅同一文档、同一适用范围的旧生效版本自动归档。"""
    try:
        return get_document_registry().publish_version(
            principal.tenant_id,
            document_id,
            revision_id,
            actor=principal.key_id,
        )
    except (KeyError, ValueError) as exc:
        raise _lifecycle_error(exc) from exc


@router.post("/knowledge/documents/{document_id}/versions/{revision_id}/rollback")
async def rollback_knowledge_version(
    document_id: str,
    revision_id: str,
    principal: Principal = Depends(require_role("admin")),
):
    """把已导入完成的历史版本重新发布为当前版本。"""
    try:
        return get_document_registry().publish_version(
            principal.tenant_id,
            document_id,
            revision_id,
            actor=principal.key_id,
            action="rollback",
        )
    except (KeyError, ValueError) as exc:
        raise _lifecycle_error(exc) from exc


@router.post("/knowledge/documents/{document_id}/disable")
async def disable_knowledge_document(
    document_id: str,
    principal: Principal = Depends(require_role("admin")),
):
    """停用文档，立即从查询结果中过滤，但保留全部版本和对象。"""
    try:
        return get_document_registry().disable_document(
            principal.tenant_id,
            document_id,
            actor=principal.key_id,
        )
    except (KeyError, ValueError) as exc:
        raise _lifecycle_error(exc) from exc


@router.post("/knowledge/documents/{document_id}/enable")
async def enable_knowledge_document(
    document_id: str,
    principal: Principal = Depends(require_role("admin")),
):
    """重新启用仍有生效版本的文档。"""
    try:
        return get_document_registry().enable_document(
            principal.tenant_id,
            document_id,
            actor=principal.key_id,
        )
    except (KeyError, ValueError) as exc:
        raise _lifecycle_error(exc) from exc


@router.get("/knowledge/audit")
async def list_knowledge_audit_logs(
    document_id: str = "",
    limit: int = 100,
    principal: Principal = Depends(require_role("admin")),
):
    """读取发布、回滚、停用等治理操作审计记录。"""
    return {
        "items": get_document_registry().list_audit_logs(
            principal.tenant_id,
            document_id=document_id,
            limit=limit,
        )
    }
