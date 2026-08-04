from bson import ObjectId
from pymongo import DESCENDING

from app.clients import mongo_history_utils
from app.conf.rag_tuning_config import load_rag_tuning_config
from app.query_process.agent.nodes import node_answer_output, node_item_name_confirm
from app.utils.sse_utils import SSEEvent


def test_item_name_threshold_configuration_is_bounded(monkeypatch):
    """可调阈值必须限制在0~1，候选阈值也不能高于自动确认阈值。"""
    monkeypatch.setenv("RAG_ITEM_NAME_AUTO_CONFIRM_SCORE", "0.70")
    monkeypatch.setenv("RAG_ITEM_NAME_CANDIDATE_SCORE", "0.80")
    monkeypatch.setenv("RAG_ITEM_NAME_AUTO_CONFIRM_MARGIN", "1.50")

    config = load_rag_tuning_config()

    assert config.item_name_auto_confirm_score == 0.70
    assert config.item_name_candidate_score == 0.70
    assert config.item_name_auto_confirm_margin == 1.0


def test_explicit_model_extraction_uses_current_question():
    """当前问题中的两个明确型号都应被确定性提取，不依赖历史或LLM。"""
    assert node_item_name_confirm._extract_explicit_item_names(
        "请根据LJ2268/LJ2268W激光打印机手册说明使用方法"
    ) == ["LJ2268", "LJ2268W"]


def test_lj2268_low_vector_score_is_confirmed_by_model_token():
    """型号token一致时，向量绝对分数偏低也能映射到知识库标准名称。"""
    result = node_item_name_confirm.step_5_align_item_names(
        [
            {
                "extracted_name": "LJ2268",
                "matches": [
                    {"item_name": "LJ2268/LJ2268W激光打印机", "score": 0.682},
                ],
            }
        ]
    )

    assert result == {
        "confirmed_item_names": ["LJ2268/LJ2268W激光打印机"],
        "options": [],
    }


def test_explicit_pt770_rejects_unrelated_lj2268_candidate():
    """用户写明PT770后，不允许只凭0.682向量分推荐LJ2268。"""
    result = node_item_name_confirm.step_5_align_item_names(
        [
            {
                "extracted_name": "PT770",
                "matches": [
                    {"item_name": "LJ2268/LJ2268W激光打印机", "score": 0.682},
                ],
            }
        ]
    )

    assert result == {"confirmed_item_names": [], "options": []}


def test_affirmative_reply_confirms_only_one_pending_candidate():
    """“是的”只能确认上轮澄清消息中唯一的候选，并恢复澄清前的问题。"""
    history = [
        {"role": "user", "text": "这个打印机怎么安装？", "rewritten_query": "LJ2268打印机怎么安装？"},
        {
            "role": "assistant",
            "text": "您是想问以下哪个产品：LJ2268/LJ2268W激光打印机？请明确一下型号。",
            "item_names": ["LJ2268/LJ2268W激光打印机"],
        },
    ]

    assert node_item_name_confirm._resolve_pending_confirmation("是的", history) == (
        "LJ2268/LJ2268W激光打印机",
        "LJ2268打印机怎么安装？",
    )


def test_affirmative_reply_does_not_guess_among_multiple_candidates():
    """候选超过一个时，“是的”信息不足，系统不能擅自选择第一项。"""
    history = [
        {
            "role": "assistant",
            "text": "您是想问以下哪个产品：设备A、设备B？请明确一下型号。",
            "item_names": ["设备A", "设备B"],
        }
    ]

    assert node_item_name_confirm._resolve_pending_confirmation("是的", history) == (None, "")


def test_node_prefers_explicit_model_over_llm_history(monkeypatch):
    """当前问题含LJ2268时，型号提取LLM不应被调用，也就无法注入历史中的PT770。"""
    saved_messages = []

    monkeypatch.setattr(node_item_name_confirm, "get_recent_messages", lambda *_args, **_kwargs: [])

    def fake_save(*args, **kwargs):
        saved_messages.append((args, kwargs))
        return kwargs.get("message_id") or "507f1f77bcf86cd799439011"

    monkeypatch.setattr(node_item_name_confirm, "save_chat_message", fake_save)
    monkeypatch.setattr(
        node_item_name_confirm,
        "step_3_extract_info",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("明确型号不应调用LLM提取")),
    )
    monkeypatch.setattr(
        node_item_name_confirm,
        "step_4_vectorize_and_query",
        lambda names, _tenant: [
            {
                "extracted_name": name,
                "matches": [{"item_name": "LJ2268/LJ2268W激光打印机", "score": 0.68}],
            }
            for name in names
        ],
    )
    monkeypatch.setattr(node_item_name_confirm, "add_running_task", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(node_item_name_confirm, "add_done_task", lambda *_args, **_kwargs: None)

    result = node_item_name_confirm.node_item_name_confirm(
        {
            "session_id": "local:regression-session",
            "tenant_id": "local",
            "original_query": "请说明LJ2268的使用方法",
            "is_stream": False,
        }
    )

    assert result["item_names"] == ["LJ2268/LJ2268W激光打印机"]
    assert result["rewritten_query"] == "请说明LJ2268的使用方法"
    # 一次新增用户消息、一次更新同一用户消息；这里不会保存助手消息。
    assert len(saved_messages) == 2


def test_item_confirmation_step_writes_only_user_message(monkeypatch):
    """澄清答案由最终输出节点统一保存，型号确认节点只能更新当前用户消息。"""
    calls = []
    monkeypatch.setattr(node_item_name_confirm, "save_chat_message", lambda **kwargs: calls.append(kwargs) or "id")

    state = {
        "original_query": "打印机怎么使用？",
        "answer": "请明确型号",
        "item_names": [],
    }
    node_item_name_confirm.step_7_write_history(state, "session", "打印机怎么使用？", "message-id")

    assert len(calls) == 1
    assert calls[0]["role"] == "user"
    assert calls[0]["message_id"] == "message-id"


def test_answer_history_saves_pending_candidates(monkeypatch):
    """澄清助手消息要保存候选型号，供下一轮简短肯定回复使用。"""
    calls = []
    monkeypatch.setattr(node_answer_output, "save_chat_message", lambda **kwargs: calls.append(kwargs) or "id")

    node_answer_output.step_4_write_history(
        {
            "session_id": "session",
            "answer": "您是想问以下哪个产品：设备A？请明确一下型号。",
            "item_names": [],
            "pending_item_names": ["设备A"],
            "trace_id": "trace",
        },
        [],
    )

    assert calls[0]["item_names"] == ["设备A"]


def test_answer_node_no_longer_sends_final_before_node_completion(monkeypatch):
    """答案节点只发delta；final必须由服务层在全部节点完成后统一发送。"""
    events = []
    monkeypatch.setattr(node_answer_output, "push_to_session", lambda session, event, data: events.append((event, data)))
    monkeypatch.setattr(node_answer_output, "resolve_object_urls", lambda values: values)
    monkeypatch.setattr(node_answer_output, "step_4_write_history", lambda state, _refs: state)
    monkeypatch.setattr(node_answer_output, "add_running_task", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(node_answer_output, "add_done_task", lambda *_args, **_kwargs: None)

    node_answer_output.node_answer_output(
        {
            "session_id": "session",
            "answer": "已找到答案",
            "is_stream": True,
        }
    )

    assert events == [(SSEEvent.DELTA, {"delta": "已找到答案"})]


class _FakeCursor:
    """只实现本测试需要的PyMongo游标排序和limit行为。"""

    def __init__(self, messages):
        self.messages = list(messages)

    def sort(self, key, direction):
        assert key == "ts"
        assert direction == DESCENDING
        self.messages.sort(key=lambda message: message[key], reverse=True)
        return self

    def limit(self, limit):
        self.messages = self.messages[:limit]
        return self

    def __iter__(self):
        return iter(self.messages)


class _FakeCollection:
    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.updated = None

    def find(self, query):
        return _FakeCursor(message for message in self.messages if message["session_id"] == query["session_id"])

    def update_one(self, query, update):
        self.updated = (query, update)
        return object()


def test_recent_history_returns_latest_n_in_chronological_order(monkeypatch):
    """会话较长时应取最新N条，再按旧到新提供给LLM。"""
    collection = _FakeCollection(
        [{"session_id": "session", "role": "user", "text": str(ts), "ts": ts} for ts in range(1, 6)]
    )
    monkeypatch.setattr(
        mongo_history_utils,
        "get_history_mongo_tool",
        lambda: type("FakeMongo", (), {"chat_message": collection})(),
    )

    messages = mongo_history_utils.get_recent_messages("session", limit=3)

    assert [message["ts"] for message in messages] == [3, 4, 5]


def test_updating_existing_message_preserves_original_timestamp(monkeypatch):
    """补充用户消息字段时不能重写ts，否则历史会变成assistant在user之前。"""
    collection = _FakeCollection()
    monkeypatch.setattr(
        mongo_history_utils,
        "get_history_mongo_tool",
        lambda: type("FakeMongo", (), {"chat_message": collection})(),
    )
    message_id = str(ObjectId())

    mongo_history_utils.save_chat_message(
        session_id="session",
        role="user",
        text="LJ2268怎么使用？",
        rewritten_query="LJ2268怎么使用？",
        item_names=["LJ2268/LJ2268W激光打印机"],
        message_id=message_id,
    )

    _, update = collection.updated
    assert "ts" not in update["$set"]
