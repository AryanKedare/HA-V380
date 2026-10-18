"""Expose only aggregate, non-secret diagnostics."""

from homeassistant.core import HomeAssistant

from . import V380ConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: V380ConfigEntry
) -> dict[str, object]:
    """Exclude credentials, vendor IDs, raw responses, and playback URLs."""
    coordinator = entry.runtime_data.coordinator
    return {
        "experimental": True,
        "camera_count": len(coordinator.data),
        "online_camera_count": sum(device.online is True for device in coordinator.data.values()),
        "offline_camera_count": sum(device.online is False for device in coordinator.data.values()),
        "unknown_status_camera_count": sum(device.online is None for device in coordinator.data.values()),
        "last_update_success": coordinator.last_update_success,
    }

