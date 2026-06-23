from __future__ import annotations

import importlib.util
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "src" / "photo_culler.py"
SAMPLE_IMAGE = ROOT / "docs" / "screenshots" / "sample-photo.jpg"
DATA_DIR = ROOT / ".tmp" / "visual-original-end-data"
SCREENSHOT_DIR = ROOT / ".tmp" / "screenshots"
ORIGINAL_SCREENSHOT = SCREENSHOT_DIR / "original-main.png"
END_SCREENSHOT = SCREENSHOT_DIR / "end-marker.png"


def load_app_module():
    spec = importlib.util.spec_from_file_location("photo_culler_gui_original_end", APP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load app module: {APP_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def create_sample_images() -> None:
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    DATA_DIR.mkdir(parents=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    for index in range(1, 6):
        shutil.copy2(SAMPLE_IMAGE, DATA_DIR / f"IMG_{index:04d}.JPG")


def wait_until(qapp, condition, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if condition():
            return
        time.sleep(0.05)
    raise TimeoutError("Timed out waiting for GUI condition")


def main() -> None:
    create_sample_images()
    module = load_app_module()

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    window = module.PhotoCullerWindow()
    window.resize(1280, 820)
    window.show()
    app.processEvents()

    window.load_folder(DATA_DIR)
    wait_until(app, lambda: window.view.has_image and window.view.pixmap_item is not None)
    if window.view.source_image is None:
        raise AssertionError("Main image source was not retained")
    source_width = window.view.source_image.width()
    source_height = window.view.source_image.height()
    if source_width <= 1000 or source_height <= 1000:
        raise AssertionError(f"Main source image is unexpectedly small: {source_width}x{source_height}")
    pixmap = window.view.pixmap_item.pixmap()
    if pixmap.width() >= source_width or pixmap.height() >= source_height:
        raise AssertionError(f"Fit preview was not downsampled to viewport: {pixmap.width()}x{pixmap.height()}")
    wait_until(app, lambda: bool(window.main_cache) or not window.pending_main_prefetch, timeout=10.0)
    window.grab().save(str(ORIGINAL_SCREENSHOT))

    window.jump_to(len(window.library.items) - 1)
    wait_until(app, lambda: window.view.has_image and window.index_label.text().startswith("5 / 5"))
    window.navigate(1)
    wait_until(app, lambda: window.end_marker)
    window.grab().save(str(END_SCREENSHOT))

    print(f"original_screenshot={ORIGINAL_SCREENSHOT}")
    print(f"end_screenshot={END_SCREENSHOT}")
    print(f"prefetch_cache_items={len(window.main_cache)}")
    print("REAL GUI ORIGINAL/END TEST PASSED")

    window.close()
    app.processEvents()


if __name__ == "__main__":
    main()
