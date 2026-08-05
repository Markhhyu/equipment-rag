"""Deprecated compatibility imports for the image enrichment worker."""

from app.workers.image_enrichment import start_image_enrichment_worker, stop_image_enrichment_worker

__all__ = ["start_image_enrichment_worker", "stop_image_enrichment_worker"]
