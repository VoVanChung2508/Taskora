"""
Compatibility layer for legacy imports from ``sharepoint.paths``.

The canonical Microsoft Graph / SharePoint implementation lives in
``upload.py``.  This module keeps the old public names available without
maintaining another duplicated ``Client`` implementation.
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
