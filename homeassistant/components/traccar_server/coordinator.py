"""Data update coordinator for Traccar Server."""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypedDict

from pytraccar import (
    ApiClient,
    DeviceModel,
    GeofenceModel,
    PositionModel,
    SubscriptionData,
    TraccarException,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EVENTS, LOGGER
from .helpers import get_device, get_first_geofence


class TraccarServerCoordinatorDataDevice(TypedDict):
    """Traccar Server coordinator data."""

    device: DeviceModel
    geofence: GeofenceModel | None
    position: PositionModel
    attributes: dict[str, Any]


TraccarServerCoordinatorData = dict[int, TraccarServerCoordinatorDataDevice]


class TraccarServerCoordinator(DataUpdateCoordinator[TraccarServerCoordinatorData]):
    """Class to manage fetching Traccar Server data."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: ApiClient,
        *,
        events: list[str],
        max_accuracy: float,
        skip_accuracy_filter_for: list[str],
        custom_attributes: list[str],
    ) -> None:
        """Initialize global Traccar Server data updater."""
        super().__init__(
            hass=hass,
            logger=LOGGER,
            name=DOMAIN,
            update_interval=None,
        )
        self.client = client
        self.custom_attributes = custom_attributes
        self.events = events
        self.max_accuracy = max_accuracy
        self.skip_accuracy_filter_for = skip_accuracy_filter_for
        self._geofences: list[GeofenceModel] = []
        self._last_event_import: datetime | None = None
        self._subscription: asyncio.Task | None = None
        self._should_log_subscription_error: bool = True

    async def _async_update_data(self) -> TraccarServerCoordinatorData:
        """Fetch data from Traccar Server."""
        LOGGER.debug("Updating device data")
        data: TraccarServerCoordinatorData = {}
        try:
            (
                devices,
                positions,
                geofences,
            ) = await asyncio.gather(
                self.client.get_devices(),
                self.client.get_positions(),
                self.client.get_geofences(),
            )
        except TraccarException as ex:
            raise UpdateFailed(f"Error while updating device data: {ex}") from ex

        if TYPE_CHECKING:
            assert isinstance(devices, list[DeviceModel])  # type: ignore[misc]
            assert isinstance(positions, list[PositionModel])  # type: ignore[misc]
            assert isinstance(geofences, list[GeofenceModel])  # type: ignore[misc]

        self._geofences = geofences

        for position in positions:
            if (device := get_device(position["deviceId"], devices)) is None:
                continue

            if (
                attr
                := self._return_custom_attributes_if_not_filtered_by_accuracy_configuration(
                    device, position
                )
            ) is None:
                continue

            data[device["id"]] = {
                "device": device,
                "geofence": get_first_geofence(
                    geofences,
                    position["geofenceIds"] or [],
                ),
                "position": position,
                "attributes": attr,
            }

        await self.subscribe()

        return data

    async def handle_subscription_data(self, data: SubscriptionData) -> None:
        """Handle subscription data."""
        self.logger.debug("Received subscription data: %s", data)
        self._should_log_subscription_error = True
        update_devices = set()
        for device in data.get("devices") or []:
            device_id = device["id"]
            if device_id not in self.data:
                continue

            if (
                attr
                := self._return_custom_attributes_if_not_filtered_by_accuracy_configuration(
                    device, self.data[device_id]["position"]
                )
            ) is None:
                continue

            self.data[device_id]["device"] = device
            self.data[device_id]["attributes"] = attr
            update_devices.add(device_id)

        for position in data.get("positions") or []:
            device_id = position["deviceId"]
            if device_id not in self.data:
                continue

            if (
                attr
                := self._return_custom_attributes_if_not_filtered_by_accuracy_configuration(
                    self.data[device_id]["device"], position
                )
            ) is None:
                continue

            self.data[device_id]["position"] = position
            self.data[device_id]["attributes"] = attr
            self.data[device_id]["geofence"] = get_first_geofence(
                self._geofences,
                position["geofenceIds"] or [],
            )
            update_devices.add(device_id)

        for device_id in update_devices:
            dispatcher_send(self.hass, f"{DOMAIN}_{device_id}")

    async def import_events(self, _: datetime) -> None:
        """Import events from Traccar."""
        start_time = dt_util.utcnow().replace(tzinfo=None)
        end_time = None

        if self._last_event_import is not None:
            end_time = start_time - (start_time - self._last_event_import)

        events = await self.client.get_reports_events(
            devices=list(self.data),
            start_time=start_time,
            end_time=end_time,
            event_types=self.events,
        )
        if not events:
            return

        self._last_event_import = start_time
        for event in events:
            device = self.data[event["deviceId"]]["device"]
            self.hass.bus.async_fire(
                # This goes against two of the HA core guidelines:
                # 1. Event names should be prefixed with the domain name of
                #    the integration
                # 2. This should be event entities
                #
                # However, to not break it for those who currently use
                # the "old" integration, this is kept as is.
                f"traccar_{EVENTS[event['type']]}",
                {
                    "device_traccar_id": event["deviceId"],
                    "device_name": device["name"] if device else None,
                    "type": event["type"],
                    "serverTime": event["eventTime"],
                    "attributes": event["attributes"],
                },
            )

    async def unsubscribe(self, *args) -> None:
        """Unsubscribe from Traccar Server."""
        if self._subscription is None:
            return
        self._should_log_subscription_error = False
        self._subscription.cancel()
        self._subscription = None

    async def subscribe(self) -> None:
        """Subscribe to events."""
        if self._subscription is not None:
            return

        async def _subscriber():
            try:
                await self.client.subscribe(self.handle_subscription_data)
            except TraccarException as ex:
                if self._should_log_subscription_error:
                    self._should_log_subscription_error = False
                    LOGGER.error("Error while subscribing to Traccar: %s", ex)
                # Retry after 10 seconds
                await asyncio.sleep(10)
                await _subscriber()

        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.unsubscribe)
        self.config_entry.async_on_unload(self.unsubscribe)
        self._subscription = asyncio.create_task(_subscriber())

    def _return_custom_attributes_if_not_filtered_by_accuracy_configuration(
        self,
        device: DeviceModel,
        position: PositionModel,
    ) -> dict[str, Any] | None:
        """Return a dictionary of custom attributes if not filtered by accuracy configuration."""
        attr = {}
        skip_accuracy_filter = False

        for custom_attr in self.custom_attributes:
            if custom_attr in self.skip_accuracy_filter_for:
                skip_accuracy_filter = True
            attr[custom_attr] = device["attributes"].get(
                custom_attr,
                position["attributes"].get(custom_attr, None),
            )

        accuracy = position["accuracy"] or 0.0
        if (
            not skip_accuracy_filter
            and self.max_accuracy > 0
            and accuracy > self.max_accuracy
        ):
            return None
        return attr
