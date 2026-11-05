"""Expose only aggregate, non-secret diagnostics."""

from homeassistant.core import HomeAssistant

from . import V380ConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: V380ConfigEntry
) -> dict[str, object]:
    """Exclude credentials, vendor IDs, raw responses, and playback URLs."""
    coordinator = entry.runtime_data.coordinator
    client = entry.runtime_data.client
    catalog = {
        "device_list_complete": getattr(client, "device_list_complete", False) is True,
        "last_device_list_result": getattr(client, "last_device_list_result", None),
        "partial_device_list_responses": getattr(client, "partial_device_list_responses", 0),
        "device_removal_checks": getattr(client, "device_removal_checks", 0),
        "empty_catalog_recoveries": getattr(client, "empty_catalog_recoveries", 0),
    }
    return {
        "experimental": True,
        "camera_count": len(coordinator.data),
        "online_camera_count": sum(device.online is True for device in coordinator.data.values()),
        "offline_camera_count": sum(device.online is False for device in coordinator.data.values()),
        "unknown_status_camera_count": sum(device.online is None for device in coordinator.data.values()),
        "last_update_success": coordinator.last_update_success,
        "device_catalog": catalog,
        "live_stream_failures": getattr(client, "live_stream_failures", 0),
        "last_live_failure": dict(getattr(client, "last_live_failure", {})),
    }

