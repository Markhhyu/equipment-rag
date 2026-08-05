"""Public application access to knowledge document image assets."""

from app.modules.knowledge.infrastructure.image_assets import ImageAssetMongoTool, get_image_asset_tool

__all__ = ["ImageAssetMongoTool", "get_image_asset_tool"]
