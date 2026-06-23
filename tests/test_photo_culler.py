from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "src" / "photo_culler.py"


def load_app_module():
    spec = importlib.util.spec_from_file_location("photo_culler", APP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load src/photo_culler.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_jpg(path: Path, color: tuple[int, int, int]) -> None:
    image = Image.new("RGB", (120, 80), color)
    image.save(path, "JPEG")


def write_raw(path: Path) -> None:
    path.write_bytes(b"NEF-DUMMY")


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    app = load_app_module()
    with tempfile.TemporaryDirectory(prefix="photo-culler-test-") as tmp:
        root = Path(tmp) / "folder with spaces"
        root.mkdir()
        (root / "PICK").mkdir()
        (root / "_TRASH").mkdir()
        write_jpg(root / "DSC_10.JPG", (200, 0, 0))
        write_jpg(root / "DSC_2.jpg", (0, 200, 0))
        write_jpg(root / "DSC_1.JPEG", (0, 0, 200))
        write_jpg(root / "DSC_0001.jpeg", (100, 100, 0))
        write_raw(root / "DSC_2.NEF")
        write_raw(root / "RAW_ONLY.NEF")
        write_jpg(root / "PICK" / "IGNORED.JPG", (1, 1, 1))
        write_jpg(root / "_TRASH" / "IGNORED2.JPG", (2, 2, 2))

        library = app.PhotoLibrary()
        library.load(root)
        names = [item.jpg.name for item in library.items]
        assert_true(
            names == ["DSC_0001.jpeg", "DSC_1.JPEG", "DSC_2.jpg", "DSC_10.JPG"],
            f"sort/scan failed: {names}",
        )
        assert_true(library.items[2].raw and library.items[2].raw.name == "DSC_2.NEF", "NEF pairing failed")

        library.current_index = 2
        msg = library.pick_current()
        assert_true("PICK" in msg, msg)
        assert_true((root / "PICK" / "DSC_2.jpg").exists(), "Pick did not copy JPG")
        assert_true((root / "PICK" / "DSC_2.NEF").exists(), "Pick did not copy NEF")
        assert_true((root / "DSC_2.jpg").exists(), "Pick moved original JPG unexpectedly")
        assert_true((root / "DSC_2.NEF").exists(), "Pick moved original NEF unexpectedly")
        assert_true(library.current().jpg.name == "DSC_10.JPG", "Pick did not auto-advance")

        msg = library.undo()
        assert_true(bool(msg), "Undo pick did not return a status message")
        assert_true(not (root / "PICK" / "DSC_2.jpg").exists(), "Undo pick did not remove copied JPG")
        assert_true(not (root / "PICK" / "DSC_2.NEF").exists(), "Undo pick did not remove copied NEF")
        assert_true(library.current().jpg.name == "DSC_2.jpg", "Undo pick did not return to picked image")

        (root / "PICK" / "DSC_2.jpg").write_text("collision", encoding="utf-8")
        (root / "PICK" / "DSC_2.NEF").write_text("collision", encoding="utf-8")
        msg = library.pick_current()
        assert_true("PICK" in msg, msg)
        assert_true((root / "PICK" / "DSC_2__001.jpg").exists(), "Auto-rename JPG failed")
        assert_true((root / "PICK" / "DSC_2__001.NEF").exists(), "Auto-rename NEF failed")

        picked_index = library.index_for_name("DSC_2.jpg")
        msg = library.unpick_at_index(picked_index)
        assert_true("Pick" in msg or "PICK" in msg, msg)
        assert_true(not (root / "PICK" / "DSC_2__001.jpg").exists(), "Cancel Pick did not remove copied JPG")
        assert_true(not (root / "PICK" / "DSC_2__001.NEF").exists(), "Cancel Pick did not remove copied NEF")
        assert_true(not library.items[picked_index].picked, "Cancel Pick did not clear picked state")

        library.current_index = 1
        msg = library.delete_current()
        assert_true("_TRASH" in msg, msg)
        assert_true(not (root / "DSC_1.JPEG").exists(), "Delete did not move JPG")
        assert_true((root / "_TRASH" / "DSC_1.JPEG").exists(), "Delete target missing")
        assert_true("DSC_1.JPEG" not in [item.jpg.name for item in library.items], "Deleted image still active")
        assert_true(library.current_index == 1, "Delete did not keep current index on the next image")
        assert_true(library.current().jpg.name == "DSC_2.jpg", "Delete did not advance to the shifted image")

        msg = library.undo()
        assert_true(bool(msg), "Undo delete did not return a status message")
        assert_true((root / "DSC_1.JPEG").exists(), "Undo delete did not restore JPG")
        assert_true(library.current().jpg.name == "DSC_1.JPEG", "Undo delete did not return to deleted image")

        library.current_index = 0
        msg = library.delete_current()
        assert_true("_TRASH" in msg, msg)
        assert_true((root / "_TRASH" / "DSC_0001.jpeg").exists(), "Delete JPG-only failed")

        state_path = root / app.STATE_NAME
        assert_true(state_path.exists(), "State file not written")

        empty = Path(tmp) / "empty"
        empty.mkdir()
        library.load(empty)
        assert_true(library.current_index == -1 and not library.items, "Empty folder handling failed")

        raw_only = Path(tmp) / "raw only"
        raw_only.mkdir()
        write_raw(raw_only / "ONLY.NEF")
        library.load(raw_only)
        assert_true(library.current_index == -1 and not library.items, "Raw-only folder handling failed")

        loader = Path(tmp) / "loader"
        loader.mkdir()
        full_size_path = loader / "FULL_SIZE.JPG"
        Image.new("RGB", (1234, 777), (12, 80, 160)).save(full_size_path, "JPEG", quality=95)
        full_image = app.load_image_as_qimage(str(full_size_path), max_dimension=None)
        assert_true(
            full_image.width() == 1234 and full_image.height() == 777,
            f"Main image was resized unexpectedly: {full_image.width()}x{full_image.height()}",
        )
        thumb_image = app.load_image_as_qimage(str(full_size_path), max_dimension=160)
        assert_true(max(thumb_image.width(), thumb_image.height()) <= 160, "Thumbnail decode was not resized")

        shutil.rmtree(root / "PICK", ignore_errors=True)
        shutil.rmtree(root / "_TRASH", ignore_errors=True)

    print("ALL PHOTO CULLER VARIABLE TESTS PASSED")


if __name__ == "__main__":
    main()
