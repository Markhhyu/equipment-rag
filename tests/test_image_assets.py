import time
from concurrent.futures import ThreadPoolExecutor

import app.modules.knowledge.infrastructure.image_assets as image_assets


def test_image_asset_store_is_initialized_once_across_worker_threads(monkeypatch):
    created = []

    class FakeImageAssetStore:
        def __init__(self):
            created.append(self)
            time.sleep(0.02)

    monkeypatch.setattr(image_assets, "_image_asset_tool", None)
    monkeypatch.setattr(image_assets, "ImageAssetMongoTool", FakeImageAssetStore)

    with ThreadPoolExecutor(max_workers=8) as executor:
        stores = list(executor.map(lambda _: image_assets.get_image_asset_tool(), range(8)))

    assert len(created) == 1
    assert all(store is stores[0] for store in stores)
