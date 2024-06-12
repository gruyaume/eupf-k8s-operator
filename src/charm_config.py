# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Config of the Charm."""

import dataclasses
import logging
from ipaddress import ip_network
from typing import Optional

import ops
from pydantic import (
    BaseModel,
    StrictStr,
    ValidationError,
    validator,
)
from pydantic.networks import IPvAnyAddress, IPvAnyNetwork

logger = logging.getLogger(__name__)


class CharmConfigInvalidError(Exception):
    """Exception raised when a charm configuration is found to be invalid."""

    def __init__(self, msg: str):
        """Initialize a new instance of the CharmConfigInvalidError exception.

        Args:
            msg (str): Explanation of the error.
        """
        self.msg = msg


def to_kebab(name: str) -> str:
    """Convert a snake_case string to kebab-case."""
    return name.replace("_", "-")


class UpfConfig(BaseModel):  # pylint: disable=too-few-public-methods
    """Represent UPF operator builtin configuration values."""

    class Config:
        """Represent config for Pydantic model."""
        alias_generator = to_kebab

    gnb_subnet: IPvAnyNetwork = IPvAnyNetwork("192.168.251.0/24")
    core_ip: str
    core_gateway_ip: IPvAnyAddress = IPvAnyAddress("192.168.250.1")
    access_ip: str
    access_gateway_ip: IPvAnyAddress = IPvAnyAddress("192.168.252.1")
    external_upf_hostname: Optional[StrictStr]

    @validator("core_ip", "access_ip")
    @classmethod
    def validate_ip_network_address(cls, value: str) -> str:
        """Validate that IP network address is valid."""
        ip_network(value, strict=False)
        return value


@dataclasses.dataclass
class CharmConfig:
    """Represent the configuration of the charm."""

    gnb_subnet: IPvAnyNetwork
    core_ip: StrictStr
    core_gateway_ip: IPvAnyAddress
    access_ip: StrictStr
    external_upf_hostname: Optional[str]
    access_gateway_ip: IPvAnyAddress

    def __init__(self, *, upf_config: UpfConfig):
        """Initialize a new instance of the CharmConfig class.

        Args:
            upf_config: UPF operator configuration.
        """
        self.gnb_subnet = upf_config.gnb_subnet
        self.core_ip = upf_config.core_ip
        self.core_gateway_ip = upf_config.core_gateway_ip
        self.access_ip = upf_config.access_ip
        self.access_gateway_ip = upf_config.access_gateway_ip
        self.external_upf_hostname = upf_config.external_upf_hostname

    @classmethod
    def from_charm(
        cls,
        charm: ops.CharmBase,
    ) -> "CharmConfig":
        """Initialize a new instance of the CharmState class from the associated charm."""
        try:
            # ignoring because mypy fails with:
            # "has incompatible type "**dict[str, str]"; expected ...""
            return cls(upf_config=UpfConfig(**dict(charm.config.items())))  # type: ignore
        except ValidationError as exc:
            error_fields: list = []
            for error in exc.errors():
                if param := error["loc"]:
                    error_fields.extend(param)
                else:
                    value_error_msg: ValueError = error["ctx"]["error"]  # type: ignore
                    error_fields.extend(str(value_error_msg).split())
            error_fields.sort()
            error_field_str = ", ".join(f"'{f}'" for f in error_fields)
            raise CharmConfigInvalidError(
                f"The following configurations are not valid: [{error_field_str}]"
            ) from exc
