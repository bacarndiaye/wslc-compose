"""Compose resource labels shared by wslc-compose and other WSLc tools."""

from __future__ import annotations

import hashlib
import json
from typing import Dict

from wslc_compose import (
    LABEL_CONFIG_FILES,
    LABEL_CONFIG_HASH,
    LABEL_ENV_FILE,
    LABEL_INDEX,
    LABEL_NETWORK,
    LABEL_PROJECT,
    LABEL_SERVICE,
    LABEL_VERSION,
    LABEL_VOLUME,
    LABEL_WORKING_DIR,
    __version__,
)
from wslc_compose.model import Project, Service


def _project_labels(project: Project) -> Dict[str, str]:
    labels = {
        LABEL_PROJECT: project.name,
        LABEL_VERSION: __version__,
        LABEL_WORKING_DIR: project.directory,
        LABEL_CONFIG_FILES: json.dumps(project.compose_files, separators=(",", ":")),
    }
    if project.env_file:
        labels[LABEL_ENV_FILE] = project.env_file
    return labels


def container_labels(project: Project, service: Service, index: int) -> Dict[str, str]:
    labels = _project_labels(project)
    config_hash = service.config_hash()
    labels.update(
        {
            LABEL_SERVICE: service.name,
            LABEL_INDEX: str(index),
            LABEL_CONFIG_HASH: config_hash,
        }
    )
    return labels


def resource_labels(project: Project, kind: str, key: str, name: str) -> Dict[str, str]:
    labels = _project_labels(project)
    labels[LABEL_NETWORK if kind == "network" else LABEL_VOLUME] = key
    labels[LABEL_CONFIG_HASH] = hashlib.sha256(
        f"{kind}:{key}:{name}".encode()
    ).hexdigest()[:16]
    return labels
