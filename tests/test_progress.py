import threading

from conftest import PASSWORD
from test_ui import window as window

from planbak.app import Task
from planbak.backup import BackupSession
from planbak.i18n import ENGLISH, SIMPLIFIED_CHINESE, LanguageContext
from planbak.progress import ProgressMessage, ProgressPhase


def test_structured_task_progress_preserves_phase_parameters_and_large_counts(monkeypatch):
    monkeypatch.setattr("planbak.app.time.monotonic", lambda: 100)
    large = 5 * 1024**3

    def work(progress, cancel):
        progress(ProgressMessage(ProgressPhase.WRITE, name="x：y"), 1, large)
        progress(ProgressMessage(ProgressPhase.WRITE, name="another"), 2, large)
        progress(ProgressMessage(ProgressPhase.APPLY), 0, 0)

    task = Task(work)
    seen = []
    task.progress.connect(lambda message, done, total: seen.append((message, done, total)))
    task.run()
    assert [item[0].phase for item in seen] == [ProgressPhase.WRITE, ProgressPhase.APPLY]
    assert seen[0][0].values["name"] == "x：y"
    assert seen[0][2] == large
    language = LanguageContext(ENGLISH)
    assert seen[0][0].translate(language) == "Writing: x：y"
    language.set_language(SIMPLIFIED_CHINESE)
    assert seen[0][0].translate(language) == "写入：x：y"


def test_cancellation_after_apply_phase_does_not_interrupt_install(backup_root, tmp_path):
    session = BackupSession(backup_root, PASSWORD)
    cancelled = threading.Event()
    phases = []
    try:
        copied = session.create_copy(tmp_path / "copy")
        session.use_copy(copied)
        entry = session.at("HomeDomain", "Documents/a.txt")
        session.stage_bytes(entry.token, b"changed safely")

        def progress(message, done, total):
            if isinstance(message, ProgressMessage):
                phases.append(message.phase)
                if message.phase is ProgressPhase.APPLY:
                    cancelled.set()

        session.save(progress=progress, cancel=cancelled.is_set)
        assert ProgressPhase.APPLY in phases and cancelled.is_set()
        reopened = BackupSession(copied.path, PASSWORD)
        try:
            target = tmp_path / "readback"
            reopened.extract_file(reopened.at("HomeDomain", "Documents/a.txt").token, target)
            assert target.read_bytes() == b"changed safely"
        finally:
            reopened.close()
    finally:
        session.close()


def test_save_dialog_translates_structured_phase_and_disables_cancel(window, qtbot):
    next_phase, finish = threading.Event(), threading.Event()
    window.language_context.set_language(ENGLISH)

    def work(progress, cancel):
        progress(ProgressMessage(ProgressPhase.WRITE, name="a：b"), 1, 5)
        assert next_phase.wait(10)
        progress(ProgressMessage(ProgressPhase.APPLY), 0, 0)
        assert finish.wait(10)

    window.run_task("应用更改", work, dialog_progress=True)
    try:
        dialog = window.progress_dialog
        qtbot.waitUntil(lambda: dialog._status_label.toolTip() == "Writing: a：b")
        window.language_context.set_language(SIMPLIFIED_CHINESE)
        assert dialog._status_label.toolTip() == "写入：a：b"
        next_phase.set()
        qtbot.waitUntil(lambda: not dialog._cancel_button.isEnabled())
        assert dialog._status_label.toolTip() == "正在应用更改…"
    finally:
        next_phase.set()
        finish.set()
    qtbot.waitUntil(lambda: window.task is None)
