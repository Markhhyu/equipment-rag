"""Architecture checks that keep new modules independent from compatibility paths."""

import ast
import importlib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NEW_CODE_ROOTS = (
    PROJECT_ROOT / "app" / "apps",
    PROJECT_ROOT / "app" / "modules",
    PROJECT_ROOT / "app" / "platform",
    PROJECT_ROOT / "app" / "workers",
)
LEGACY_COMPATIBILITY_MODULES = (
    "app.clients",
    "app.conf",
    "app.core",
    "app.import_process.agent",
    "app.import_process.api.file_import_service",
    "app.import_process.page_attribution",
    "app.knowledge_trust",
    "app.lm",
    "app.model",
    "app.modules.qa.infrastructure.history_legacy",
    "app.observability",
    "app.runtime",
    "app.security",
    "app.utils",
    "app.query_process.analytics",
    "app.query_process.agent",
    "app.query_process.api.query_service",
    "app.query_process.version_context",
    "app.tasks.image_enrichment_worker",
    "app.workflow",
)


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


def test_new_code_does_not_depend_on_legacy_compatibility_modules():
    violations = []
    for root in NEW_CODE_ROOTS:
        for path in root.rglob("*.py"):
            for module in _imported_modules(path):
                if any(module == legacy or module.startswith(f"{legacy}.") for legacy in LEGACY_COMPATIBILITY_MODULES):
                    violations.append(f"{path.relative_to(PROJECT_ROOT)} -> {module}")
    assert not violations, "新模块禁止依赖旧兼容路径：\n" + "\n".join(violations)


def test_platform_legacy_paths_reexport_canonical_symbols():
    from app.platform.runtime.config import load_runtime_config as platform_runtime_config
    from app.platform.security.config import load_security_config as platform_security_config
    from app.runtime.config import load_runtime_config as legacy_runtime_config
    from app.security.config import load_security_config as legacy_security_config

    assert legacy_runtime_config is platform_runtime_config
    assert legacy_security_config is platform_security_config


def test_config_and_observability_legacy_paths_alias_canonical_modules():
    assert importlib.import_module("app.conf.rag_tuning_config") is importlib.import_module(
        "app.platform.config.rag_tuning_config"
    )
    assert importlib.import_module("app.observability.quality_metrics") is importlib.import_module(
        "app.platform.observability.quality_metrics"
    )


def test_ai_legacy_paths_alias_canonical_modules():
    assert importlib.import_module("app.lm.lm_utils") is importlib.import_module("app.platform.ai.chat")
    assert importlib.import_module("app.lm.embedding_utils") is importlib.import_module(
        "app.platform.ai.embeddings"
    )
    assert importlib.import_module("app.model.reranker.factory") is importlib.import_module(
        "app.platform.ai.reranking.factory"
    )


def test_client_legacy_paths_alias_owning_modules():
    aliases = {
        "app.clients.minio_utils": "app.platform.storage.minio",
        "app.clients.milvus_utils": "app.platform.vector_store.milvus",
        "app.clients.mineru_client": "app.modules.ingestion.infrastructure.mineru",
        "app.clients.mongo_history_utils": "app.modules.qa.infrastructure.history",
        "app.clients.mongo_history_utils_new": "app.modules.qa.infrastructure.history_legacy",
        "app.clients.session_attachment_utils": "app.modules.qa.infrastructure.attachments",
        "app.clients.image_asset_mongo_utils": "app.modules.knowledge.infrastructure.image_assets",
        "app.clients.neo4j_utils": "app.modules.knowledge.infrastructure.neo4j",
    }

    for legacy, canonical in aliases.items():
        assert importlib.import_module(legacy) is importlib.import_module(canonical)


def test_core_and_utility_legacy_paths_alias_owning_modules():
    aliases = {
        "app.core.logger": "app.platform.observability.logging",
        "app.core.load_prompt": "app.platform.ai.prompts",
        "app.utils.path_util": "app.shared.paths",
        "app.utils.sse_utils": "app.platform.runtime.sse",
        "app.utils.task_utils": "app.platform.runtime.task_progress",
        "app.utils.format_utils": "app.modules.ingestion.graph.formatting",
        "app.utils.escape_milvus_string_utils": "app.platform.vector_store.expressions",
        "app.utils.normalize_sparse_vector": "app.platform.vector_store.sparse",
        "app.utils.rate_limit_utils": "app.platform.runtime.rate_limit",
    }

    for legacy, canonical in aliases.items():
        assert importlib.import_module(legacy) is importlib.import_module(canonical)


def test_ingestion_legacy_paths_alias_owning_modules():
    assert importlib.import_module("app.import_process.page_attribution") is importlib.import_module(
        "app.modules.ingestion.page_attribution"
    )
