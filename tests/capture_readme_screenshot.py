from __future__ import annotations

import importlib.util
import shutil
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "src" / "photo_culler.py"
SAMPLE_IMAGE = ROOT / "docs" / "screenshots" / "sample-photo.jpg"
DATA_DIR = ROOT / ".tmp" / "readme-screenshot-data"
README_SCREENSHOT = ROOT / "docs" / "screenshots" / "app-window.png"


def load_app_module():
    spec = importlib.util.spec_from_file_location("photo_culler_readme_screenshot", APP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load app module: {APP_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def wait_until(qapp, condition, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if condition():
            return
        time.sleep(0.05)
    raise TimeoutError("Timed out waiting for GUI condition")


def main() -> None:
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    DATA_DIR.mkdir(parents=True)
    for index in range(1, 4):
        shutil.copy2(SAMPLE_IMAGE, DATA_DIR / f"IMG_{index:04d}.JPG")

    module = load_app_module()
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    window = module.PhotoCullerWindow()
    window.resize(1280, 820)
    window.show()
    app.processEvents()
    window.load_folder(DATA_DIR)
    wait_until(app, lambda: window.view.has_image and window.view.pixmap_item is not None)
    for _ in range(10):
        app.processEvents()
        time.sleep(0.03)
    window.grab().save(str(README_SCREENSHOT))
    print(f"readme_screenshot={README_SCREENSHOT}")
    print("README GUI SCREENSHOT CAPTURED")
    window.close()
    app.processEvents()


if __name__ == "__main__":
    main()
