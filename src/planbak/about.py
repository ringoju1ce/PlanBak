"""应用介绍；复用统一的主题、固定尺寸和关闭行为。"""

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QDialogButtonBox, QLabel, QVBoxLayout

from . import __version__
from .dialogs import FixedDialog


class AboutDialog(FixedDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关于 PlanBak")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(14)

        title = QLabel("PlanBak", self)
        font = title.font()
        font.setPointSizeF(font.pointSizeF() * 1.8)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        layout.addWidget(QLabel(f"版本 {__version__}", self))

        subtitle = QLabel("iOS 备份浏览与编辑工具", self)
        font = subtitle.font()
        font.setBold(True)
        subtitle.setFont(font)
        layout.addWidget(subtitle)

        for text in (
            "以文件管理器的方式浏览本地备份，管理文件与文件夹，在多个备份窗口之间复制项目。",
            "内置 plist 编辑器、SQLite 查看器、图片查看器和十六进制查看器。",
            "支持加密与未加密备份。更改先保存在会话中，应用更改时写入可编辑副本，保留原始备份。",
        ):
            label = QLabel(text, self)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            layout.addWidget(label)

        layout.addSpacing(4)
        layout.addWidget(QLabel("基于 Python 与 Qt 构建", self))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.lock_size(self.sizeHint().expandedTo(QSize(520, 0)))
