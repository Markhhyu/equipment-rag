import math

from app.model.reranker.factory import get_reranker, get_reranker_info


def main():
    """
    验证当前配置的Reranker能否正常加载和计算分数。

    该测试只验证模型运行，不代表完整的检索效果评测。
    """

    # 同时包含英文、中文查询英文文档和无关文档。
    sentence_pairs = [
        [
            "How should an abnormal LPE process chamber pressure alarm be handled?",
            "When process chamber pressure is abnormal, check the vacuum pump, pressure sensor and gas valves."
        ],
        [
            "LPE设备生长腔压力异常如何处理？",
            "When process chamber pressure is abnormal, verify the vacuum pump status and inspect the pressure sensor."
        ],
        [
            "LPE设备生长腔压力异常如何处理？",
            "The wafer diameter is measured before the packaging process."
        ]
    ]

    reranker = get_reranker()
    scores = reranker.compute_score(sentence_pairs)

    print("当前Reranker配置：", get_reranker_info())
    print("模型运行信息：", reranker.get_info())

    for index, score in enumerate(scores, start=1):
        print(f"候选文档{index}分数：{score:.6f}")

    # 基础校验：返回数量必须与输入数量相同。
    if len(scores) != len(sentence_pairs):
        raise RuntimeError(f"分数数量错误，期望{len(sentence_pairs)}，实际{len(scores)}")

    # 基础校验：所有分数必须是有效数字。
    if not all(math.isfinite(score) for score in scores):
        raise RuntimeError(f"存在无效分数：{scores}")

    print("Reranker Provider冒烟测试通过。")


if __name__ == "__main__":
    main()