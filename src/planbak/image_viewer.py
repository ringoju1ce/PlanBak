"""Independent resizable image windows with local input and latest-only loading."""

import math
import time

from PySide6.QtCore import QEvent, QPointF, QRectF, QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QImage, QInputDevice, QPainter
from PySide6.QtWidgets import QCheckBox, QMainWindow, QSlider, QWidget

from .document_ui import DocumentStatus, DocumentToolBar, translate_command
from .i18n import ENGLISH, SIMPLIFIED_CHINESE, LanguageContext
from .image_metadata import information_text
from .viewer_tasks import ViewerTask


class ImageCanvas(QWidget):
    navigate = Signal(int)
    scale_changed = Signal(float, bool)
    view_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        context = getattr(parent, "language_context", None) or LanguageContext(parent=self)
        self.setAccessibleName(
            context.translate(
                "Image preview; use arrow keys or the wheel to navigate, Ctrl+wheel to zoom, and drag to pan"
            )
        )
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
        self.message = context.translate("Loading…")
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
        touchpad = (
            event.device().type() == QInputDevice.DeviceType.TouchPad
            or not event.pixelDelta().isNull()
        )
        zooming = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if touchpad and not zooming:
            delta = event.pixelDelta()
            if delta.isNull():
                delta = event.angleDelta() / 8
            self.pan += QPointF(delta)
            self.clamp_pan()
            self.view_changed.emit()
            self.update()
            self._wheel = 0
            return
        if event.phase() == Qt.ScrollPhase.ScrollMomentum:
            return
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
        tr = viewer.language_context.translate
        self.details_button.setText(tr("Image Information"))
        self.details_button.setAccessibleName(tr("Image Information"))
        self.details_button.setToolTip(tr("View image information and EXIF"))

    def show_details(self):
        self.show_text_details(
            lambda: self.window().language_context.translate("Image Information"),
            self.window().image_information,
        )


class ImageViewer(QMainWindow):
    finished = Signal(int)
    activated = Signal()
    load_progress = Signal(object, object, object, object)
    load_finished = Signal(object)
    prefetch_started = Signal(object, str)
    prefetch_finished = Signal(object)
    prefetch_miss = Signal(object, str)

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
        self.language_context = getattr(parent, "language_context", None) or LanguageContext(
            parent=self
        )
        tr = self.language_context.translate
        self._status_source, self._status_values = None, {}
        self._canvas_source = None
        if parent is not None:
            self.setScreen(parent.screen())
        self.items = tuple(items)
        self.index = next(i for i, item in enumerate(self.items) if item[0] == token)
        self.loader, self.accept_image = loader, accept_image
        self._with_progress = with_progress
        self.prefetch_miss.connect(self._prefetch_miss)
        self.task = None
        self.prefetch_task = None
        self._prefetch_queue = []
        self._direction = 0
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
        toolbar = self.toolbar = DocumentToolBar(tr("Image viewer controls"), self)
        self.addToolBar(Qt.ToolBarArea.BottomToolBarArea, toolbar)
        self.previous_button = toolbar.command(
            tr("Previous Image"), lambda: self.navigate(-1), icon="go-previous", hint="Left"
        )
        self.next_button = toolbar.command(
            tr("Next Image"), lambda: self.navigate(1), icon="go-next", hint="Right"
        )
        self.loop_toggle = QCheckBox(tr("Loop"), toolbar)
        self.loop_toggle.setToolTip(tr("Loop through images in this window until it closes"))
        self.loop_toggle.toggled.connect(self.update_navigation)
        toolbar.addWidget(self.loop_toggle)
        toolbar.addSeparator()
        self.fit_button = toolbar.command(
            tr("Fit to Window"), self.canvas.fit, icon="zoom-fit-best"
        )
        self.actual_button = toolbar.command(
            tr("Actual Size"), lambda: self.canvas.zoom(1.0), icon="zoom-original"
        )
        self.actual_button.defaultAction().setToolTip(tr("Actual size: 100% (1:1 pixels)"))
        toolbar.addSeparator()
        self.zoom_slider = ImageZoomSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setAccessibleName(tr("Image zoom"))
        self.zoom_slider.setToolTip(
            tr("Adjust zoom; actual size is 100% (1:1 pixels), fit to window is a separate mode")
        )
        self.zoom_slider.setRange(-1661, 400)
        self.zoom_slider.setMinimumWidth(90)
        self.zoom_slider.setMaximumWidth(240)
        self.zoom_slider.valueChanged.connect(lambda value: self.canvas.zoom(2 ** (value / 100)))
        toolbar.addWidget(self.zoom_slider)
        toolbar.stretch()
        self.reload_button = toolbar.command(tr("Reload"), self.reload, icon="view-refresh")
        self.status = ImageInformationStatus(self)
        self.statusBar().setSizeGripEnabled(True)
        self.statusBar().addWidget(self.status, 1)
        self.canvas.scale_changed.connect(self.update_zoom)
        self._info = ""
        self._information = {}
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
        self.language_context.changed.connect(self.retranslate)
        self.retranslate()
        self.request()

    def set_status(self, source, **values):
        self._status_source, self._status_values = source, values
        values = {
            key: self.language_context.error(value) if isinstance(value, Exception) else value
            for key, value in values.items()
        }
        self.status.setText(self.language_context.translate(source, **values))

    def clear_canvas(self, source):
        self._canvas_source = source
        self.canvas.clear(self.language_context.translate(source))

    def retranslate(self):
        tr = self.language_context.translate
        self.setWindowTitle(tr("{name} — Image Viewer", name=self.items[self.index][1]))
        self.toolbar.setWindowTitle(tr("Image viewer controls"))
        self.toolbar.setAccessibleName(tr("Image viewer controls"))
        self.canvas.setAccessibleName(
            tr(
                "Image preview; use arrow keys or the wheel to navigate, Ctrl+wheel to zoom, and drag to pan"
            )
        )
        for button, source in (
            (self.previous_button, "Previous Image"),
            (self.next_button, "Next Image"),
            (self.fit_button, "Fit to Window"),
            (self.actual_button, "Actual Size"),
            (self.reload_button, "Reload"),
        ):
            translate_command(button, tr(source))
        self.actual_button.defaultAction().setToolTip(tr("Actual size: 100% (1:1 pixels)"))
        self.loop_toggle.setText(tr("Loop"))
        self.loop_toggle.setToolTip(tr("Loop through images in this window until it closes"))
        self.zoom_slider.setAccessibleName(tr("Image zoom"))
        self.zoom_slider.setToolTip(
            tr("Adjust zoom; actual size is 100% (1:1 pixels), fit to window is a separate mode")
        )
        self.status.details_button.setText(tr("Image Information"))
        self.status.details_button.setAccessibleName(tr("Image Information"))
        self.status.details_button.setToolTip(tr("View image information and EXIF"))
        if self._canvas_source and self.canvas.image.isNull():
            self.canvas.message = tr(self._canvas_source)
            self.canvas.update()
        if self._status_source == "{info} · {mode}{scale}%":
            self.update_zoom(self.canvas.scale, self.canvas.fitting)
        elif self._status_source:
            self.set_status(self._status_source, **self._status_values)
        elif not self.canvas.image.isNull():
            self.update_zoom(self.canvas.scale, self.canvas.fitting)

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
            mode = self.language_context.translate("Fit to Window") + " · " if fitting else ""
            self.set_status(
                "{info} · {mode}{scale}%", info=self._info, mode=mode, scale=f"{scale * 100:.3g}"
            )

    def image_information(self):
        if self._information:
            return self._information[self.language_context.language]
        return self.language_context.translate(
            "File name: {name}\n\n{status}",
            name=self.items[self.index][1],
            status=self.status.text(),
        )

    def navigate(self, delta):
        target = (
            (self.index + delta) % len(self.items)
            if self.loop_toggle.isChecked()
            else max(0, min(len(self.items) - 1, self.index + delta))
        )
        if not self._closing and target != self.index:
            self._direction = 1 if delta > 0 else -1
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
        self._information = {}
        self._prefetch_queue = []
        if self.prefetch_task:
            self.prefetch_task.cancelled.set()
        self._region_timer.stop()
        self._pending_region = self._rendered_region = None
        if self.region_task:
            self.region_task.cancelled.set()
        self._generation += 1
        self._pending = (self._generation, self.current_token, force)
        name = self.items[self.index][1]
        self.setWindowTitle(self.language_context.translate("{name} — Image Viewer", name=name))
        self.update_navigation()
        self.clear_canvas("Loading…")
        self.set_status(
            "Loading: {name} · {index}/{total}",
            name=name,
            index=self.index + 1,
            total=len(self.items),
        )
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
                if not self.region_task and not self.prefetch_task:
                    self.close()
            elif self._pending:
                self.start_pending()
            elif generation == self._generation:
                if task.error or task.cancelled.is_set():
                    self.clear_canvas(
                        "Unable to display the image; you can navigate to another image."
                    )
                    self.set_status(
                        "Unable to load image: {error}",
                        error=task.error or self.language_context.translate("Operation canceled"),
                    )
                elif not self.accept_image(self, task.result.image):
                    self.clear_canvas(
                        "Open images use too much memory; close some image windows and reload."
                    )
                    self.set_status(
                        "Open images use too much memory; close some image windows and reload."
                    )
                else:
                    result = task.result
                    name = result.name or self.items[self.index][1]
                    self.setWindowTitle(
                        self.language_context.translate("{name} — Image Viewer", name=name)
                    )
                    self._info = (
                        f"{result.format} · {result.width} × {result.height} · "
                        f"{self.index + 1}/{len(self.items)}"
                    )
                    self._information = {
                        language: information_text(result, LanguageContext(language).translate)
                        for language in (ENGLISH, SIMPLIFIED_CHINESE)
                    }
                    self._canvas_source = None
                    self.canvas.set_image(result)
                    if self._with_progress:
                        self.schedule_prefetch()
            task.result = None
            task.deleteLater()

        task.finished.connect(finished)
        task.start()

    def schedule_prefetch(self):
        offsets = (1, -1) if not self._direction else (self._direction, 2 * self._direction)
        self._prefetch_queue = [
            self.items[self.index + offset]
            for offset in offsets
            if 0 <= self.index + offset < len(self.items)
        ]
        self.start_prefetch()

    def start_prefetch(self):
        if self._closing or self.task or self.prefetch_task or not self._prefetch_queue:
            return
        token, name = self._prefetch_queue.pop(0)
        task = ViewerTask(
            lambda cancel: self.loader(
                token,
                cancel,
                force=False,
                on_miss=lambda: self.prefetch_miss.emit(task, name),
            ),
            self,
        )
        self.prefetch_task = task

        def finished():
            self.prefetch_task = None
            self.prefetch_finished.emit(task)
            task.result = None
            task.deleteLater()
            if self._closing:
                if not self.task and not self.region_task:
                    self.close()
            else:
                self.start_prefetch()

        task.finished.connect(finished)
        task.start()

    def _prefetch_miss(self, task, name):
        if task is self.prefetch_task and not task.cancelled.is_set() and not self._closing:
            self.prefetch_started.emit(task, name)

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
        self.set_status("{info} · Loading the visible region…", info=self._info)

        def finished():
            self.region_task = None
            if self._closing:
                if not self.task and not self.prefetch_task:
                    self.close()
            elif generation == self._generation and self._pending_region == key:
                if not task.cancelled.is_set() and not task.error:
                    if self.accept_image(self, task.result, True):
                        self.canvas.region, self.canvas.region_rect = task.result, rect
                        self._rendered_region = key
                        self.canvas.update()
                        self.update_zoom(self.canvas.scale, self.canvas.fitting)
                    else:
                        self.set_status(
                            "Visible region cache exceeds the memory budget; close some image windows."
                        )
                elif task.error and not task.cancelled.is_set():
                    self.set_status(
                        "Unable to load image details: {error}; showing the preview.",
                        error=task.error,
                    )
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
        if self.task or self.region_task or self.prefetch_task:
            self._closing = True
            self._prefetch_queue = []
            if self.prefetch_task:
                self.prefetch_task.cancelled.set()
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
