"""Stable semantic phases and parameters for background progress."""

from dataclasses import dataclass
from enum import StrEnum, auto
from types import MappingProxyType
from typing import Mapping


class ProgressPhase(StrEnum):
    READ = auto()
    INDEX = auto()
    COPY_BACKUP = auto()
    EXPORT = auto()
    ESTIMATE = auto()
    COPY_OUTPUT = auto()
    WRITE = auto()
    VERIFY = auto()
    BASELINE = auto()
    APPLY = auto()
    UPDATE_CHANGES = auto()
    ADD = auto()
    LOAD_IMAGE = auto()
    OPEN_FILE = auto()
    CLEANUP = auto()
    COPY_TO = auto()
    COPY_FROM = auto()
    WORKING = auto()
    STAGE_EXTERNAL = auto()
    CLOSE_BACKUP = auto()
    ADD_ITEMS = auto()
    DELETE_ITEMS = auto()
    DISCARD = auto()
    PREPARE_DISCARD = auto()
    EXPORT_ITEMS = auto()
    RELOAD_FILE = auto()
    OPEN_ITEM = auto()
    SAVE_EXTERNAL = auto()
    MOVE_ITEMS = auto()
    APPLY_CHANGES = auto()


@dataclass(frozen=True, init=False)
class ProgressMessage:
    phase: ProgressPhase
    values: Mapping[str, object]

    def __init__(self, phase: ProgressPhase, **values: object):
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "values", MappingProxyType(values))
