from __future__ import annotations

import json

from app.modules.ingestion.page_attribution import attach_pdf_page_numbers, load_mineru_pages


def test_content_list_page_idx_is_converted_to_physical_pdf_page(tmp_path):
    path = tmp_path / "manual_content_list.json"
    path.write_text(
        json.dumps(
            [
                {"type": "text", "text": "封面和产品名称", "page_idx": 0},
                {"type": "text", "text": "按下电源按钮启动设备", "page_idx": 7},
                {"type": "list", "list_items": ["等待状态灯变为绿色", "装入打印耗材"], "page_idx": 8},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    chunks = [
        {"content": "## 启动\n按下电源按钮启动设备"},
        {"content": "等待状态灯变为绿色，然后装入打印耗材"},
    ]

    pages = load_mineru_pages(str(path))
    assert set(pages) == {1, 8, 9}
    assert attach_pdf_page_numbers(chunks, str(path)) == 2
    assert chunks[0]["page_numbers"] == [8]
    assert chunks[1]["page_numbers"] == [9]
    assert chunks[1]["page_start"] == 9
    assert chunks[1]["page_end"] == 9
