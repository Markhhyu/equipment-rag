"""MinerU document parser adapter."""

import json
import mimetypes
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

from app.platform.config.mineru_config import MineruConfig, mineru_config
from app.platform.observability.logging import logger


@dataclass(frozen=True)
class MineruParseResult:
    """
    MinerU文档解析结果。

    保存Markdown、结构化JSON、图片目录以及MinerU版本等信息。
    """

    task_id: str
    output_dir: str
    md_path: str
    content_list_path: str
    content_list_v2_path: str
    middle_json_path: str
    mineru_version: str
    api_protocol_version: str


class MineruClient:
    """
    MinerU 3.x异步任务API客户端。

    主要流程：
    1. 检查MinerU服务；
    2. 上传PDF并创建解析任务；
    3. 轮询任务状态；
    4. 下载ZIP解析结果；
    5. 解压并定位Markdown及结构化JSON。
    """

    def __init__(self, config: MineruConfig):
        self.config = config
        self.session = requests.Session()

        # 本地MinerU服务不经过系统代理，避免代理影响127.0.0.1访问。
        if self.config.base_url.startswith(("http://127.0.0.1", "http://localhost")):
            self.session.trust_env = False

    def _headers(self) -> dict[str, str]:
        """构造MinerU接口请求头。"""

        headers = {}

        # 本地部署通常不需要Token，配置后才发送鉴权头。
        if self.config.api_token:
            headers["Authorization"] = f"Bearer {self.config.api_token}"

        return headers

    @staticmethod
    def _unwrap_response(data: dict) -> dict:
        """
        兼容不同MinerU接口响应格式。

        有些接口直接返回数据，有些接口将数据放在data节点中。
        """

        inner_data = data.get("data")

        if isinstance(inner_data, dict):
            return {**data, **inner_data}

        return data

    @staticmethod
    def _bool_text(value: bool) -> str:
        """将Python布尔值转换为接口需要的小写字符串。"""

        return "true" if value else "false"

    def _build_form_data(self) -> list[tuple[str, str]]:
        """
        根据MinerU后端构造任务提交参数。

        规则：
        1. pipeline和hybrid支持parse_method；
        2. pipeline和hybrid可接收OCR语言；
        3. effort仅对hybrid生效；
        4. image_analysis仅对hybrid和vlm生效。
        """

        backend = self.config.backend

        # 所有后端共用参数。
        data = [
            ("backend", backend),
            ("formula_enable", self._bool_text(self.config.formula_enable)),
            ("table_enable", self._bool_text(self.config.table_enable)),
            ("return_md", "true"),
            ("return_middle_json", self._bool_text(self.config.return_middle_json)),
            ("return_content_list", self._bool_text(self.config.return_content_list)),
            ("return_model_output", "false"),
            ("return_images", "true"),
            ("response_format_zip", "true"),
            ("return_original_file", "false"),
            ("start_page_id", "0")
        ]

        # pipeline和hybrid支持文本提取、OCR模式及OCR语言配置。
        if backend == "pipeline" or backend.startswith("hybrid"):
            data.append(("parse_method", self.config.parse_method))
            data.append(("lang_list", self.config.language))

        # effort只对hybrid后端生效。
        if backend.startswith("hybrid"):
            data.append(("effort", self.config.effort))

        # 图片和图表语义分析只适用于hybrid和vlm后端。
        if backend.startswith(("hybrid", "vlm")):
            data.append(("image_analysis", self._bool_text(self.config.image_analysis)))

        return data

    def health_check(self) -> dict:
        """检查MinerU API服务是否可用。"""

        url = f"{self.config.base_url}/health"

        try:
            response = self.session.get(
                url=url,
                headers=self._headers(),
                timeout=self.config.request_timeout_seconds,
                verify=self.config.verify_ssl
            )
            response.raise_for_status()

            result = self._unwrap_response(response.json())
            logger.info(f"MinerU服务连接成功，base_url={self.config.base_url}")
            return result

        except Exception as e:
            raise RuntimeError(f"MinerU服务连接失败，请检查服务是否已启动：{url}，错误：{e}") from e

    def _submit_task(self, file_path: Path) -> str:
        """上传文档并创建MinerU异步解析任务。"""

        url = f"{self.config.base_url}/tasks"
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

        backend_info = f"backend={self.config.backend}"

        if self.config.backend.startswith("hybrid"):
            backend_info += f"，effort={self.config.effort}"

        logger.info(f"开始提交MinerU解析任务，file={file_path.name}，{backend_info}")

        with file_path.open("rb") as file:
            files = {"files": (file_path.name, file, content_type)}

            response = self.session.post(
                url=url,
                headers=self._headers(),
                files=files,
                data=self._build_form_data(),
                timeout=self.config.request_timeout_seconds,
                verify=self.config.verify_ssl
            )

        if response.status_code >= 400:
            raise RuntimeError(
                f"MinerU任务提交失败，status={response.status_code}，"
                f"response={response.text[:1000]}"
            )

        result = self._unwrap_response(response.json())
        task_id = result.get("task_id")

        if not task_id:
            raise RuntimeError(f"MinerU任务提交成功但未返回task_id：{result}")

        logger.info(
            f"MinerU任务提交成功，task_id={task_id}，"
            f"queued_ahead={result.get('queued_ahead')}"
        )

        return str(task_id)

    def _wait_task(self, task_id: str) -> None:
        """轮询MinerU任务，直到完成、失败或超时。"""

        url = f"{self.config.base_url}/tasks/{task_id}"
        start_time = time.time()
        last_status = ""

        while True:
            elapsed_seconds = int(time.time() - start_time)

            if elapsed_seconds > self.config.task_timeout_seconds:
                raise TimeoutError(
                    f"MinerU解析超时，task_id={task_id}，"
                    f"elapsed={elapsed_seconds}s"
                )

            try:
                response = self.session.get(
                    url=url,
                    headers=self._headers(),
                    timeout=self.config.request_timeout_seconds,
                    verify=self.config.verify_ssl
                )
                response.raise_for_status()
                result = self._unwrap_response(response.json())

            except Exception as e:
                logger.warning(
                    f"MinerU任务状态查询失败，"
                    f"{self.config.poll_interval_seconds}秒后重试：{e}"
                )
                time.sleep(self.config.poll_interval_seconds)
                continue

            status = str(result.get("status") or result.get("state") or "").strip().lower()

            if status != last_status:
                logger.info(
                    f"MinerU任务状态更新，task_id={task_id}，status={status}，"
                    f"queued_ahead={result.get('queued_ahead')}，"
                    f"elapsed={elapsed_seconds}s"
                )
                last_status = status

            if status in {"completed", "done", "success", "succeeded"}:
                return

            if status in {"failed", "error", "cancelled", "canceled"}:
                error = result.get("error") or result.get("message") or result.get("detail") or "未知错误"
                raise RuntimeError(f"MinerU解析失败，task_id={task_id}，error={error}")

            time.sleep(self.config.poll_interval_seconds)

    def _download_result(self, task_id: str, temp_dir: Path) -> Path:
        """下载MinerU解析结果ZIP文件。"""

        url = f"{self.config.base_url}/tasks/{task_id}/result"
        temp_dir.mkdir(parents=True, exist_ok=True)

        zip_path = temp_dir / f"{task_id}.zip"

        response = self.session.get(
            url=url,
            headers=self._headers(),
            timeout=self.config.download_timeout_seconds,
            verify=self.config.verify_ssl,
            stream=True
        )

        if response.status_code >= 400:
            raise RuntimeError(
                f"MinerU结果下载失败，status={response.status_code}，"
                f"response={response.text[:1000]}"
            )

        with zip_path.open("wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file.write(chunk)

        # ZIP文件通常以PK开头，防止接口返回错误JSON或HTML。
        with zip_path.open("rb") as file:
            if file.read(2) != b"PK":
                preview = zip_path.read_text(encoding="utf-8", errors="ignore")[:1000]
                zip_path.unlink(missing_ok=True)
                raise RuntimeError(f"MinerU返回内容不是ZIP文件：{preview}")

        logger.info(f"MinerU结果下载完成，task_id={task_id}，zip_path={zip_path}")
        return zip_path

    @staticmethod
    def _safe_extract(zip_path: Path, output_dir: Path) -> None:
        """安全解压ZIP文件，防止路径穿越。"""

        output_root = output_dir.resolve()

        with zipfile.ZipFile(zip_path, "r") as zip_file:
            for member in zip_file.infolist():
                target_path = (output_dir / member.filename).resolve()

                if target_path != output_root and output_root not in target_path.parents:
                    raise RuntimeError(f"MinerU结果ZIP包含非法路径：{member.filename}")

            zip_file.extractall(output_dir)

    @staticmethod
    def _find_markdown(output_dir: Path, file_stem: str) -> Path:
        """从解析结果中查找主要Markdown文件。"""

        md_files = list(output_dir.rglob("*.md"))

        if not md_files:
            raise FileNotFoundError(f"MinerU结果中未找到Markdown文件：{output_dir}")

        # 优先使用与原文件同名的Markdown。
        for md_file in md_files:
            if md_file.stem == file_stem:
                return md_file

        # 兼容部分版本生成的full.md。
        for md_file in md_files:
            if md_file.name.lower() == "full.md":
                return md_file

        # 多个Markdown文件时，优先选择内容最大的文件。
        return max(md_files, key=lambda path: path.stat().st_size)

    @staticmethod
    def _find_optional_file(output_dir: Path, pattern: str) -> str:
        """按通配符查找可选输出文件。"""

        files = list(output_dir.rglob(pattern))
        return str(files[0].resolve()) if files else ""

    @staticmethod
    def _read_mineru_version(middle_json_path: str) -> str:
        """从middle.json中读取MinerU版本。"""

        if not middle_json_path:
            return ""

        try:
            with open(middle_json_path, "r", encoding="utf-8") as file:
                data = json.load(file)

            return str(data.get("_version_name") or "")

        except Exception as e:
            logger.warning(f"读取MinerU版本失败：{e}")
            return ""

    def parse_file(self, file_path: str, output_root: str) -> MineruParseResult:
        """
        执行完整的MinerU文件解析流程。

        :param file_path: 原始PDF路径
        :param output_root: 解析结果输出根目录
        :return: MinerU解析结果对象
        """

        input_path = Path(file_path).resolve()
        root_dir = Path(output_root).resolve()

        if not input_path.exists() or not input_path.is_file():
            raise FileNotFoundError(f"待解析文件不存在：{input_path}")

        root_dir.mkdir(parents=True, exist_ok=True)

        # 获取API协议版本，同时确认服务正常。
        health_result = self.health_check()
        api_protocol_version = str(health_result.get("protocol_version") or "")

        # 创建任务并等待任务完成。
        task_id = self._submit_task(input_path)
        self._wait_task(task_id)

        # 下载ZIP结果。
        temp_dir = root_dir / ".mineru_temp"
        zip_path = self._download_result(task_id, temp_dir)

        # 每份文档使用独立结果目录。
        extract_dir = root_dir / input_path.stem

        if extract_dir.exists():
            shutil.rmtree(extract_dir)

        extract_dir.mkdir(parents=True, exist_ok=True)

        try:
            self._safe_extract(zip_path, extract_dir)
        finally:
            zip_path.unlink(missing_ok=True)

        # 定位Markdown及结构化JSON。
        md_path = self._find_markdown(extract_dir, input_path.stem)
        middle_json_path = self._find_optional_file(extract_dir, "*_middle.json")

        # 稳定版content_list，不选择content_list_v2。
        content_list_files = [
            file for file in extract_dir.rglob("*_content_list.json")
            if not file.name.endswith("_content_list_v2.json")
        ]

        content_list_path = str(content_list_files[0].resolve()) if content_list_files else ""
        content_list_v2_path = self._find_optional_file(extract_dir, "*_content_list_v2.json")
        mineru_version = self._read_mineru_version(middle_json_path)

        logger.info(
            f"MinerU解析结果整理完成，task_id={task_id}，"
            f"md={md_path.name}，version={mineru_version}"
        )

        return MineruParseResult(
            task_id=task_id,
            output_dir=str(extract_dir),
            md_path=str(md_path.resolve()),
            content_list_path=content_list_path,
            content_list_v2_path=content_list_v2_path,
            middle_json_path=middle_json_path,
            mineru_version=mineru_version,
            api_protocol_version=api_protocol_version
        )


# 以下代码必须位于MineruClient类外部，行首不能有空格。

# MinerU客户端全局单例，复用HTTP Session连接。
_mineru_client: MineruClient | None = None


def get_mineru_client() -> MineruClient:
    """
    获取MinerU客户端单例。

    第一次调用时创建客户端，后续直接复用。
    """

    global _mineru_client

    if _mineru_client is None:
        _mineru_client = MineruClient(mineru_config)

    return _mineru_client
