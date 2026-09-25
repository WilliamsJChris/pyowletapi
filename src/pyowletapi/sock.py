import logging
from logging import Logger
import json
import datetime
import time
from .api import OwletAPI, TokenDict, SockData
from .const import PROPERTIES, VITALS_3, VITALS_2, PropertyKey, Properties
from typing import Union, TypedDict, NotRequired, Any, Optional

logger: Logger = logging.getLogger(__package__)


class PropertiesDict(TypedDict):
    raw_properties: dict[str, dict[str, Any]]
    properties: Properties
    tokens: NotRequired[TokenDict]


class Sock:
    """Class representing an Owlet sock device."""

    def __init__(
        self,
        api: OwletAPI,
        data: SockData,
    ) -> None:
        self._api = api
        self._name: str = data.get("product_name", "Owlet Baby Monitors")
        self._model: str = data.get("model", "")
        self._serial: str = data.get("dsn", "Unknown")
        self._oem_model: str = data.get("oem_model", "Unknown")
        self._sw_version: str = data.get("sw_version", "Unknown")
        self._mac: str = data.get("mac", "")
        self._lan_ip: str = data.get("lan_ip", "")
        self._connection_status: str = data.get("connection_status", "Unknown")
        self._device_type: str = data.get("device_type", "Wifi")
        self._manuf_model: str = data.get("manuf_model", "Unknown")
        self._version: Union[int, None] = None
        self._revision = None

        self._raw_properties: dict[str, dict[str, Any]] = {}
        self._properties: Properties = {}

    @property
    def api(self) -> OwletAPI:
        return self._api

    @property
    def version(self) -> Union[int, None]:
        return self._version

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model

    @property
    def serial(self) -> str:
        return self._serial

    @property
    def oem_model(self) -> str:
        return self._oem_model

    @property
    def sw_version(self) -> str:
        return self._sw_version

    @property
    def mac(self) -> str:
        return self._mac

    @property
    def lan_ip(self) -> str:
        return self._lan_ip

    @property
    def connection_status(self) -> str:
        return self._connection_status

    @property
    def device_type(self) -> str:
        return self._device_type

    @property
    def manuf_model(self) -> str:
        return self._manuf_model

    @property
    def properties(self) -> Properties:
        return self._properties

    @property
    def raw_properties(self) -> dict[str, dict[str, Any]]:
        return self._raw_properties

    @property
    def revision(self) -> Optional[int]:
        return self._revision

    def get_property(self, property: PropertyKey) -> Union[bool, str, float, int, None]:
        return self._properties.get(property)

    async def _normalise_properties(self) -> Properties:
        properties: Properties = {}

        for data_type, properties_tmp in PROPERTIES.items():
            for key, property in properties_tmp.items():
                try:
                    properties[key] = data_type(
                        self._raw_properties[property]["value"],
                    )
                except KeyError:
                    pass

        if self._version == 3:
            try:
                vitals_raw = self._raw_properties.get("REAL_TIME_VITALS", {}).get("value", "{}")
                vitals = json.loads(vitals_raw)

                for data_type, vitals_list in VITALS_3.items():
                    for vital_desc, vital_key in vitals_list.items():
                        match vital_desc:
                            case "base_station_on":
                                try:
                                    properties[vital_desc] = vitals["bso"]
                                except (KeyError, TypeError):
                                    pass
                            case _:
                                try:
                                    val = vitals.get(vital_key)
                                    if val is not None:
                                        properties[vital_desc] = data_type(val)
                                except (KeyError, TypeError, ValueError):
                                    pass

                if "data_updated_at" in self._raw_properties.get("REAL_TIME_VITALS", {}):
                    properties["last_updated"] = datetime.datetime.strptime(
                        self._raw_properties["REAL_TIME_VITALS"]["data_updated_at"],
                        "%Y-%m-%dT%H:%M:%SZ",
                    ).strftime("%Y/%m/%d %H:%M:%S")
            except (KeyError, json.JSONDecodeError, TypeError):
                pass

        if self._version == 2:
            for data_type, vitals_list in VITALS_2.items():
                for vital_desc, vital_key in vitals_list.items():
                    try:
                        val = self._raw_properties.get(vital_key, {}).get("value")
                        if val is not None:
                            properties[vital_desc] = data_type(val)
                    except (KeyError, TypeError, ValueError):
                        pass

        # Preserve previous state unless APP_CMD_RESPONSE gives a new val
        mon_recovery_state = self._properties.get("mon_recovery", False)
        if "APP_CMD_RESPONSE" in self._raw_properties:
            try:
                raw_val = self._raw_properties["APP_CMD_RESPONSE"].get("value", "")
                cmd_resp = json.loads(raw_val) if isinstance(raw_val, str) else raw_val
                if isinstance(cmd_resp, dict) and cmd_resp.get("cmd") == "mon_recovery":
                    # Check "result" first (from APP_CMD_RESPONSE), fallback to "val"
                    res = str(cmd_resp.get("result") or cmd_resp.get("val") or "").lower()
                    mon_recovery_state = res in ("on", "true")
            except (json.JSONDecodeError, TypeError, KeyError):
                pass

        properties["mon_recovery"] = mon_recovery_state
        return properties

    async def _check_version(self) -> None:
        version = 0
        if "REAL_TIME_VITALS" in self._raw_properties:
            version = 3
        elif "CHARGE_STATUS" in self._raw_properties:
            version = 2
        self._version = version

    async def _check_revision(self) -> None:
        try:
            revision_json = json.loads(
                self._raw_properties["oem_sock_version"]["value"],
            )
            self._revision = revision_json["rev"]
        except (KeyError, json.JSONDecodeError, TypeError):
            pass

    async def update_properties(self) -> PropertiesDict:
        properties = await self._api.get_properties(self.serial)
        self._raw_properties = properties["response"]

        if self._version not in (2, 3):
            await self._check_version()

        if self._revision is None and self._version == 3:
            await self._check_revision()

        self._properties = await self._normalise_properties()

        response: PropertiesDict = {
            "raw_properties": self._raw_properties,
            "properties": self._properties,
        }

        if "tokens" in properties:
            response["tokens"] = properties["tokens"]

        return response

    async def control_base_station(self, on: bool) -> bool:
        """Calls the Owlet API to turn base station on or off."""
        value = json.dumps(
            {"ts": int(time.time()), "val": "true" if on else "false"},
        )
        data = {"datapoint": {"metadata": {}, "value": value}}

        response = await self._api.post_command(
            self.serial,
            "BASE_STATION_ON_CMD",
            data,
        )

        if response:
            self._properties["base_station_on"] = on
            return True
        return False

    async def control_recovery_mode(self, on: bool) -> bool:
        """Calls the Owlet API to set monitor recovery mode on or off."""
        payload = json.dumps(
            {
                "cmd": "mon_recovery",
                "val": "on" if on else "off",
                "ts": int(time.time()),
            },
            separators=(",", ":"),
        )
        data = {"datapoint": {"metadata": {}, "value": payload}}

        response = await self._api.post_command(
            self.serial,
            "APP_CMD_REQUEST",
            data,
        )

        if response:
            self._properties["mon_recovery"] = on
            return True
        return False