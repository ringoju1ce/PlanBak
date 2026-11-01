"""Window-scoped language selection and translation lookup."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files

from PySide6.QtCore import QLocale, QObject, Signal

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
        source = getattr(error, "translation_source", None)
        return self.translate(source) if source else str(error)
