from types import SimpleNamespace

import pytest

import app.platform.ai.chat as chat
from app.platform.ai.embeddings import generate_embeddings
from app.platform.ai.reranking.base import convert_scores_to_list
from app.platform.ai.reranking.factory import get_reranker_info


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
