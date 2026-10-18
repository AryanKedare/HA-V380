"""Normalize data_share using the APK's DeviceManager.parseShareDevice.

Inner legacy password is clear text after Base64/JSON decode.
Encode it once to match account.device_password's ordinary ver=0 representation.
"""
from __future__ import annotations

import base64
from collections.abc import Mapping
import json
from typing import Any


def normalize_shared_device(item: Mapping[str, Any]) -> dict[str, Any]:
    """Return owned-style metadata for a ShareNewsResponse$ShareNewBean row.

    Raise ValueError for malformed shared credential records. The Android method
    catches such parse failures and returns null; callers can skip malformed
    shared rows without discarding otherwise valid owned devices.

    No status filtering is introduced: the inspected device/list handler calls
    parseShareDevice for every data_share row. Other sharing UI status handling
    is outside this method's evidence boundary.
    """
    uid = item.get("device_id")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise ValueError("Shared camera has an invalid device ID")
    encoded = item.get("str")
    inner: dict[str, Any] = {}
    if encoded:
        if not isinstance(encoded, str) or len(encoded) > 1024 * 1024:
            raise ValueError("Shared camera credential encoding is unsupported")
        try:
            # Android Base64.DEFAULT accepts line-wrapped input.
            compact = "".join(encoded.split())
            data = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True).decode("utf-8")
            inner = json.loads(data)
        except (ValueError, UnicodeError) as err:
            raise ValueError("Shared camera credential encoding is unsupported") from err
        if not isinstance(inner, dict):
            raise ValueError("Shared camera credential record is not an object")
    username = inner.get("username", "admin")
    password = inner.get("password", "")
    nickname = item.get("nickname") or inner.get("nickname") or str(uid)
    if not isinstance(username, str) or not isinstance(password, str) or not isinstance(nickname, str):
        raise ValueError("Shared camera credential record contains invalid strings")
    metadata = dict(item)
    metadata.pop("str", None)
    metadata["shared"] = True
    metadata["nickname"] = nickname
    metadata["device_port"] = 8800
    metadata["ver"] = 0
    if all(key in inner for key in ("public_key", "permission", "model")):
        permission = inner["permission"]
        if isinstance(permission, str):
            try:
                permission = int(permission)
            except ValueError as err:
                raise ValueError("Shared camera key-exchange permission is invalid") from err
        if not isinstance(inner["public_key"], str) or not isinstance(inner["model"], str) or not isinstance(permission, int) or isinstance(permission, bool):
            raise ValueError("Shared camera key-exchange record is invalid")
        metadata.update(type=2, device_account="", device_password="", public_key=inner["public_key"], permission=permission, model=inner["model"])
    else:
        metadata.update(type=0, device_account=username, device_password=base64.b64encode(password.encode("utf-8")).decode("ascii"))
    return metadata
