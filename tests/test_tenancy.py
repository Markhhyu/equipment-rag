import pytest

from app.platform.security.config import reset_security_config_for_tests
from app.platform.security.tenancy import (
    public_session_id,
    safe_upload_filename,
    scoped_session_id,
    tenant_filter,
    tenant_object_prefix,
)
from app.platform.storage import minio as minio_utils


def test_session_keys_are_scoped_and_reversible_for_the_same_tenant():
    stored = scoped_session_id("tenant-a", "session-123")

    assert stored == "tenant-a:session-123"
    assert public_session_id("tenant-a", stored) == "session-123"
    with pytest.raises(ValueError, match="does not belong"):
        public_session_id("tenant-b", stored)


def test_upload_filename_removes_paths_and_enforces_allowlist():
    allowed = frozenset({".pdf", ".md"})

    assert safe_upload_filename("../../manual.pdf", allowed) == "manual.pdf"
    assert safe_upload_filename(r"..\manual.md", allowed) == "manual.md"
    with pytest.raises(ValueError, match="Unsupported"):
        safe_upload_filename("payload.exe", allowed)


def test_tenant_object_and_milvus_filters_are_namespaced():
    assert (
        tenant_object_prefix("tenant-a", "pdf_files", "20260730", "manual.pdf")
        == "tenants/tenant-a/pdf_files/20260730/manual.pdf"
    )
    expression = tenant_filter("tenant-a", 'item_name == "meter"')
    assert expression == '(tenant_id == "tenant-a") and (item_name == "meter")'


def test_invalid_session_id_is_rejected():
    with pytest.raises(ValueError, match="unsupported"):
        scoped_session_id("tenant-a", "../other-session")


def test_private_minio_reference_is_resolved_only_for_the_response(monkeypatch):
    class FakePublicClient:
        def presigned_get_object(self, bucket_name, object_name, expires):
            assert bucket_name == "equipment-rag"
            assert object_name == "tenants/tenant-a/images/manual/panel 1.png"
            assert expires.total_seconds() == 3600
            return "https://objects.example.test/signed"

    monkeypatch.setenv("AUTH_MODE", "disabled")
    monkeypatch.setenv("MINIO_PUBLIC_READ", "false")
    reset_security_config_for_tests()
    monkeypatch.setattr(minio_utils, "minio_client", object())
    monkeypatch.setattr(minio_utils, "minio_public_client", FakePublicClient())
    reference = minio_utils.minio_object_uri(
        "equipment-rag",
        "tenants/tenant-a/images/manual/panel 1.png",
    )

    assert reference.endswith("panel%201.png")
    assert minio_utils.resolve_object_url(reference) == "https://objects.example.test/signed"
    reset_security_config_for_tests()
