"""Window-scoped language selection and translation lookup."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files

from PySide6.QtCore import QLocale, QObject, Signal

from .progress import ProgressMessage, ProgressPhase

ENGLISH = "en"
SIMPLIFIED_CHINESE = "zh_CN"
SUPPORTED_LANGUAGES = (SIMPLIFIED_CHINESE, ENGLISH)


def match_language(locale: str) -> str:
    """Prefer a supported variant of the same language, then English."""
    language = str(locale).replace("-", "_").split("_", 1)[0].lower()
    return SIMPLIFIED_CHINESE if language == "zh" else ENGLISH


def system_language() -> str:
    locales = QLocale.system().uiLanguages()
    return match_language(locales[0] if locales else QLocale.system().name())


@lru_cache(maxsize=None)
def _catalog(language: str) -> dict[str, str]:
    if language == ENGLISH:
        return {}
    path = files("planbak").joinpath("translations", f"{language}.json")
    return json.loads(path.read_text(encoding="utf-8"))


class LanguageContext(QObject):
    """Language choice for one backup window and its document windows."""

    changed = Signal()

    def __init__(self, language: str | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self._language = language if language is not None else system_language()
        if self._language not in SUPPORTED_LANGUAGES:
            raise ValueError("Unsupported language")

    @property
    def language(self) -> str:
        return self._language

    def set_language(self, language: str) -> None:
        if language not in SUPPORTED_LANGUAGES:
            raise ValueError("Unsupported language")
        if language != self._language:
            self._language = language
            self.changed.emit()

    def translate(self, source: str, **values: object) -> str:
        text = _catalog(self._language).get(source, source)
        return text.format(**values) if values else text

    def error(self, error: Exception) -> str:
        return self._error(error, set())

    def _error(self, error: Exception, seen: set[int]) -> str:
        if id(error) in seen:
            return str(error)
        seen.add(id(error))
        source = getattr(error, "translation_source", None)
        if not source:
            return str(error)
        values = {
            key: self._error(value, seen.copy()) if isinstance(value, Exception) else value
            for key, value in getattr(error, "translation_values", {}).items()
        }
        return self.translate(source, **values)


_PROGRESS_LABELS = {
    ProgressPhase.READ: "Reading backup…",
    ProgressPhase.INDEX: "Building backup index…",
    ProgressPhase.COPY_BACKUP: "Creating backup copy…",
    ProgressPhase.EXPORT: "Exporting: {name}",
    ProgressPhase.ESTIMATE: "Estimating backup size…",
    ProgressPhase.COPY_OUTPUT: "Copying backup to output folder…",
    ProgressPhase.WRITE: "Writing: {name}",
    ProgressPhase.VERIFY: "Repacking and verifying manifest…",
    ProgressPhase.BASELINE: "Establishing the new save baseline…",
    ProgressPhase.APPLY: "Applying changes…",
    ProgressPhase.UPDATE_CHANGES: "Updating changes…",
    ProgressPhase.ADD: "Adding: {name}",
    ProgressPhase.LOAD_IMAGE: "Loading file: {name}",
    ProgressPhase.OPEN_FILE: "Opening file…",
    ProgressPhase.CLEANUP: "Cleaning old session cache…",
    ProgressPhase.COPY_TO: "Copying items to this backup…",
    ProgressPhase.COPY_FROM: "Copying items to another backup…",
    ProgressPhase.WORKING: "Working…",
    ProgressPhase.STAGE_EXTERNAL: "Applying external edits…",
    ProgressPhase.CLOSE_BACKUP: "Closing backup and clearing cache…",
    ProgressPhase.ADD_ITEMS: "Adding items…",
    ProgressPhase.DELETE_ITEMS: "Deleting items…",
    ProgressPhase.DISCARD: "Discarding changes…",
    ProgressPhase.PREPARE_DISCARD: "Preparing to discard changes…",
    ProgressPhase.EXPORT_ITEMS: "Exporting items…",
    ProgressPhase.RELOAD_FILE: "Reloading file…",
    ProgressPhase.OPEN_ITEM: "Opening item…",
    ProgressPhase.SAVE_EXTERNAL: "Saving external edits…",
    ProgressPhase.MOVE_ITEMS: "Moving items…",
    ProgressPhase.APPLY_CHANGES: "Apply Changes",
}


def translate_progress(context: LanguageContext, message: ProgressMessage) -> str:
    return context.translate(_PROGRESS_LABELS[message.phase], **message.values)
