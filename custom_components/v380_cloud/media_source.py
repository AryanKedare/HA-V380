"""Browse V380 cloud recordings through Home Assistant's Media browser."""

from __future__ import annotations

from datetime import date
from urllib.parse import quote, unquote

from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant

from . import V380RuntimeData
from .api import AuthError, CloudError, UnsupportedError
from .const import DOMAIN, NAME
from .errors import LegacyHttpRequired


async def async_get_media_source(hass: HomeAssistant) -> V380MediaSource:
    """Register the recordings source; accounts are looked up on each request."""
    return V380MediaSource(hass)


def _identifier(*parts: str) -> str:
    return "/".join(quote(part, safe="") for part in parts)


def _directory(
    identifier: str | None, title: str, children: list[BrowseMediaSource] | None = None
) -> BrowseMediaSource:
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=identifier,
        media_class=MediaClass.DIRECTORY,
        media_content_type="",
        title=title,
        can_play=False,
        can_expand=True,
        children=children,
    )


class V380MediaSource(MediaSource):
    """Resolve opaque recording IDs to fresh cloud playback URLs."""

    name = "V380 Cloud recordings"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    def _runtime(self, entry_id: str) -> V380RuntimeData:
        if runtime := self.hass.data.get(DOMAIN, {}).get(entry_id):
            return runtime
        raise BrowseError(translation_domain=DOMAIN, translation_key="account_unavailable")

    def _auth_failed(self, entry_id: str) -> None:
        if entry := self.hass.config_entries.async_get_entry(entry_id):
            entry.async_start_reauth(self.hass)

    @staticmethod
    def _parts(identifier: str) -> list[str]:
        parts = [unquote(part) for part in identifier.split("/")]
        if (
            len(parts) not in (2, 4, 6, 8)
            or parts[0] != "account"
            or not parts[1]
            or (len(parts) >= 4 and (parts[2] != "camera" or not parts[3]))
            or (len(parts) >= 6 and parts[4] != "date")
            or (len(parts) == 8 and (parts[6] != "clip" or not parts[7]))
        ):
            raise ValueError("Invalid recording identifier")
        if len(parts) >= 6 and date.fromisoformat(parts[5]).isoformat() != parts[5]:
            raise ValueError("Invalid recording date")
        return parts

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse account, camera, date, then playable recording nodes."""
        if not item.identifier:
            accounts = []
            for entry_id in self.hass.data.get(DOMAIN, {}):
                if entry := self.hass.config_entries.async_get_entry(entry_id):
                    accounts.append(_directory(_identifier("account", entry_id), entry.title))
            return _directory(None, self.name, accounts)
        try:
            parts = self._parts(item.identifier)
        except ValueError as err:
            raise BrowseError(
                translation_domain=DOMAIN, translation_key="invalid_media"
            ) from err
        runtime = self._runtime(parts[1])
        try:
            return await self._async_browse(parts, runtime)
        except AuthError as err:
            self._auth_failed(parts[1])
            raise BrowseError(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except LegacyHttpRequired as err:
            raise BrowseError(
                translation_domain=DOMAIN, translation_key="legacy_http_required"
            ) from err
        except UnsupportedError as err:
            raise BrowseError(
                translation_domain=DOMAIN, translation_key="unsupported_recordings"
            ) from err
        except CloudError as err:
            raise BrowseError(
                translation_domain=DOMAIN, translation_key="cloud_unavailable"
            ) from err

    async def _async_browse(
        self, parts: list[str], runtime: V380RuntimeData
    ) -> BrowseMediaSource:
        if len(parts) == 2:
            entry = self.hass.config_entries.async_get_entry(parts[1])
            return _directory(
                _identifier(*parts),
                entry.title if entry else NAME,
                [
                    _directory(_identifier(*parts, "camera", device.device_id), device.name)
                    for device in runtime.coordinator.data.values()
                ],
            )
        if parts[3] not in runtime.coordinator.data:
            raise BrowseError(translation_domain=DOMAIN, translation_key="invalid_media")
        device = runtime.coordinator.data[parts[3]]
        if len(parts) == 4:
            dates = await runtime.client.async_recording_dates(device.device_id)
            # Invalid dates from the service are rejected rather than used in IDs.
            try:
                valid_dates = sorted(
                    {date.fromisoformat(day).isoformat() for day in dates}, reverse=True
                )
            except (ValueError, TypeError) as err:
                raise BrowseError(
                    translation_domain=DOMAIN, translation_key="cloud_unavailable"
                ) from err
            return _directory(
                _identifier(*parts),
                device.name,
                [_directory(_identifier(*parts, "date", day), day) for day in valid_dates],
            )
        recordings = await runtime.client.async_recordings(device.device_id, parts[5])
        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=_identifier(*parts[:6], "clip", recording.recording_id),
                media_class=MediaClass.VIDEO,
                media_content_type=recording.mime_type,
                title=recording.title,
                can_play=True,
                can_expand=False,
            )
            for recording in recordings
        ]
        if len(parts) == 8:
            for child, recording in zip(children, recordings, strict=True):
                if recording.recording_id == parts[7]:
                    return child
            raise BrowseError(translation_domain=DOMAIN, translation_key="recording_missing")
        return _directory(_identifier(*parts), f"{device.name} — {parts[5]}", children)

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Ask the service for a playable URL only when a clip is selected."""
        try:
            parts = self._parts(item.identifier)
            if len(parts) != 8:
                raise ValueError("A recording must be selected")
            runtime = self._runtime(parts[1])
            if parts[3] not in runtime.coordinator.data:
                raise ValueError("Unknown camera")
            recordings = await runtime.client.async_recordings(parts[3], parts[5])
            recording = next(
                (recording for recording in recordings if recording.recording_id == parts[7]),
                None,
            )
            if recording is None:
                raise Unresolvable(
                    translation_domain=DOMAIN, translation_key="recording_missing"
                )
            source = await runtime.client.async_recording_source(parts[3], parts[7])
            return PlayMedia(source, recording.mime_type)
        except AuthError as err:
            self._auth_failed(parts[1])
            raise Unresolvable(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except LegacyHttpRequired as err:
            raise Unresolvable(
                translation_domain=DOMAIN, translation_key="legacy_http_required"
            ) from err
        except UnsupportedError as err:
            raise Unresolvable(
                translation_domain=DOMAIN, translation_key="unsupported_recordings"
            ) from err
        except CloudError as err:
            raise Unresolvable(
                translation_domain=DOMAIN, translation_key="cloud_unavailable"
            ) from err
        except (ValueError, BrowseError) as err:
            raise Unresolvable(
                translation_domain=DOMAIN, translation_key="invalid_media"
            ) from err

