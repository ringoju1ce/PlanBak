import plistlib

import pytest
from conftest import make_backup
from PySide6.QtWidgets import QWidget
from test_backup_windows import factory as factory
from test_backup_windows import ready as ready

from planbak.about import AboutDialog
from planbak.app import BackupChoiceDialog, PasswordDialog
from planbak.dialogs import DocumentWindow, FixedDialog
from planbak.i18n import ENGLISH, SIMPLIFIED_CHINESE, LanguageContext, match_language


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("zh_CN", SIMPLIFIED_CHINESE),
        ("zh-Hant-TW", SIMPLIFIED_CHINESE),
        ("en-GB", ENGLISH),
        ("fr-FR", ENGLISH),
        ("C", ENGLISH),
    ],
)
def test_language_matching(locale, expected):
    assert match_language(locale) == expected


def test_language_contexts_are_independent(qtbot):
    first = LanguageContext(ENGLISH)
    second = LanguageContext(ENGLISH)
    changes = []
    first.changed.connect(lambda: changes.append(first.language))
    first.set_language(SIMPLIFIED_CHINESE)
    assert first.translate("Simplified Chinese") == "简体中文"
    assert second.translate("Simplified Chinese") == "Simplified Chinese"
    assert changes == [SIMPLIFIED_CHINESE]
    assert first.translate("English fallback") == "English fallback"


def test_child_windows_inherit_language_context(qtbot):
    parent = QWidget()
    parent.language_context = LanguageContext(ENGLISH, parent)
    qtbot.addWidget(parent)
    nested = QWidget(parent)
    document = DocumentWindow(parent)
    dialog = FixedDialog(nested)
    qtbot.addWidget(document)
    assert document.language_context is parent.language_context
    assert dialog.language_context is parent.language_context


def test_new_backup_window_inherits_then_keeps_own_language(factory, qtbot, tmp_path):
    first = factory()
    first.language_context.set_language(SIMPLIFIED_CHINESE)
    first.open_path(make_backup(tmp_path / "backup"), new_window=True)
    second = next(w for w in factory.manager.windows if w is not first)
    ready(second, qtbot)
    assert second.language_context.language == SIMPLIFIED_CHINESE
    first.language_context.set_language(ENGLISH)
    assert second.language_context.language == SIMPLIFIED_CHINESE


def test_about_uses_owning_window_language_and_updates(qtbot):
    parent = QWidget()
    parent.language_context = LanguageContext(SIMPLIFIED_CHINESE, parent)
    qtbot.addWidget(parent)
    dialog = AboutDialog(parent)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.windowTitle() == "关于 PlanBak"
    assert dialog.close_button.text() == "关闭"
    parent.language_context.set_language(ENGLISH)
    assert dialog.windowTitle() == "About PlanBak"
    assert dialog.close_button.text() == "Close"
    for label in dialog.description_labels:
        assert label.heightForWidth(label.width()) <= label.height()
    parent.language_context.set_language(SIMPLIFIED_CHINESE)
    assert dialog.close_button.text() == "关闭"


def test_backup_choice_translates_without_changing_names_or_selection(qtbot, tmp_path):
    parent = QWidget()
    parent.language_context = LanguageContext(SIMPLIFIED_CHINESE, parent)
    qtbot.addWidget(parent)
    first = make_backup(tmp_path / "first")
    second = make_backup(tmp_path / "second")
    info = plistlib.loads((first / "Info.plist").read_bytes())
    info["Device Name"] = "未知"
    (first / "Info.plist").write_bytes(plistlib.dumps(info))
    dialog = BackupChoiceDialog([first, second], parent)
    qtbot.addWidget(dialog)
    dialog.table.setCurrentCell(1, 0)
    assert dialog.table.horizontalHeaderItem(0).text() == "设备名称"
    parent.language_context.set_language(ENGLISH)
    assert dialog.table.horizontalHeaderItem(0).text() == "Device Name"
    assert dialog.table.item(0, 0).text() == "未知"
    assert dialog.table.currentRow() == 1


def test_password_dialog_translation_preserves_input(qtbot):
    parent = QWidget()
    parent.language_context = LanguageContext(SIMPLIFIED_CHINESE, parent)
    qtbot.addWidget(parent)
    dialog = PasswordDialog(parent)
    qtbot.addWidget(dialog)
    dialog.password.setText("test-value")
    assert dialog.reveal.text() == "显示密码"
    parent.language_context.set_language(ENGLISH)
    assert dialog.windowTitle() == "Unlock Encrypted Backup"
    assert dialog.reveal.text() == "Show password"
    assert dialog.password.text() == "test-value"


def test_hex_viewer_language_change_preserves_state(qtbot, tmp_path):
    from planbak.hex_viewer import HexViewer

    parent = QWidget()
    parent.language_context = LanguageContext(SIMPLIFIED_CHINESE, parent)
    qtbot.addWidget(parent)
    path = tmp_path / 'sample.bin'
    path.write_bytes(bytes(range(64)))
    viewer = HexViewer(path, 'sample.bin', parent)
    qtbot.addWidget(viewer)
    viewer.search_input.setText('hello')
    viewer.go(16, select=True)
    viewer.set_source_state(stale=True)
    parent.language_context.set_language(ENGLISH)
    assert 'Hex Viewer' in viewer.windowTitle()
    assert viewer.search_input.text() == 'hello'
    assert viewer.cursor_offset == 16
    assert 'source file has changed' in viewer.status.text()
    viewer.address.setText('-1')
    viewer.jump()
    assert viewer.status.text() != ''
    english_error = viewer.status.text()
    parent.language_context.set_language(SIMPLIFIED_CHINESE)
    assert viewer.status.text() != english_error
    assert viewer.address.text() == '-1'
    assert viewer.search_input.text() == 'hello'


def test_error_translation_preserves_external_errors():
    from planbak.codec import BackupError

    context = LanguageContext(ENGLISH)
    assert context.error(OSError('external detail')) == 'external detail'
    assert context.error(BackupError('错误', english='Error')) == 'Error'


def test_language_menu_changes_only_its_window(factory):
    first = factory()
    second = factory()
    first.language_context.set_language(SIMPLIFIED_CHINESE)
    second.language_context.set_language(SIMPLIFIED_CHINESE)
    first.language_actions[ENGLISH].trigger()
    assert first.file_menu.title() == 'File'
    assert second.file_menu.title() == '文件'
    assert first.language_actions[ENGLISH].isChecked()
    assert second.language_actions[SIMPLIFIED_CHINESE].isChecked()


def test_sqlite_viewer_switch_keeps_query_and_selected_table(qtbot, tmp_path):
    import sqlite3

    from planbak.sqlite_database import DatabaseSnapshot
    from planbak.sqlite_viewer import SQLiteViewer

    path = tmp_path / 'sample.db'
    with sqlite3.connect(path) as connection:
        connection.execute('create table example(value text)')
    parent = QWidget()
    parent.language_context = LanguageContext(SIMPLIFIED_CHINESE, parent)
    qtbot.addWidget(parent)
    viewer = SQLiteViewer(DatabaseSnapshot(tmp_path, path, b''), 'sample.db', parent)
    qtbot.addWidget(viewer)
    qtbot.waitUntil(lambda: viewer.task is None)
    viewer.tree.setCurrentItem(viewer.tree.topLevelItem(0))
    qtbot.waitUntil(lambda: viewer.task is None)
    viewer.code.setPlainText('select * from example')
    parent.language_context.set_language(ENGLISH)
    assert viewer.tree.currentItem().text(0) == 'example'
    assert viewer.tree.currentItem().text(1) == 'Table'
    assert viewer.code.toPlainText() == 'select * from example'
    assert viewer.tabs.tabText(0) == 'Data'
    assert 'SQLite Viewer' in viewer.windowTitle()


def test_image_viewer_switch_preserves_picture_and_zoom(qtbot):
    from PySide6.QtGui import QImage

    from planbak.image_data import DecodedImage
    from planbak.image_viewer import ImageViewer

    parent = QWidget()
    parent.language_context = LanguageContext(SIMPLIFIED_CHINESE, parent)
    qtbot.addWidget(parent)
    img = QImage(100, 80, QImage.Format.Format_RGB32)
    viewer = ImageViewer([('one', '一.png')], 'one', lambda *args, **kwargs: DecodedImage(img, 'PNG'), lambda *_: True, parent)
    qtbot.addWidget(viewer)
    qtbot.waitUntil(lambda: viewer.task is None)
    viewer.canvas.zoom(0.5)
    viewer.loop_toggle.setChecked(True)
    parent.language_context.set_language(ENGLISH)
    assert viewer.current_token == 'one'
    assert viewer.canvas.scale == 0.5
    assert viewer.loop_toggle.isChecked()
    assert viewer.status.details_button.text() == 'Image Information'
    assert 'File name:' in viewer.image_information()
    parent.language_context.set_language(SIMPLIFIED_CHINESE)
    assert '文件名称：' in viewer.image_information()


def test_plist_tree_language_switch_preserves_selection_and_document(qtbot):
    from planbak.plist_editor import PlistDocument, PlistEditor

    parent = QWidget()
    parent.language_context = LanguageContext(SIMPLIFIED_CHINESE, parent)
    qtbot.addWidget(parent)
    document = PlistDocument(plistlib.dumps({'原始键': ['value']}))
    editor = PlistEditor(document, '原始.plist', lambda *_: None, parent)
    qtbot.addWidget(editor)
    root = editor.tree.topLevelItem(0)
    child = root.child(0)
    editor.tree.setCurrentItem(child)
    parent.language_context.set_language(ENGLISH)
    assert editor.tree.currentItem() is child
    assert child.text(0) == '原始键'
    assert child.text(2) == '1 items'
    assert root.text(0) == '(Root)'
    assert editor.tree.headerItem().text(0) == 'Key / Index'
    assert document.root.to_value() == {'原始键': ['value']}


def test_plist_xml_search_language_preserves_inputs(qtbot):
    from planbak.plist_editor import PlistDocument, PlistEditor

    parent = QWidget()
    parent.language_context = LanguageContext(SIMPLIFIED_CHINESE, parent)
    qtbot.addWidget(parent)
    editor = PlistEditor(PlistDocument(plistlib.dumps({'key': 'value'})), 'x.plist', lambda *_: None, parent)
    qtbot.addWidget(editor)
    editor.switch_mode(1)
    dialog = editor.xml.ensure_search()
    dialog.find_field.setText('value')
    dialog.replace_field.setText('new value')
    parent.language_context.set_language(ENGLISH)
    assert dialog.windowTitle() == 'Find and Replace'
    assert dialog.find_field.text() == 'value'
    assert dialog.replace_field.text() == 'new value'
    assert editor.xml.tree_button.text() == 'Tree Editor'


def test_plist_value_dialog_language_preserves_input(qtbot):
    from planbak.plist_editor import PlistNode, ValueDialog

    parent = QWidget()
    parent.language_context = LanguageContext(SIMPLIFIED_CHINESE, parent)
    qtbot.addWidget(parent)
    node = PlistNode('String', 'key', 'old')
    dialog = ValueDialog(node, 'Dictionary', [node], True, parent)
    qtbot.addWidget(dialog)
    dialog.key_field.setText('edited key')
    dialog.value_field.setPlainText('edited value')
    parent.language_context.set_language(ENGLISH)
    assert dialog.windowTitle() == 'Edit plist Item'
    assert dialog.key_field.text() == 'edited key'
    assert dialog.value_field.toPlainText() == 'edited value'
    assert dialog.form.labelForField(dialog.key_field).text() == 'Key:'
    dialog.accept()
    assert dialog.result_node.value == 'edited value'


def test_plist_value_dialog_localizes_validation(qtbot):
    from planbak.plist_editor import PlistNode, ValueDialog

    parent = QWidget()
    parent.language_context = LanguageContext(ENGLISH, parent)
    qtbot.addWidget(parent)
    node, other = PlistNode('String', 'key', 'value'), PlistNode('String', 'existing', '')
    dialog = ValueDialog(node, 'Dictionary', [node, other], True, parent)
    qtbot.addWidget(dialog)
    dialog.key_field.setText('existing')
    dialog.accept()
    assert dialog.error.text() == 'This dictionary already contains that key'
    parent.language_context.set_language(SIMPLIFIED_CHINESE)
    assert dialog.error.text() == '该字典已存在同名键'


def test_main_language_switch_preserves_navigation_and_progress(factory, qtbot, tmp_path):
    from planbak.navigation import Location

    w = factory()
    w.language_context.set_language(SIMPLIFIED_CHINESE)
    w.open_path(make_backup(tmp_path / 'backup'))
    ready(w, qtbot)
    w.navigate(Location('HomeDomain', ''))
    original_location = w.current_location()
    old_rows = w.model.rows
    marker = object()
    from planbak.progress import ProgressMessage, ProgressPhase

    w._status_jobs[marker] = (ProgressMessage(ProgressPhase.INDEX), 5, 10)
    w.update_work_status()
    w.language_context.set_language(ENGLISH)
    assert w.current_location() == original_location
    assert w.model.rows is old_rows
    assert w.model.headerData(0, __import__('PySide6').QtCore.Qt.Orientation.Horizontal, __import__('PySide6').QtCore.Qt.ItemDataRole.DisplayRole) == 'Name'
    assert w.work_label.text() == 'Building backup index…'
    assert w.changes_dock.windowTitle() == 'Changes'
    assert 'Backup' in w.directory_tree.directory_model.data(w.directory_tree.directory_model.index(0, 0))
    w._status_jobs.pop(marker)
    w.update_work_status()


def test_pending_changes_retranslate_without_changing_operation_types(factory, qtbot, tmp_path):
    from planbak.staging import ChangeKind

    w = factory()
    w.language_context.set_language(SIMPLIFIED_CHINESE)
    w.open_path(make_backup(tmp_path / "backup"))
    ready(w, qtbot)
    entry = w.session.at("HomeDomain", "Documents/a.txt")
    w.session.move([entry.token], "HomeDomain", "Documents", name="renamed.txt")
    w.session.stage_bytes(entry.token, b"changed")
    w.refresh()
    index = w.changes_model.index(0, 0)
    assert "重命名" in index.data() and "修改" in index.data()
    assert w.session.changes[0].kinds == (ChangeKind.RENAME, ChangeKind.MODIFY)
    w.language_context.set_language(ENGLISH)
    assert "Rename" in index.data() and "Modify" in index.data()
    assert w.session.changes[0].kinds == (ChangeKind.RENAME, ChangeKind.MODIFY)


def test_main_conflict_dialog_uses_window_language(factory, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from planbak.dialogs import FixedMessageBox

    window = factory()
    window.language_context.set_language(ENGLISH)
    observed = []
    def fake_exec(box):
        observed.append((box.windowTitle(), box.text(), [button.text() for button in box.buttons()]))
        return QMessageBox.StandardButton.Cancel
    monkeypatch.setattr(FixedMessageBox, 'exec', fake_exec)
    assert window.collision_policy() is None
    assert observed[0][0] == 'Name Conflict'
    assert 'Keep Both' in observed[0][2]
    assert 'Cancel' in observed[0][2]


def test_standard_message_buttons_follow_owner_without_overwriting_custom(qtbot):
    from PySide6.QtWidgets import QMessageBox

    from planbak.dialogs import FixedMessageBox

    parent = QWidget()
    parent.language_context = LanguageContext(ENGLISH, parent)
    qtbot.addWidget(parent)
    box = FixedMessageBox(QMessageBox.Icon.Question, 'Example', '', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
    qtbot.addWidget(box)
    box.button(QMessageBox.StandardButton.No).setText('Custom')
    box.show()
    parent.language_context.set_language(SIMPLIFIED_CHINESE)
    assert box.button(QMessageBox.StandardButton.Yes).text() == '是'
    assert box.button(QMessageBox.StandardButton.No).text() == 'Custom'
    parent.language_context.set_language(ENGLISH)
    assert box.button(QMessageBox.StandardButton.Yes).text() == 'Yes'


def test_invalid_backup_error_is_localized_without_translating_external_details(qtbot, tmp_path, monkeypatch):
    from planbak.app import message
    from planbak.backup import detect_backups
    from planbak.codec import BackupError
    from planbak.dialogs import FixedMessageBox

    parent = QWidget()
    parent.language_context = LanguageContext(ENGLISH, parent)
    qtbot.addWidget(parent)
    shown = []
    monkeypatch.setattr(FixedMessageBox, 'exec', lambda box: shown.append((box.windowTitle(), box.text())))
    with pytest.raises(BackupError) as caught:
        detect_backups(tmp_path)
    message(parent, 'Unable to Open Backup', caught.value)
    assert shown[-1][0] == 'Unable to Open Backup'
    assert 'does not contain an iOS backup' in shown[-1][1]
    message(parent, 'Unable to Open Backup', OSError('opaque 外部 detail'))
    assert shown[-1][1] == 'opaque 外部 detail'


def test_file_and_input_dialog_controls_follow_owning_language(qtbot, tmp_path):
    from PySide6.QtWidgets import QFileDialog

    from planbak.dialogs import FixedFileDialog, FixedInputDialog

    parent = QWidget()
    parent.language_context = LanguageContext(ENGLISH, parent)
    qtbot.addWidget(parent)
    picker = FixedFileDialog(parent, '', str(tmp_path))
    qtbot.addWidget(picker)
    picker.setFileMode(QFileDialog.FileMode.Directory)
    picker.show()
    prompt = FixedInputDialog(parent)
    qtbot.addWidget(prompt)
    prompt.setTextValue('keep input')
    assert picker.labelText(QFileDialog.DialogLabel.Accept) == 'Choose'
    parent.language_context.set_language(SIMPLIFIED_CHINESE)
    assert picker.labelText(QFileDialog.DialogLabel.Accept) == '选择'
    assert picker.labelText(QFileDialog.DialogLabel.FileName) == '文件名：'
    assert prompt.okButtonText() == '确定'
    assert prompt.textValue() == 'keep input'


def test_sqlite_result_values_translate_without_touching_data(qtbot):
    from planbak.sqlite_database import QueryResult
    from planbak.sqlite_viewer import ResultModel

    parent = QWidget()
    parent.language_context = LanguageContext(ENGLISH, parent)
    qtbot.addWidget(parent)
    model = ResultModel(parent)
    model.set_result(QueryResult(['name', 'data'], [['', b'\x01\x02']], False))
    assert model.data(model.index(0, 0)) == '"" (empty string)'
    assert '2 bytes' in model.data(model.index(0, 1))
    parent.language_context.set_language(SIMPLIFIED_CHINESE)
    assert '空字符串' in model.data(model.index(0, 0))
    assert '2 字节' in model.data(model.index(0, 1))
    assert model.result.rows == [['', b'\x01\x02']]


def test_single_file_picker_uses_window_scoped_dialog(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog, QFileDialog, QToolButton

    from planbak.dialogs import FixedFileDialog

    parent = QWidget()
    parent.language_context = LanguageContext(SIMPLIFIED_CHINESE, parent)
    qtbot.addWidget(parent)
    path = tmp_path / '原始.txt'
    path.write_text('example')
    def choose(dialog):
        dialog.show()
        assert dialog.fileMode() == QFileDialog.FileMode.ExistingFile
        assert dialog.labelText(QFileDialog.DialogLabel.Accept) == '打开'
        assert dialog.findChild(QToolButton, 'backButton').toolTip() == '后退'
        dialog.selectFile(str(path))
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(FixedFileDialog, 'exec', choose)
    result, _ = FixedFileDialog.getOpenFileName(parent, 'test', str(tmp_path))
    assert result == str(path)


def test_image_metadata_explanations_translate_not_exif_values():
    from types import SimpleNamespace

    from planbak.image_metadata import ImageMetadata, MetadataIssue, information_text

    metadata = ImageMetadata(50, mode='L', exif=(('Make', '灰度'),), error=(MetadataIssue('EXIF data is too large to display'),))
    image = SimpleNamespace(metadata=metadata, name='照片.png', backup_path='', format='PNG', width=2, height=3)
    text = information_text(image, LanguageContext(ENGLISH).translate)
    assert 'Color mode: Grayscale' in text
    assert 'EXIF data is too large to display' in text
    assert 'Make: 灰度' in text
    assert '照片.png' in text


def test_xml_error_translates_without_changing_buffer(qtbot):
    from planbak.plist_editor import PlistDocument, PlistEditor

    parent = QWidget()
    parent.language_context = LanguageContext(SIMPLIFIED_CHINESE, parent)
    qtbot.addWidget(parent)
    editor = PlistEditor(PlistDocument(plistlib.dumps({'key': 'value'})), 'x.plist', lambda *_: None, parent)
    qtbot.addWidget(editor)
    editor.switch_mode(1)
    invalid = '<plist><dict><key>原始键</key><string>a</string><key>原始键</key><string>b</string></dict></plist>'
    editor.xml.code.setPlainText(invalid)
    assert not editor.sync_xml()
    assert '重复键：原始键' in editor.xml.error.text()
    parent.language_context.set_language(ENGLISH)
    assert 'Duplicate dictionary key: 原始键' in editor.xml.error.text()
    assert editor.xml.code.toPlainText() == invalid
    editor.discard_and_close()


def test_backup_validation_error_remains_stable_and_translates():
    from planbak.codec import BackupError, safe_path

    with pytest.raises(BackupError) as caught:
        safe_path('/absolute')
    assert str(caught.value) == 'The path cannot be absolute or contain . / ..'
    assert LanguageContext(ENGLISH).error(caught.value) == 'The path cannot be absolute or contain . / ..'
    assert LanguageContext(SIMPLIFIED_CHINESE).error(caught.value) == '路径不能是绝对路径或包含 . / ..'


def test_dynamic_backup_error_translates_template_not_filename(tmp_path):
    from planbak.backup import load_plist
    from planbak.codec import BackupError

    path = tmp_path / '文件{奇怪}.plist'
    path.write_bytes(b'not a plist')
    with pytest.raises(BackupError) as caught:
        load_plist(path)
    assert '文件{奇怪}.plist' in str(caught.value)
    assert LanguageContext(ENGLISH).error(caught.value) == 'Unable to read 文件{奇怪}.plist'
    assert LanguageContext(SIMPLIFIED_CHINESE).error(caught.value) == str(caught.value)


def test_plist_xml_unavailable_tooltip_switches_language(qtbot):
    from planbak.plist_editor import PlistDocument, PlistEditor

    parent = QWidget()
    parent.language_context = LanguageContext(ENGLISH, parent)
    qtbot.addWidget(parent)
    data = plistlib.dumps({'ref': plistlib.UID(3)}, fmt=plistlib.FMT_BINARY)
    editor = PlistEditor(PlistDocument(data), 'uid.plist', lambda *_: None, parent)
    qtbot.addWidget(editor)
    assert not editor.xml_button.isEnabled()
    assert 'cannot represent the UID type' in editor.xml_button.toolTip()
    parent.language_context.set_language(SIMPLIFIED_CHINESE)
    assert '无法表示 UID 类型' in editor.xml_button.toolTip()


def test_plist_toolbar_tooltips_follow_language_and_keep_shortcut(qtbot):
    from planbak.plist_editor import PlistDocument, PlistEditor
    parent = QWidget()
    parent.language_context = LanguageContext(SIMPLIFIED_CHINESE, parent)
    qtbot.addWidget(parent)
    editor = PlistEditor(PlistDocument(plistlib.dumps({'key': 'value'})), 'x.plist', lambda *_: None, parent)
    qtbot.addWidget(editor)
    assert '编辑' in editor.edit_button.toolTip() and 'F2' in editor.edit_button.toolTip()
    parent.language_context.set_language(ENGLISH)
    assert 'Edit' in editor.edit_button.toolTip() and 'F2' in editor.edit_button.toolTip()
    assert 'Find' in editor.xml.search_button.toolTip() and 'Ctrl+F' in editor.xml.search_button.toolTip()


def test_plist_validation_error_preserves_api_and_translates():
    from planbak.plist_editor import validate_structure

    value = []
    value.append(value)
    with pytest.raises(ValueError) as caught:
        validate_structure(value)
    assert str(caught.value) == 'plist 包含循环引用，无法作为树编辑'
    assert 'contains a cycle' in LanguageContext(ENGLISH).error(caught.value)
    assert LanguageContext(SIMPLIFIED_CHINESE).error(caught.value) == str(caught.value)


def test_image_read_error_formats_in_both_languages():
    from planbak.codec import BackupError

    error = BackupError('无法读取图片：raw {detail}', english='Unable to read image: {error}', values={'error': 'raw {detail}'})
    assert LanguageContext(ENGLISH).error(error) == 'Unable to read image: raw {detail}'
    assert LanguageContext(SIMPLIFIED_CHINESE).error(error) == '无法读取图片：raw {detail}'


def test_file_dialog_headers_change_without_renaming_items(qtbot, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QFileDialog, QTreeView

    from planbak.dialogs import FixedFileDialog

    parent = QWidget()
    parent.language_context = LanguageContext(ENGLISH, parent)
    qtbot.addWidget(parent)
    item = tmp_path / 'Name'
    item.write_text('content')
    dialog = FixedFileDialog(parent, '', str(tmp_path))
    qtbot.addWidget(dialog)
    dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
    dialog.show()
    tree = dialog.findChild(QTreeView, 'treeView')
    model = tree.model()
    assert model.headerData(0, Qt.Orientation.Horizontal) == 'Name'
    dialog.selectFile(str(item))
    parent.language_context.set_language(SIMPLIFIED_CHINESE)
    assert model.headerData(0, Qt.Orientation.Horizontal) == '名称'
    assert dialog.selectedFiles() == [str(item)]


def test_file_picker_translates_default_filter_and_preserves_custom_filter(qtbot, tmp_path):
    from PySide6.QtWidgets import QComboBox

    from planbak.dialogs import FixedFileDialog

    parent = QWidget()
    parent.language_context = LanguageContext(ENGLISH, parent)
    qtbot.addWidget(parent)
    default = FixedFileDialog(parent, '', str(tmp_path))
    qtbot.addWidget(default)
    default.show()
    filters = default.findChild(QComboBox, 'fileTypeCombo')
    parent.language_context.set_language(SIMPLIFIED_CHINESE)
    assert filters.itemText(0) == '所有文件 (*)'
    custom = FixedFileDialog(parent, '', str(tmp_path), 'User filter (*.abc)')
    qtbot.addWidget(custom)
    custom.show()
    parent.language_context.set_language(ENGLISH)
    assert custom.findChild(QComboBox, 'fileTypeCombo').itemText(0) == 'User filter (*.abc)'


def test_plist_date_calendar_follows_owner_without_changing_value(qtbot):
    from datetime import datetime

    from planbak.plist_editor import PlistNode, ValueDialog

    parent = QWidget()
    parent.language_context = LanguageContext(ENGLISH, parent)
    qtbot.addWidget(parent)
    value = datetime(2024, 2, 3, 4, 5, 6, 123456)
    node = PlistNode('Date', 'date', value)
    dialog = ValueDialog(node, 'Dictionary', [node], True, parent)
    qtbot.addWidget(dialog)
    assert dialog.date_field.date.calendarWidget().locale().name().startswith('en_')
    parent.language_context.set_language(SIMPLIFIED_CHINESE)
    assert dialog.date_field.date.calendarWidget().locale().name() == 'zh_CN'
    assert dialog.date_field.value() == value


def test_structured_error_templates_have_chinese_translations():
    import ast
    import json
    from pathlib import Path
    from string import Formatter

    source = Path(__file__).resolve().parents[1] / 'src' / 'planbak'
    catalog = json.loads((source / 'translations' / 'zh_CN.json').read_text(encoding='utf-8'))
    missing = []

    def fields(value):
        return {key for _, key, _, _ in Formatter().parse(value) if key}

    for path in source.rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in {'BackupError', 'PlistValidationError'}:
                continue
            for keyword in node.keywords:
                if keyword.arg != 'english' or not isinstance(keyword.value, ast.Constant):
                    continue
                text = keyword.value.value
                if not isinstance(text, str):
                    continue
                if text not in catalog:
                    missing.append(f'{path.name}:{node.lineno}: {text}')
                    continue
                assert fields(text) == fields(catalog[text]), f'{path.name}:{node.lineno}: {text}'
    assert not missing, '\n'.join(missing)
