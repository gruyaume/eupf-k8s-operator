#!/usr/bin/env python3
# Copyright 2024 Guillaume Belanger

"""PFCP service for the UPF."""

import logging
from typing import Iterable, Optional

from httpx import HTTPStatusError
from lightkube.core.client import Client
from lightkube.core.exceptions import ApiError
from lightkube.models.apps_v1 import StatefulSetSpec
from lightkube.models.core_v1 import (
    Container,
    HostPathVolumeSource,
    ServicePort,
    ServiceSpec,
    Volume,
    VolumeMount,
)
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.apps_v1 import StatefulSet
from lightkube.resources.core_v1 import Pod, Service

logger = logging.getLogger(__name__)


def get_upf_load_balancer_service_hostname(namespace: str, app_name: str) -> Optional[str]:
    """Get the hostname of the UPF service."""
    client = Client()  # type: ignore[reportArgumentType]
    service = client.get(Service, name=f"{app_name}-external", namespace=namespace)
    try:
        return service.status.loadBalancer.ingress[0].hostname  # type: ignore[reportAttributeAccessIssue]
    except (AttributeError, TypeError):
        logger.error(
            "Service '%s-external' does not have a hostname:\n%s",
            app_name,
            service,
        )
        return None


class PFCPService:
    """PFCP service for the UPF."""

    def __init__(self, namespace: str, service_name: str, app_name: str, pfcp_port: int):
        self.client = Client()  # type: ignore[reportArgumentType]
        self.namespace = namespace
        self.service_name = service_name
        self.app_name = app_name
        self.pfcp_port = pfcp_port

    def is_created(self) -> bool:
        """Check if the external UPF service is created."""
        try:
            self.client.get(
                Service,
                namespace=self.namespace,
                name=self.service_name,
            )
            return True
        except HTTPStatusError as status:
            if status.response.status_code == 404:
                return False
        return False

    def create(self) -> None:
        """Create the external UPF service."""
        service = Service(
            apiVersion="v1",
            kind="Service",
            metadata=ObjectMeta(
                namespace=self.namespace,
                name=self.service_name,
                labels={
                    "app.kubernetes.io/name": self.app_name,
                },
            ),
            spec=ServiceSpec(
                selector={
                    "app.kubernetes.io/name": self.app_name,
                },
                ports=[
                    ServicePort(name="pfcp", port=self.pfcp_port, protocol="UDP"),
                ],
                type="LoadBalancer",
            ),
        )
        self.client.apply(service, field_manager=self.app_name)
        logger.info("Created/asserted existence of the external UPF service")

    def delete(self) -> None:
        """Delete the external UPF service."""
        try:
            self.client.delete(
                Service,
                name=self.service_name,
                namespace=self.namespace,
            )
            logger.info("Deleted external UPF service")
        except HTTPStatusError as status:
            logger.info(f"Could not delete {self.app_name}-external due to: {status}")


class EBPFVolume:
    """eBPF volume for the UPF."""

    def __init__(self, namespace: str, container_name: str, app_name: str, unit_name: str):
        self.client = Client()  # type: ignore[reportArgumentType]
        self.namespace = namespace
        self.app_name = app_name
        self.unit_name = unit_name
        self.container_name = container_name
        self.requested_volumemount = VolumeMount(
            name="ebpf",
            mountPath="/sys/fs/bpf",
        )
        self.requested_volume = Volume(
            name="ebpf",
            hostPath=HostPathVolumeSource(
                path="/sys/fs/bpf",
                type="",
            ),
        )

    def is_created(self) -> bool:
        """Check if the eBPF volume is created."""
        return self._pod_is_patched() and self._statefulset_is_patched()

    def _pod_is_patched(self) -> bool:
        try:
            pod = self.client.get(Pod, name=self._pod_name, namespace=self.namespace)
        except ApiError as e:
            if e.status.reason == "Unauthorized":
                logger.debug("kube-apiserver not ready yet")
            else:
                raise RuntimeError(f"Pod `{self._pod_name}` not found")
            logger.info("Pod `%s` not found", self._pod_name)
            return False
        pod_has_volumemount = self._pod_contains_requested_volumemount(
            requested_volumemount=self.requested_volumemount,
            containers=pod.spec.containers,  # type: ignore[attr-defined]
            container_name=self.container_name,
        )
        logger.info("Pod `%s` has eBPF volume mounted: %s", self._pod_name, pod_has_volumemount)
        return pod_has_volumemount

    def _statefulset_is_patched(self) -> bool:
        try:
            statefulset = self.client.get(
                res=StatefulSet, name=self.app_name, namespace=self.namespace
            )
        except ApiError as e:
            if e.status.reason == "Unauthorized":
                logger.debug("kube-apiserver not ready yet")
            else:
                raise RuntimeError(f"Could not get statefulset `{self.app_name}`")
            logger.info("Statefulset `%s` not found", self.app_name)
            return False

        contains_volume = self._statefulset_contains_requested_volume(
            statefulset_spec=statefulset.spec,  # type: ignore[attr-defined]
            requested_volume=self.requested_volume,
        )
        logger.info("Statefulset `%s` has eBPF volume: %s", self.app_name, contains_volume)
        return contains_volume

    @staticmethod
    def _statefulset_contains_requested_volume(
        statefulset_spec: StatefulSetSpec,
        requested_volume: Volume,
    ) -> bool:
        if not statefulset_spec.template.spec:
            logger.info("Statefulset has no template spec")
            return False
        if not statefulset_spec.template.spec.volumes:
            logger.info("Statefulset has no volumes")
            return False
        return requested_volume in statefulset_spec.template.spec.volumes

    @classmethod
    def _get_container(cls, container_name: str, containers: Iterable[Container]) -> Container:
        try:
            return next(iter(filter(lambda ctr: ctr.name == container_name, containers)))
        except StopIteration:
            raise RuntimeError(f"Container `{container_name}` not found")

    def _pod_contains_requested_volumemount(
        self,
        containers: Iterable[Container],
        container_name: str,
        requested_volumemount: VolumeMount,
    ) -> bool:
        container = self._get_container(container_name=container_name, containers=containers)
        if not container.volumeMounts:
            return False
        return requested_volumemount in container.volumeMounts

    def create(self) -> None:
        """Create the eBPF volume."""
        try:
            statefulset = self.client.get(
                res=StatefulSet, name=self.app_name, namespace=self.namespace
            )
        except ApiError:
            raise RuntimeError(f"Could not get statefulset `{self.app_name}`")

        containers: Iterable[Container] = statefulset.spec.template.spec.containers  # type: ignore[attr-defined]
        container = self._get_container(container_name=self.container_name, containers=containers)
        if not container.volumeMounts:
            container.volumeMounts = [self.requested_volumemount]
        else:
            container.volumeMounts.append(self.requested_volumemount)
        if not statefulset.spec.template.spec.volumes:  # type: ignore[attr-defined]
            statefulset.spec.template.spec.volumes = [self.requested_volume]  # type: ignore[attr-defined]
        else:
            statefulset.spec.template.spec.volumes.append(self.requested_volume)  # type: ignore[attr-defined]
        try:
            self.client.replace(obj=statefulset)
        except ApiError:
            raise RuntimeError(f"Could not replace statefulset `{self.app_name}`")
        logger.info("Replaced `%s` statefulset", self.app_name)

    @property
    def _pod_name(self) -> str:
        """Name of the unit's pod.

        Returns:
            str: A string containing the name of the current unit's pod.
        """
        return "-".join(self.unit_name.rsplit("/", 1))
