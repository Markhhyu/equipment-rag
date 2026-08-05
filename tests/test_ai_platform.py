from types import SimpleNamespace

import pytest

import app.platform.ai.chat as chat
from app.platform.ai.embeddings import generate_embeddings
from app.platform.ai.prompts import load_prompt
from app.platform.ai.reranking.base import convert_scores_to_list
from app.platform.ai.reranking.factory import get_reranker_info
from app.shared.paths import PROJECT_ROOT


def test_chat_client_cache_reuses_equivalent_configuration(monkeypatch):
    created = []

    class FakeChatClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(self)

    monkeypatch.setattr(
        chat,
        "lm_config",
        SimpleNamespace(
            llm_model="test-chat-model",
            llm_temperature=0.1,
            api_key="test-key",
            base_url="https://models.example.test/v1",
        ),
    )
    monkeypatch.setattr(chat, "ChatOpenAI", FakeChatClient)
    chat._llm_client_cache.clear()

    first = chat.get_llm_client(timeout_seconds=15, max_retries=1)
    second = chat.get_llm_client(timeout_seconds=15, max_retries=1)

    assert first is second
    assert len(created) == 1
    assert first.kwargs["model"] == "test-chat-model"
    assert first.kwargs["timeout"] == 15.0
    chat._llm_client_cache.clear()


@pytest.mark.parametrize("texts", [None, "single text", []])
def test_embeddings_reject_invalid_input_before_loading_model(texts):
    with pytest.raises(ValueError, match="非空列表"):
        generate_embeddings(texts)


def test_reranker_helpers_do_not_load_a_model():
    assert convert_scores_to_list(0.5) == [0.5]
    assert convert_scores_to_list([1, 2.5]) == [1.0, 2.5]

    info = get_reranker_info()
    assert info["reranker_provider"] in {"bge", "qwen"}
    assert info["reranker_model"]


@pytest.mark.parametrize(
    ("name", "kwargs", "expected"),
    [
        (
            "answer_out",
            {
                "context": "[1] 测试手册证据",
                "history": "无历史对话",
                "item_names": "TEST-100设备",
                "question": "如何启动TEST-100？",
                "image_section": "本轮没有图片分析补充信息。",
            },
            "企业批准 SOP > 厂商手册",
        ),
        (
            "rewritten_query_and_itemnames",
            {"history_text": "用户: 设备报警", "query": "固件V2.1出现E101"},
            '"item_names"',
        ),
        ("hyde_prompt", {"rewritten_query": "TEST-100出现E101"}, "用于向量检索"),
        (
            "item_name_recognition",
            {"file_title": "TEST-100手册", "context": "TEST-100设备操作说明"},
            "主要设备名称与型号",
        ),
        ("product_recognition_system", {}, "企业设备文档"),
        ("query_rewrite_system", {}, "查询理解器"),
        (
            "image_enrichment",
            {
                "document_name": "TEST-100手册",
                "page_text": "第8页",
                "base_description": "设备面板",
                "context_before": "启动步骤",
                "context_after": "停止步骤",
            },
            "图片本身是事实依据",
        ),
        ("query_image_reasoning", {"question": "图中哪个是复位按钮？"}, "无法从图片确认"),
    ],
)
def test_active_prompt_templates_render(name, kwargs, expected):
    prompt = load_prompt(name, **kwargs)

    assert prompt.strip()
    assert expected in prompt


def test_prompt_inventory_contains_only_active_templates():
    prompt_names = {path.stem for path in (PROJECT_ROOT / "prompts").glob("*.prompt")}

    assert prompt_names == {
        "answer_out",
        "hyde_prompt",
        "image_enrichment",
        "item_name_recognition",
        "product_recognition_system",
        "query_image_reasoning",
        "query_rewrite_system",
        "rewritten_query_and_itemnames",
    }
