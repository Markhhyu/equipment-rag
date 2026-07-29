from pymilvus.model.hybrid import BGEM3EmbeddingFunction


def main() -> None:
    embedding_function = BGEM3EmbeddingFunction(
        model_name="BAAI/bge-m3",
        device="cpu",
        use_fp16=False,
        return_dense=True,
        return_sparse=True,
        return_colbert_vecs=False
    )

    documents = [
        "设备报警后，应按照设备SOP检查报警代码和相关部件。",
        "MinIO用于保存设备手册、维修记录和故障附件。"
    ]

    embeddings = embedding_function.encode_documents(documents)

    print("返回类型：", embeddings.keys())
    print("稠密向量数量：", len(embeddings["dense"]))
    print("稠密向量维度：", embeddings["dense"][0].shape)
    print("稀疏向量形状：", embeddings["sparse"].shape)


if __name__ == "__main__":
    main()