"""
Compatibility layer for legacy imports from ``sharepoint.folders``.

The actual Microsoft Graph / SharePoint implementation is centralized in
``upload.py``.  This module intentionally contains no second Client
implementation; it re-exports the names that the previous folders.py exposed
so existing imports keep working.
"""

from .upload import (
    GRAPH_BASE,
    Client,
    DEFAULT_PROJECT_SUBFOLDERS,
    DriveItem,
    DriveItemFolderFacet,
    GraphError,
    _item_path,
)

__all__ = [
    "GRAPH_BASE",
    "Client",
    "DEFAULT_PROJECT_SUBFOLDERS",
    "DriveItem",
    "DriveItemFolderFacet",
    "GraphError",
    "_item_path",
]
