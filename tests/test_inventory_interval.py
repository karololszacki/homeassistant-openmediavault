"""Test for the filesystem inventory cache (inventory_interval).

``FileSystemMgmt.enumerateFilesystems`` makes OMV run a full ``blkid`` scan
server-side, which blocks in uninterruptible disk wait on a degrading disk.
The raw records are therefore refreshed at most once per
``CONF_INVENTORY_INTERVAL`` seconds.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.omv.const import (
    CONF_INVENTORY_INTERVAL,
    CONF_SMART_INTERVAL,
)
from custom_components.omv.coordinator import OMVDataUpdateCoordinator
from custom_components.omv.exceptions import OMVApiError


@pytest.mark.asyncio
async def test_filesystem_inventory_interval_caches_between_polls(hass, config_entry) -> None:
    """The filesystem RPC runs once per interval; in between the cache is reused."""
    patched_entry = config_entry.__class__(
        domain=config_entry.domain,
        title=config_entry.title,
        data=config_entry.data,
        options={CONF_INVENTORY_INTERVAL: 3600, CONF_SMART_INTERVAL: 3600},
    )
    patched_entry.add_to_hass(hass)
    api = Mock()
    api.base_url = "http://192.0.2.10:80"

    async def async_call(service, method, params=None, **kwargs):
        if method == "enumerateFilesystems" and service == "FileSystemMgmt":
            return [{"devicefile": "/dev/sda1", "type": "ext4", "uuid": "1234"}]
        return []

    api.async_call = AsyncMock(side_effect=async_call)
    coordinator = OMVDataUpdateCoordinator(hass, patched_entry, api, scan_interval=60)
    coordinator.omv_version = 8

    # First refresh: the inventory is fetched live.
    first = await coordinator._async_fetch_filesystem_inventory()
    fs_calls = [
        c
        for c in api.async_call.await_args_list
        if c.args[0] == "FileSystemMgmt" and c.args[1] == "enumerateFilesystems"
    ]
    assert len(fs_calls) == 1
    assert first[0]["uuid"] == "1234"

    api.async_call.reset_mock()

    # Second refresh within the interval: no inventory RPC at all, but the
    # cached payload is still served.
    second = await coordinator._async_fetch_filesystem_inventory()
    assert api.async_call.await_count == 0
    assert second == first


@pytest.mark.asyncio
async def test_filesystem_inventory_refreshes_after_interval(hass, config_entry) -> None:
    """Once the interval has passed the inventory is fetched again."""
    patched_entry = config_entry.__class__(
        domain=config_entry.domain,
        title=config_entry.title,
        data=config_entry.data,
        options={CONF_INVENTORY_INTERVAL: 60},
    )
    patched_entry.add_to_hass(hass)
    api = Mock()
    api.base_url = "http://192.0.2.10:80"

    async def async_call(service, method, params=None, **kwargs):
        if method == "enumerateFilesystems" and service == "FileSystemMgmt":
            return [{"devicefile": "/dev/sda1", "type": "ext4", "uuid": "1234"}]
        return []

    api.async_call = AsyncMock(side_effect=async_call)
    coordinator = OMVDataUpdateCoordinator(hass, patched_entry, api, scan_interval=60)
    coordinator.omv_version = 8

    await coordinator._async_fetch_filesystem_inventory()
    first_count = api.async_call.await_count

    # Pretend the interval has elapsed.
    coordinator._filesystem_inventory_last_poll = coordinator._filesystem_inventory_last_poll - 61
    await coordinator._async_fetch_filesystem_inventory()
    assert api.async_call.await_count > first_count


@pytest.mark.asyncio
async def test_filesystem_inventory_failure_is_not_cached(hass, config_entry) -> None:
    """A failed fetch must not be cached and must be reported as degraded.

    Otherwise the filesystems would disappear until the next refresh and the
    registry cleanup could prune them (Issue #118).
    """
    patched_entry = config_entry.__class__(
        domain=config_entry.domain,
        title=config_entry.title,
        data=config_entry.data,
        options={CONF_INVENTORY_INTERVAL: 60},
    )
    patched_entry.add_to_hass(hass)
    api = Mock()
    api.base_url = "http://192.0.2.10:80"

    async def async_call(service, method, params=None, **kwargs):
        raise OMVApiError("FileSystemMgmt.enumerateFilesystems unavailable")

    api.async_call = AsyncMock(side_effect=async_call)
    coordinator = OMVDataUpdateCoordinator(hass, patched_entry, api, scan_interval=60)
    coordinator.omv_version = 8

    assert await coordinator._async_fetch_filesystem_inventory() == []
    assert "filesystem" in coordinator.degraded_collections
    # Nothing was cached, so the next refresh retries the fetch.
    assert coordinator._filesystem_inventory_cache == []
