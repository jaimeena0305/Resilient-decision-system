"""
app/services/config_loader.py
─────────────────────────────────────────────────────────────────────────────
Loads and caches workflow YAML configuration files.

Design:
  • `load_workflow_config(workflow_id)` is the single public API.
  • Configs are cached in-process (LRU) after the first read.
  • For production, this can be replaced with a DB-backed or S3-backed
    registry without changing any caller code.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import functools
import logging
import os
from typing import Any, Dict

import yaml

logger = logging.getLogger(__name__)

# Resolve the workflows directory relative to this file
_WORKFLOWS_DIR = os.path.join(
    os.path.dirname(__file__),   # app/services/
    "..",                         # app/
    "workflows",
)


@functools.lru_cache(maxsize=64)
def load_workflow_config(workflow_id: str) -> Dict[str, Any]:
    """
    Load a workflow YAML config by workflow_id.

    The file is expected at: app/workflows/<workflow_id>.yaml

    Results are cached per process. To force a reload (e.g. in tests),
    call `load_workflow_config.cache_clear()`.

    Raises:
        FileNotFoundError: if no YAML file exists for the given workflow_id.
        yaml.YAMLError:    if the file is not valid YAML.
    """
    yaml_path = os.path.normpath(os.path.join(_WORKFLOWS_DIR, f"{workflow_id}.yaml"))

    # Guard against path traversal (belt-and-suspenders; Pydantic also checks)
    if not yaml_path.startswith(os.path.normpath(_WORKFLOWS_DIR)):
        raise ValueError(f"Invalid workflow_id causes path traversal: '{workflow_id}'")

    if not os.path.isfile(yaml_path):
        raise FileNotFoundError(
            f"Workflow config not found: '{workflow_id}'. "
            f"Expected file at: {yaml_path}"
        )

    with open(yaml_path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)

    logger.info("Loaded workflow config: %s (version=%s)", workflow_id, config.get("version"))
    return config


def list_available_workflows() -> list[str]:
    """Return the workflow_ids of all YAML files in the workflows directory."""
    if not os.path.isdir(_WORKFLOWS_DIR):
        return []
    return [
        fname[:-5]  # strip .yaml
        for fname in os.listdir(_WORKFLOWS_DIR)
        if fname.endswith(".yaml")
    ]
