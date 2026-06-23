from __future__ import annotations

import importlib.util
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "src" / "photo_culler.py"
SAMPLE_IMAGE = ROOT / "docs" / "screenshots" / "sample-photo.jpg"
DATA_DIR = ROOT / ".tmp" / "visual-zoom-pan-data"
SCREENSHOT_DIR = ROOT / ".tmp" / "screenshots"
FIT_SCREENSHOT = SCREENSHOT_DIR / "fit-preview.png"
ZOOM_SCREENSHOT = SCREENSHOT_DIR / "zoom-pan.png"


def load_app_module():
    spec = importlib.util.spec_from_file_location("photo_culler_gui_zoom_pan", APP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load app module: {APP_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def create_sample_image() -> Path:
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    DATA_DIR.mkdir(parents=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "IMG_0001.JPG"
    shutil.copy2(SAMPLE_IMAGE, path)
    return path


def wait_until(qapp, condition, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if condition():
            return
        time.sleep(0.05)
    raise TimeoutError("Timed out waiting for GUI condition")


def main() -> None:
    create_sample_image()
    module = load_app_module()

    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    window = module.PhotoCullerWindow()
    window.resize(1280, 820)
    window.show()
    app.processEvents()
    window.load_folder(DATA_DIR)
    wait_until(app, lambda: window.view.has_image and window.view.pixmap_item is not None)

    source = window.view.source_image
    if source is None or source.width() <= 1000 or source.height() <= 1000:
        raise AssertionError(
            f"Unexpected source image size: {None if source is None else (source.width(), source.height())}"
        )
    if not window.view.using_fit_preview:
        raise AssertionError("Fit preview was not used for default view")

    window.grab().save(str(FIT_SCREENSHOT))
    center = window.view.viewport().rect().center()

    class WheelEvent:
        def angleDelta(self):
            return QPoint(0, 120)

        def position(self):
            return center

        def accept(self):
            return None

    for _ in range(5):
        window.view.wheelEvent(WheelEvent())
        app.processEvents()
    if window.view.view_scale <= window.view.fit_scale:
        raise AssertionError("Zoom in did not increase view scale")

    class MouseEvent:
        def __init__(self, point):
            self._point = point

        def button(self):
            return Qt.MouseButton.LeftButton

        def position(self):
            return self._point

        def accept(self):
            return None

    window.view.mousePressEvent(MouseEvent(QPointF(center.x(), center.y())))
    window.view.mouseMoveEvent(MouseEvent(QPointF(center.x() + 80, center.y() + 40)))
    window.view.mouseReleaseEvent(MouseEvent(QPointF(center.x() + 80, center.y() + 40)))
    window.grab().save(str(ZOOM_SCREENSHOT))

    print(f"fit_screenshot={FIT_SCREENSHOT}")
    print(f"zoom_screenshot={ZOOM_SCREENSHOT}")
    print(f"source_size={source.width()}x{source.height()}")
    print(f"preview_size={window.view.pixmap_item.pixmap().width()}x{window.view.pixmap_item.pixmap().height()}")
    print(f"view_scale={window.view.view_scale:.4f}")
    print("REAL GUI ZOOM/PAN TEST PASSED")

    window.close()
    app.processEvents()


if __name__ == "__main__":
    main()
