"""Helpers for look-based workflows."""

from __future__ import annotations

import re
from typing import Optional

import bpy

from .constants import AYON_PROPERTY


CBID_ATTR_NAMES = ("cbId", "cbid", "CbId", "CBID")


def get_cbid_from_node(node: bpy.types.ID) -> Optional[str]:
    """Return cbId stored on an object or its data block."""

    if node is None:
        return None

    value = _get_cbid_from_idprops(node)
    if value is not None:
        return value

    # Try data block (e.g. mesh data) when available
    data_block = getattr(node, "data", None)
    value = _get_cbid_from_idprops(data_block)
    if value is not None:
        return value

    # Finally try data stored in AYON metadata
    ayon_prop = node.get(AYON_PROPERTY) if hasattr(node, "get") else None
    value = _get_cbid_from_mapping(ayon_prop)
    if value is not None:
        return value

    return None


def _get_cbid_from_idprops(node: Optional[bpy.types.ID]) -> Optional[str]:
    if node is None or not hasattr(node, "keys"):
        return None
    return _get_cbid_from_mapping(node)


def _get_cbid_from_mapping(data: Optional[dict]) -> Optional[str]:
    if not data:
        return None
    for attr in CBID_ATTR_NAMES:
        if attr in data and data[attr] not in (None, "", 0):
            return str(data[attr])
    return None


def slugify_name(value: str) -> str:
    """Return filesystem friendly name for the current value."""

    if not value:
        return "look"

    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_\-]+", "_", value)
    value = re.sub(r"_+", "_", value)

    return value.strip("_") or "look"
