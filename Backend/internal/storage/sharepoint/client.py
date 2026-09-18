"""Compatibility wrapper for the canonical SharePoint client in upload.py."""

from .upload import (
    Client,
    DriveItem,
    DriveItemFolderFacet,
    GraphError,
)

__all__ = [
    "Client",
    "DriveItem",
    "DriveItemFolderFacet",
    "GraphError",
]
