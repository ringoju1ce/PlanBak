"""Independent resizable image windows with local input and latest-only loading."""

import math
import time

from PySide6.QtCore import QEvent, QPointF, QRectF, QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QImage, QPainter
from PySide6.QtWidgets import QCheckBox, QMainWindow, QSlider, QWidget

from .document_ui import DocumentStatus, DocumentToolBar
from .image_metadata import information_text
from .viewer_tasks import ViewerTask


class ImageCanvas(QWidget):
    navigate = Signal(int)
    scale_changed = Signal(float, bool)
    view_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("图片预览；左右键或滚轮切图，Ctrl+滚轮缩放，拖动平移")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.grabGesture(Qt.GestureType.PinchGesture)
        self.image = QImage()
        self.region = QImage()
        self.region_rect = None
        self.large = self.lease = None
        self.full_width = self.full_height = 0
        self.scale = 1.0
        self.fitting = True
        self.pan = QPointF()
        self.message = "正在加载…"
        self._drag = None
        self._wheel = 0.0
        self._wheel_mode = None
        self._wheel_time = 0.0
        self._pinch_scale = 1.0
        self._pinching = False

    def set_image(self, result):
        self.image = result.image
        self.large = result.large
        self.lease = result.lease or (result.large.lease() if result.large else None)
        self.full_width, self.full_height = result.width, result.height
        self.message = ""
        self.fit()

    def clear(self, message):
        self.image = self.region = QImage()
        self.large = self.lease = None
        self.region_rect = None
        self.full_width = self.full_height = 0
        self.message = message
        self._drag = None
        self.update()

    def fit(self):
        self.fitting = True
        self.pan = QPointF()
        if not self.image.isNull():
            ratio = self.devicePixelRatioF()
            self.scale = min(
                1.0,
                max(1, self.width() - 16) * ratio / self.full_width,
                max(1, self.height() - 16) * ratio / self.full_height,
            )
        self.scale_changed.emit(self.scale, True)
        self.view_changed.emit()
        self.update()

    def zoom(self, scale, anchor=None):
        if self.image.isNull() or not math.isfinite(scale):
            return
        scale = max(0.00001, min(16.0, scale))
        center = QPointF(self.rect().center())
        anchor = anchor if anchor is not None else center
        self.pan = anchor - (anchor - center - self.pan) * (scale / self.scale) - center
        self.scale, self.fitting = scale, False
        self.clamp_pan()
        self.scale_changed.emit(scale, False)
        self.view_changed.emit()
        self.update()

    def clamp_pan(self):
        if self.image.isNull():
            return
        ratio = self.scale / self.devicePixelRatioF()
        limit_x = max(0, (self.full_width * ratio - self.width()) / 2)
        limit_y = max(0, (self.full_height * ratio - self.height()) / 2)
        self.pan = QPointF(
            max(-limit_x, min(limit_x, self.pan.x())),
            max(-limit_y, min(limit_y, self.pan.y())),
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fitting:
            self.fit()
        else:
            self.clamp_pan()
            self.view_changed.emit()
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())
        if self.image.isNull():
            painter.setPen(self.palette().text().color())
            painter.drawText(
                self.rect().adjusted(20, 20, -20, -20),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self.message,
            )
            return
        ratio = self.scale / self.devicePixelRatioF()
        width, height = self.full_width * ratio, self.full_height * ratio
        center = QPointF(self.rect().center()) + self.pan
        target = QRectF(center.x() - width / 2, center.y() - height / 2, width, height)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.scale < 1.0)
        self.draw_visible(painter, target, self.image)
        if self.region_rect and not self.region.isNull():
            x, y, width, height = self.region_rect
            region_target = QRectF(
                target.x() + x * ratio, target.y() + y * ratio, width * ratio, height * ratio
            )
            self.draw_visible(painter, region_target, self.region)

    def draw_visible(self, painter, target, image):
        # Keep raster paint coordinates bounded even for million-pixel edges.
        visible = target.intersected(QRectF(self.rect()))
        if visible.isEmpty():
            return
        source = QRectF(
            (visible.x() - target.x()) / target.width() * image.width(),
            (visible.y() - target.y()) / target.height() * image.height(),
            visible.width() / target.width() * image.width(),
            visible.height() / target.height() * image.height(),
        )
        painter.drawImage(visible, image, source)

    def viewport_request(self):
        if not self.large or self.image.isNull():
            return None
        preview_scale = min(
            self.image.width() / self.full_width, self.image.height() / self.full_height
        )
        if self.scale <= preview_scale * 1.05:
            self.region = QImage()
            self.region_rect = None
            return None
        ratio = self.scale / self.devicePixelRatioF()
        center = QPointF(self.rect().center()) + self.pan
        left = center.x() - self.full_width * ratio / 2
        top = center.y() - self.full_height * ratio / 2
        x, y = max(0, math.floor(-left / ratio)), max(0, math.floor(-top / ratio))
        right = min(self.full_width, math.ceil((self.width() - left) / ratio))
        bottom = min(self.full_height, math.ceil((self.height() - top) / ratio))
        width, height = right - x, bottom - y
        if width <= 0 or height <= 0:
            return None
        return (x, y, width, height), (
            max(1, round(width * self.scale)),
            max(1, round(height * self.scale)),
        )

    def wheelEvent(self, event):
        event.accept()
        if not self.window().isActiveWindow() or self._pinching:
            return
        if event.phase() == Qt.ScrollPhase.ScrollMomentum:
            return
        zooming = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        # Qt has already applied the system's natural/classic scroll preference.
        # Do not undo it with event.inverted().
        delta = event.angleDelta().y()
        steps = delta / 120 if delta else event.pixelDelta().y() / 80
        if not steps:
            if event.phase() in (Qt.ScrollPhase.ScrollBegin, Qt.ScrollPhase.ScrollEnd):
                self._wheel = 0
            return
        if zooming:
            anchor = self.mapFromGlobal(event.globalPosition().toPoint())
            self.zoom(
                self.scale * 2 ** max(-4, min(4, steps / 4)),
                QPointF(anchor) if self.rect().contains(anchor) else None,
            )
            self._wheel = 0
            return
        now = time.monotonic()
        if (
            self._wheel_mode != zooming
            or now - self._wheel_time > 0.4
            or self._wheel * steps < 0
            or event.phase() == Qt.ScrollPhase.ScrollBegin
        ):
            self._wheel = 0
        self._wheel_mode, self._wheel_time = zooming, now
        self._wheel += steps
        count = math.trunc(self._wheel)
        if count:
            self._wheel -= count
            self.navigate.emit(-count)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._drag = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.pan += event.position() - self._drag
            self._drag = event.position()
            self.clamp_pan()
            self.view_changed.emit()
            self.update()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag = None
        self.unsetCursor()
        super().mouseReleaseEvent(event)

    def event(self, event):
        if event.type() == QEvent.Type.DevicePixelRatioChange and hasattr(self, "fitting"):
            if self.fitting:
                self.fit()
            else:
                self.clamp_pan()
                self.view_changed.emit()
                self.update()
        if event.type() == QEvent.Type.NativeGesture:
            if not self.window().isActiveWindow():
                event.accept()
                return True
            kind = event.gestureType()
            if kind == Qt.NativeGestureType.BeginNativeGesture:
                self._pinching = True
            elif kind == Qt.NativeGestureType.EndNativeGesture:
                self._pinching = False
            elif kind == Qt.NativeGestureType.ZoomNativeGesture:
                self.zoom(self.scale * max(0.01, 1 + event.value()), event.position())
            event.accept()
            return True
        if event.type() == QEvent.Type.Gesture:
            pinch = event.gesture(Qt.GestureType.PinchGesture)
            if pinch is not None:
                if self.window().isActiveWindow():
                    if pinch.state() == Qt.GestureState.GestureStarted:
                        self._pinch_scale = self.scale
                    self.zoom(self._pinch_scale * pinch.totalScaleFactor())
                event.accept(pinch)
                return True
        return super().event(event)


class ImageZoomSlider(QSlider):
    def wheelEvent(self, event):
        # A normal wheel must not silently turn into zoom over the slider.
        self.window().canvas.wheelEvent(event)


class ImageInformationStatus(DocumentStatus):
    def __init__(self, viewer):
        super().__init__(parent=viewer)
        self.details_button.setText("图片信息")
        self.details_button.setAccessibleName("图片信息")
        self.details_button.setToolTip("查看图片信息和 EXIF")

    def show_details(self):
        self.show_text_details("图片信息", self.window().image_information())


class ImageViewer(QMainWindow):
    finished = Signal(int)
    activated = Signal()
    load_progress = Signal(object, str, object, object)
    load_finished = Signal(object)

    def __init__(
        self,
        items,
        token,
        loader,
        accept_image,
        parent=None,
        *,
        with_progress=False,
        on_load_progress=None,
        on_load_finished=None,
    ):
        super().__init__(None)
        if parent is not None:
            self.setScreen(parent.screen())
        self.items = tuple(items)
        self.index = next(i for i, item in enumerate(self.items) if item[0] == token)
        self.loader, self.accept_image = loader, accept_image
        self._with_progress = with_progress
        self.task = None
        self.region_task = None
        self._pending_region = self._rendered_region = None
        self._region_timer = QTimer(self)
        self._region_timer.setSingleShot(True)
        self._region_timer.setInterval(30)
        self._region_timer.timeout.connect(self.start_region)
        self._generation = 0
        self._pending = None
        self._closing = self._closed = self._skip_prompt = False
        self.last_active = time.monotonic()
        self.canvas = ImageCanvas(self)
        self.setCentralWidget(self.canvas)
        self.canvas.navigate.connect(self.navigate)
        self.canvas.view_changed.connect(self.schedule_region)
        toolbar = DocumentToolBar("图片查看操作", self)
        self.addToolBar(Qt.ToolBarArea.BottomToolBarArea, toolbar)
        self.previous_button = toolbar.command(
            "上一张", lambda: self.navigate(-1), icon="go-previous", hint="Left"
        )
        self.next_button = toolbar.command(
            "下一张", lambda: self.navigate(1), icon="go-next", hint="Right"
        )
        self.loop_toggle = QCheckBox("循环滚动", toolbar)
        self.loop_toggle.setToolTip("首尾循环切换图片；仅对当前窗口生效，关闭窗口后丢弃")
        self.loop_toggle.toggled.connect(self.update_navigation)
        toolbar.addWidget(self.loop_toggle)
        toolbar.addSeparator()
        self.fit_button = toolbar.command("适应窗口", self.canvas.fit, icon="zoom-fit-best")
        self.actual_button = toolbar.command(
            "实际大小", lambda: self.canvas.zoom(1.0), icon="zoom-original"
        )
        self.actual_button.defaultAction().setToolTip("实际大小：100%（像素 1:1）")
        toolbar.addSeparator()
        self.zoom_slider = ImageZoomSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setAccessibleName("图片缩放倍率")
        self.zoom_slider.setToolTip("调整缩放倍率；实际大小为100%（像素1:1），适应窗口是独立模式")
        self.zoom_slider.setRange(-1661, 400)
        self.zoom_slider.setMinimumWidth(90)
        self.zoom_slider.setMaximumWidth(240)
        self.zoom_slider.valueChanged.connect(lambda value: self.canvas.zoom(2 ** (value / 100)))
        toolbar.addWidget(self.zoom_slider)
        toolbar.stretch()
        self.reload_button = toolbar.command("重新加载", self.reload, icon="view-refresh")
        self.status = ImageInformationStatus(self)
        self.statusBar().setSizeGripEnabled(True)
        self.statusBar().addWidget(self.status, 1)
        self.canvas.scale_changed.connect(self.update_zoom)
        self._info = ""
        self._information = ""
        if on_load_progress:
            self.load_progress.connect(on_load_progress)
        if on_load_finished:
            self.load_finished.connect(on_load_finished)
        for sequence, callback in (
            ("Left", lambda: self.navigate(-1)),
            ("Right", lambda: self.navigate(1)),
            ("Escape", self.close),
        ):
            action = QAction(self)
            action.setShortcut(sequence)
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            action.triggered.connect(callback)
            self.addAction(action)
        self.setMinimumSize(QSize(560, 380))
        available = self.screen().availableGeometry().size()
        self.resize(QSize(960, 720).boundedTo(available - QSize(60, 80)))
        self.request()

    @property
    def current_token(self):
        return self.items[self.index][0]

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                self.last_active = time.monotonic()
                self.activated.emit()
            elif hasattr(self, "canvas"):
                self.canvas._pinching = False
                self.canvas._wheel = 0

    def wheelEvent(self, event):
        self.canvas.wheelEvent(event)

    def update_zoom(self, scale, fitting):
        with QSignalBlocker(self.zoom_slider):
            self.zoom_slider.setValue(round(100 * math.log2(max(0.00001, scale))))
        if not self.canvas.image.isNull():
            mode = "适应窗口 · " if fitting else ""
            self.status.setText(f"{self._info} · {mode}{scale * 100:.3g}%")

    def image_information(self):
        if self._information:
            return self._information
        return f"文件名称：{self.items[self.index][1]}\n\n{self.status.text()}"

    def navigate(self, delta):
        target = (
            (self.index + delta) % len(self.items)
            if self.loop_toggle.isChecked()
            else max(0, min(len(self.items) - 1, self.index + delta))
        )
        if not self._closing and target != self.index:
            self.index = target
            self.request()

    def update_navigation(self, *_):
        looping = self.loop_toggle.isChecked() and len(self.items) > 1
        self.previous_button.defaultAction().setEnabled(looping or self.index > 0)
        self.next_button.defaultAction().setEnabled(looping or self.index + 1 < len(self.items))

    def reload(self):
        if not self._closing:
            self.request(force=True)

    def request(self, *, force=False):
        self._information = ""
        self._region_timer.stop()
        self._pending_region = self._rendered_region = None
        if self.region_task:
            self.region_task.cancelled.set()
        self._generation += 1
        self._pending = (self._generation, self.current_token, force)
        name = self.items[self.index][1]
        self.setWindowTitle(f"{name} — 图片查看器")
        self.update_navigation()
        self.canvas.clear("正在加载…")
        self.status.setText(f"正在加载：{name} · {self.index + 1}/{len(self.items)}")
        if self.task:
            self.task.cancelled.set()
        else:
            self.start_pending()

    def start_pending(self):
        if not self._pending or self._closing:
            return
        generation, token, force = self._pending
        self._pending = None
        if self._with_progress:
            task = ViewerTask(
                lambda cancel: self.loader(
                    token, cancel, force=force, progress=self._reporting_progress
                ),
                self,
            )
        else:
            task = ViewerTask(lambda cancel: self.loader(token, cancel, force=force), self)
        self.task = task

        def finished():
            self.task = None
            self.load_finished.emit(task)
            if self._closing:
                if not self.region_task:
                    self.close()
            elif self._pending:
                self.start_pending()
            elif generation == self._generation:
                if task.error or task.cancelled.is_set():
                    self.canvas.clear("无法显示图片；可继续查看上一张或下一张。")
                    self.status.setText(f"无法加载图片：{task.error or '操作已取消'}")
                elif not self.accept_image(self, task.result.image):
                    self.canvas.clear("已打开图片占用过多内存，请关闭部分图片窗口后重新加载。")
                    self.status.setText(self.canvas.message)
                else:
                    result = task.result
                    name = result.name or self.items[self.index][1]
                    self.setWindowTitle(f"{name} — 图片查看器")
                    self._info = (
                        f"{result.format} · {result.width} × {result.height} · "
                        f"{self.index + 1}/{len(self.items)}"
                    )
                    self._information = information_text(result)
                    self.canvas.set_image(result)
            task.result = None
            task.deleteLater()

        task.finished.connect(finished)
        task.start()

    def _reporting_progress(self, text, done, total):
        if self.task is not None:
            self.load_progress.emit(self.task, text, done, total)

    def schedule_region(self):
        request = self.canvas.viewport_request()
        self._pending_region = (self._generation, request) if request else None
        if request is None:
            self._rendered_region = None
        if request and self._pending_region != self._rendered_region and not self._closing:
            self._region_timer.start()

    def start_region(self):
        if self.region_task or not self._pending_region or self._closing:
            return
        key = self._pending_region
        generation, (rect, output) = key
        source = self.canvas.large
        if source is None:
            return
        lease = source.lease()
        task = ViewerTask(lambda cancel: source.render(rect, output, cancel), self)
        task._image_lease = lease
        self.region_task = task
        self.status.setText(f"{self._info} · 正在读取大图可见区域…")

        def finished():
            self.region_task = None
            if self._closing:
                if not self.task:
                    self.close()
            elif generation == self._generation and self._pending_region == key:
                if not task.cancelled.is_set() and not task.error:
                    if self.accept_image(self, task.result, True):
                        self.canvas.region, self.canvas.region_rect = task.result, rect
                        self._rendered_region = key
                        self.canvas.update()
                        self.update_zoom(self.canvas.scale, self.canvas.fitting)
                    else:
                        self.status.setText("可见区域缓存超过内存预算；请关闭部分图片窗口。")
                elif task.error and not task.cancelled.is_set():
                    self.status.setText(f"大图细节读取失败：{task.error}；当前保留缩略预览。")
            task.result = None
            task._image_lease = None
            task.deleteLater()
            if not self._closing and self._pending_region and self._pending_region != key:
                self._region_timer.start()

        task.finished.connect(finished)
        task.start()

    def reject(self):
        self.close()

    def closeEvent(self, event):
        self._region_timer.stop()
        self._pending_region = None
        if self.task or self.region_task:
            self._closing = True
            self._pending = None
            if self.task:
                self.task.cancelled.set()
            if self.region_task:
                self.region_task.cancelled.set()
            self.setEnabled(False)
            event.ignore()
            return
        self._closing = True
        self.canvas.clear("")
        event.accept()
        if not self._closed:
            self._closed = True
            self.finished.emit(0)
