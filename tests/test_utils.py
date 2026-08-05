import json

import numpy as np
import pytest

from app.modules.ingestion.graph.formatting import format_json, format_state
from app.platform.vector_store.expressions import escape_milvus_string
from app.platform.vector_store.sparse import normalize_sparse_vector


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ('pump "A"', 'pump \\"A\\"'),
        ("line1\nline2\tvalue", "line1 line2 value"),
        (r"C:\equipment", r"C:\\equipment"),
        (42, "42"),
    ],
)
def test_escape_milvus_string(value, expected):
    assert escape_milvus_string(value) == expected


def test_normalize_sparse_vector_has_unit_norm():
    normalized = normalize_sparse_vector({1: 3.0, 4: 4.0})

    assert normalized[1] == pytest.approx(0.6)
    assert normalized[4] == pytest.approx(0.8)
    assert np.linalg.norm(list(normalized.values())) == pytest.approx(1.0)


@pytest.mark.parametrize("value", [{}, {1: 0.0}])
def test_normalize_sparse_vector_keeps_empty_or_zero_vectors(value):
    assert normalize_sparse_vector(value) == value


def test_json_formatters_preserve_unicode():
    data = {"equipment": "真空泵", "status": "正常"}

    assert json.loads(format_json(data)) == data
    assert json.loads(format_state(data, indent=2)) == data
