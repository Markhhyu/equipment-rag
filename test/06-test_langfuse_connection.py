from dotenv import load_dotenv

load_dotenv()

from langfuse import get_client


def main():
    langfuse = get_client()

    if not langfuse.auth_check():
        raise RuntimeError("Langfuse认证失败，请检查Public Key、Secret Key和Base URL")

    with langfuse.start_as_current_observation(
            as_type="span",
            name="equipment-rag-connection-test"
    ) as span:
        span.update(
            input={"message": "测试Langfuse连接"},
            output={"status": "success"},
            metadata={
                "project": "equipment-rag",
                "test_type": "connection"
            }
        )

    # 测试脚本运行结束较快，需要主动发送缓存中的Trace
    langfuse.flush()

    print("Langfuse连接成功，测试Trace已发送。")


if __name__ == "__main__":
    main()