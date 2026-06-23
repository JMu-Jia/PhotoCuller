# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sys
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path


APP_TITLE = "Photo Culler"
IMAGE_EXTENSIONS = {".jpg", ".jpeg"}
RAW_EXTENSIONS = {".nef"}
STATE_NAME = ".photo-culler-state.json"
THUMB_ICON_SIZE = (92, 62)
THUMB_GRID_SIZE = (108, 82)
THUMB_VISIBLE_PADDING = 8
MAIN_MAX_DIMENSION = None
MAIN_PREFETCH_AHEAD = 2
MAIN_CACHE_MAX_BYTES = 1400 * 1024 * 1024
PREFETCH_TOKEN = -1


def configure_qt_environment() -> None:
    runtime = Path(sys.executable).resolve().parent
    site_packages = runtime / "Lib" / "site-packages"
    pyside_dir = site_packages / "PySide6"
    shiboken_dir = site_packages / "shiboken6"

    for path in (pyside_dir, shiboken_dir):
        if path.exists():
            os.environ["PATH"] = str(path) + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(str(path))
            except Exception:
                pass

    plugins = pyside_dir / "plugins"
    qml = pyside_dir / "qml"
    if plugins.exists():
        os.environ.setdefault("QT_PLUGIN_PATH", str(plugins))
    if qml.exists():
        os.environ.setdefault("QML2_IMPORT_PATH", str(qml))


def natural_key(path: Path) -> list[object]:
    parts = re.split(r"(\d+)", path.name.lower())
    return [int(part) if part.isdigit() else part for part in parts]


def available_destination(folder: Path, source: Path) -> Path:
    folder.mkdir(exist_ok=True)
    candidate = folder / source.name
    if not candidate.exists():
        return candidate

    for index in range(1, 10000):
        candidate = folder / f"{source.stem}__{index:03d}{source.suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法生成不重名文件名：{source.name}")


@dataclass
class PhotoItem:
    jpg: Path
    raw: Path | None
    picked: bool = False
    rotation: int = 0


class PhotoLibrary:
    def __init__(self) -> None:
        self.folder: Path | None = None
        self.items: list[PhotoItem] = []
        self.current_index = -1
        self.picked_names: set[str] = set()
        self.pick_copies: dict[str, list[str]] = {}
        self.last_action: dict | None = None

    @property
    def state_path(self) -> Path | None:
        return None if self.folder is None else self.folder / STATE_NAME

    def load(self, folder: str | Path) -> None:
        self.folder = Path(folder)
        self.picked_names = set()
        self.pick_copies = {}
        self.current_index = 0
        self.last_action = None
        self._load_state()

        raw_by_stem: dict[str, Path] = {}
        jpg_files: list[Path] = []
        for child in self.folder.iterdir():
            if not child.is_file():
                continue
            suffix = child.suffix.lower()
            if suffix in RAW_EXTENSIONS:
                raw_by_stem.setdefault(child.stem.lower(), child)
            elif suffix in IMAGE_EXTENSIONS:
                jpg_files.append(child)

        jpg_files.sort(key=natural_key)
        self.items = [
            PhotoItem(
                jpg=jpg,
                raw=raw_by_stem.get(jpg.stem.lower()),
                picked=jpg.name.lower() in self.picked_names,
            )
            for jpg in jpg_files
        ]

        if self.items:
            self.current_index = max(0, min(self.current_index, len(self.items) - 1))
        else:
            self.current_index = -1

    def _load_state(self) -> None:
        path = self.state_path
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.current_index = int(data.get("current_index", 0))
            self.picked_names = {str(name).lower() for name in data.get("picked", [])}
            raw_copies = data.get("pick_copies", {})
            if isinstance(raw_copies, dict):
                self.pick_copies = {
                    str(name).lower(): [str(path) for path in paths]
                    for name, paths in raw_copies.items()
                    if isinstance(paths, list)
                }
        except Exception:
            self.current_index = 0
            self.picked_names = set()
            self.pick_copies = {}

    def save_state(self) -> None:
        path = self.state_path
        if path is None:
            return
        data = {
            "current_index": max(self.current_index, 0),
            "picked": sorted(self.picked_names),
            "pick_copies": {
                name: paths
                for name, paths in sorted(self.pick_copies.items())
                if name in self.picked_names
            },
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def current(self) -> PhotoItem | None:
        if 0 <= self.current_index < len(self.items):
            return self.items[self.current_index]
        return None

    def jump(self, index: int) -> None:
        if not self.items:
            self.current_index = -1
        else:
            self.current_index = max(0, min(index, len(self.items) - 1))
        self.save_state()

    def advance_after_action(self) -> None:
        if not self.items:
            self.current_index = -1
        else:
            self.current_index = min(self.current_index + 1, len(self.items) - 1)
        self.save_state()

    def index_for_name(self, name: str) -> int:
        target = name.lower()
        for index, item in enumerate(self.items):
            if item.jpg.name.lower() == target:
                return index
        return -1

    def pick_sources(self, item: PhotoItem) -> list[Path]:
        sources = [item.jpg]
        if item.raw and item.raw.exists():
            sources.append(item.raw)
        return sources

    def prepare_pick_current(self) -> tuple[str, dict | None]:
        item = self.current()
        if item is None or self.folder is None:
            return "没有当前照片", None

        if item.picked:
            return self.unpick_current(), None

        pick_folder = self.folder / "PICK"
        copies: list[tuple[str, str]] = []
        copied_for_undo: list[tuple[str, str]] = []
        for source in self.pick_sources(item):
            destination = available_destination(pick_folder, source)
            copies.append((str(source), str(destination)))
            copied_for_undo.append((str(destination), str(source)))

        name = item.jpg.name.lower()
        item.picked = True
        self.picked_names.add(name)
        self.pick_copies[name] = [destination for _source, destination in copies]
        self.last_action = {
            "type": "pick",
            "name": name,
            "copied": copied_for_undo,
            "index": self.current_index,
        }
        action = {
            "type": "pick",
            "name": name,
            "copies": copies,
            "index": self.current_index,
        }
        self.save_state()
        self.advance_after_action()
        return "已标记 PICK，正在后台复制 JPG + NEF", action

    def pick_current(self) -> str:
        message, action = self.prepare_pick_current()
        if action:
            for source, destination in action["copies"]:
                Path(destination).parent.mkdir(exist_ok=True)
                shutil.copy2(source, destination)
        return message

    def pick_fallback_paths(self, item: PhotoItem) -> list[Path]:
        if self.folder is None:
            return []
        pick_folder = self.folder / "PICK"
        return [pick_folder / source.name for source in self.pick_sources(item)]

    def safe_unlink_pick_copy(self, path: str | Path) -> bool:
        if self.folder is None:
            return False
        pick_folder = (self.folder / "PICK").resolve()
        target = Path(path)
        try:
            resolved = target.resolve()
            if pick_folder not in [resolved, *resolved.parents]:
                return False
            if resolved.exists() and resolved.is_file():
                resolved.unlink()
                return True
        except Exception:
            return False
        return False

    def unpick_current(self) -> str:
        item = self.current()
        if item is None:
            return "没有当前照片"
        return self.unpick_at_index(self.current_index)

    def unpick_at_index(self, index: int) -> str:
        if not (0 <= index < len(self.items)):
            return "没有当前照片"
        item = self.items[index]
        name = item.jpg.name.lower()
        if not item.picked:
            return "当前照片还没有 Pick"

        paths = [Path(path) for path in self.pick_copies.get(name, [])]
        if not paths:
            paths = self.pick_fallback_paths(item)

        removed: list[str] = []
        for path in paths:
            if self.safe_unlink_pick_copy(path):
                removed.append(str(path))

        item.picked = False
        self.picked_names.discard(name)
        self.pick_copies.pop(name, None)
        self.last_action = {
            "type": "unpick",
            "name": name,
            "index": index,
            "removed": removed,
            "sources": [str(source) for source in self.pick_sources(item)],
        }
        self.save_state()
        return "已取消 Pick"

    def delete_current(self) -> str:
        item = self.current()
        if item is None or self.folder is None:
            return "没有当前照片"

        trash_folder = self.folder / "_TRASH"
        moved: list[tuple[str, str]] = []

        jpg_dest = available_destination(trash_folder, item.jpg)
        shutil.move(str(item.jpg), str(jpg_dest))
        moved.append((str(jpg_dest), str(item.jpg)))

        if item.raw and item.raw.exists():
            raw_dest = available_destination(trash_folder, item.raw)
            shutil.move(str(item.raw), str(raw_dest))
            moved.append((str(raw_dest), str(item.raw)))

        removed_index = self.current_index
        removed_name = item.jpg.name.lower()
        was_picked = item.picked
        del self.items[self.current_index]
        self.picked_names.discard(removed_name)

        self.last_action = {
            "type": "delete",
            "moved": moved,
            "index": removed_index,
            "name": removed_name,
            "picked": was_picked,
        }
        if self.items:
            self.current_index = min(removed_index, len(self.items) - 1)
        else:
            self.current_index = -1
        self.save_state()
        return "已移动 JPG + NEF 到 _TRASH"

    def undo(self) -> str:
        if not self.last_action:
            return "没有可撤销操作"

        action = self.last_action
        self.last_action = None
        action_type = action.get("type")

        if action_type == "pick":
            for copied, _original in action.get("copied", []):
                copied_path = Path(copied)
                if copied_path.exists():
                    copied_path.unlink()

            name = str(action.get("name", "")).lower()
            self.picked_names.discard(name)
            self.pick_copies.pop(name, None)
            for index, item in enumerate(self.items):
                if item.jpg.name.lower() == name:
                    item.picked = False
                    self.current_index = index
                    break
            self.save_state()
            return "已撤销 Pick"

        if action_type == "unpick":
            name = str(action.get("name", "")).lower()
            removed = [str(path) for path in action.get("removed", [])]
            sources = [Path(path) for path in action.get("sources", [])]
            restored: list[str] = []
            for source, destination in zip(sources, removed):
                if not source.exists():
                    continue
                destination_path = Path(destination)
                if destination_path.exists():
                    destination_path = available_destination(destination_path.parent, destination_path)
                destination_path.parent.mkdir(exist_ok=True)
                shutil.copy2(source, destination_path)
                restored.append(str(destination_path))

            self.picked_names.add(name)
            if restored:
                self.pick_copies[name] = restored
            for index, item in enumerate(self.items):
                if item.jpg.name.lower() == name:
                    item.picked = True
                    self.current_index = index
                    break
            self.save_state()
            return "已撤销取消 Pick"

        if action_type == "delete":
            restored_jpg: Path | None = None
            for source, destination in action.get("moved", []):
                source_path = Path(source)
                destination_path = Path(destination)
                if not source_path.exists():
                    continue
                if destination_path.exists():
                    destination_path = available_destination(destination_path.parent, destination_path)
                shutil.move(str(source_path), str(destination_path))
                if destination_path.suffix.lower() in IMAGE_EXTENSIONS:
                    restored_jpg = destination_path

            if self.folder:
                self.load(self.folder)
                target_name = (restored_jpg.name if restored_jpg else str(action.get("name", ""))).lower()
                for index, item in enumerate(self.items):
                    if item.jpg.name.lower() == target_name:
                        self.current_index = index
                        break
                if action.get("picked"):
                    self.picked_names.add(target_name)
                    for item in self.items:
                        if item.jpg.name.lower() == target_name:
                            item.picked = True
                            break
            self.save_state()
            return "已撤销删除"

        return "未知撤销操作"


configure_qt_environment()

from PIL import Image, ImageCms, ImageOps

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QThreadPool, QTimer, QRunnable, Signal, Slot
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

Image.MAX_IMAGE_PIXELS = None


def fit_size(width: int, height: int, max_dimension: int) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        return 1, 1
    if max(width, height) <= max_dimension:
        return width, height
    scale = max_dimension / max(width, height)
    return max(1, int(width * scale)), max(1, int(height * scale))


def exif_orientation(image: Image.Image) -> int:
    try:
        return int(image.getexif().get(274, 1))
    except Exception:
        return 1


def convert_to_srgb(image: Image.Image) -> Image.Image:
    icc_profile = image.info.get("icc_profile")
    if icc_profile:
        try:
            source_profile = ImageCms.ImageCmsProfile(BytesIO(icc_profile))
            target_profile = ImageCms.createProfile("sRGB")
            working = image if image.mode in {"RGB", "RGBA", "L"} else image.convert("RGB")
            return ImageCms.profileToProfile(working, source_profile, target_profile, outputMode="RGB")
        except Exception:
            pass
    if image.mode == "RGB":
        return image
    if image.mode == "RGBA":
        background = Image.new("RGB", image.size, (0, 0, 0))
        background.paste(image, mask=image.getchannel("A"))
        return background
    return image.convert("RGB")


def pillow_to_qimage(image: Image.Image) -> QImage:
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    data = rgb.tobytes("raw", "RGB")
    return QImage(data, rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888).copy()


def load_image_as_qimage(path: str, rotation: int = 0, max_dimension: int | None = None) -> QImage:
    with Image.open(path) as source:
        if max_dimension:
            orientation = exif_orientation(source)
            width, height = source.size
            oriented_width, oriented_height = (height, width) if orientation in {5, 6, 7, 8} else (width, height)
            target_width, target_height = fit_size(oriented_width, oriented_height, max_dimension)
            draft_size = (target_height, target_width) if orientation in {5, 6, 7, 8} else (target_width, target_height)
            source.draft("RGB", draft_size)

        image = ImageOps.exif_transpose(source)
        image = convert_to_srgb(image)

        if max_dimension and max(image.size) > max_dimension:
            image = image.resize(fit_size(image.width, image.height, max_dimension), Image.Resampling.LANCZOS)

        if rotation:
            image = image.rotate(-rotation, expand=True, resample=Image.Resampling.BICUBIC)

        return pillow_to_qimage(image)


class WorkerSignals(QObject):
    main_loaded = Signal(int, str, QImage, str)
    thumb_loaded = Signal(str, QImage, str)
    pick_copy_finished = Signal(dict, bool, str)


class ImageLoadTask(QRunnable):
    def __init__(
        self,
        signals: WorkerSignals,
        path: str,
        token: int = 0,
        rotation: int = 0,
        max_dimension: int | None = None,
        thumbnail: bool = False,
    ) -> None:
        super().__init__()
        self.signals = signals
        self.path = path
        self.token = token
        self.rotation = rotation
        self.max_dimension = max_dimension
        self.thumbnail = thumbnail

    @Slot()
    def run(self) -> None:
        image = QImage()
        error = ""
        try:
            image = load_image_as_qimage(self.path, rotation=self.rotation, max_dimension=self.max_dimension)
            if image.isNull():
                error = "无法读取图片"
        except Exception as exc:
            error = str(exc)

        if self.thumbnail:
            self.signals.thumb_loaded.emit(self.path, image, error)
        else:
            self.signals.main_loaded.emit(self.token, self.path, image, error)


class FileCopyTask(QRunnable):
    def __init__(self, signals: WorkerSignals, action: dict) -> None:
        super().__init__()
        self.signals = signals
        self.action = action

    @Slot()
    def run(self) -> None:
        try:
            for source, destination in self.action.get("copies", []):
                destination_path = Path(destination)
                destination_path.parent.mkdir(exist_ok=True)
                shutil.copy2(source, destination_path)
        except Exception as exc:
            self.signals.pick_copy_finished.emit(self.action, False, str(exc))
            return
        self.signals.pick_copy_finished.emit(self.action, True, "")


class ImageView(QGraphicsView):
    open_folder_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.scene_obj = QGraphicsScene(self)
        self.setScene(self.scene_obj)
        self.pixmap_item = None
        self.message_item: QGraphicsTextItem | None = None
        self.source_image: QImage | None = None
        self.has_image = False
        self.auto_fit = True
        self.fit_scale = 1.0
        self.view_scale = 1.0
        self.center_x = 0.0
        self.center_y = 0.0
        self.crop_left = 0
        self.crop_top = 0
        self.crop_scale = 1.0
        self.pixmap_offset_x = 0.0
        self.pixmap_offset_y = 0.0
        self.using_fit_preview = False
        self.dragging = False
        self.last_drag_pos = None

        self.setBackgroundBrush(QBrush(QColor("#050505")))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform | QPainter.RenderHint.Antialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def set_message(self, title: str, subtitle: str = "") -> None:
        self.resetTransform()
        self.scene_obj.clear()
        self.pixmap_item = None
        self.source_image = None
        self.has_image = False
        self.auto_fit = True
        self.fit_scale = 1.0
        self.view_scale = 1.0
        self.center_x = 0.0
        self.center_y = 0.0
        self.dragging = False
        self.last_drag_pos = None
        self.using_fit_preview = False
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        title_html = html.escape(title)
        subtitle_html = html.escape(subtitle)
        body = (
            f"<div style='color:#d7dde7; font-family:\"Microsoft YaHei UI\"; text-align:center;'>"
            f"<div style='font-size:28pt; font-weight:600; line-height:1.25;'>{title_html}</div>"
            f"<div style='font-size:14pt; margin-top:14px; line-height:1.45;'>{subtitle_html}</div>"
            f"</div>"
        )
        self.message_item = self.scene_obj.addText("")
        self.message_item.setHtml(body)
        self.scene_obj.setSceneRect(0, 0, max(self.width(), 800), max(self.height(), 500))
        self.message_item.setTextWidth(min(self.sceneRect().width() * 0.78, 820))
        self._center_message()

    def set_image(self, image: QImage) -> None:
        self.source_image = image
        self.has_image = True
        self.auto_fit = True
        self.center_x = image.width() / 2
        self.center_y = image.height() / 2
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        self.fit_to_window()

    def _fit_scale_for_viewport(self) -> float:
        if not self.source_image or self.source_image.isNull():
            return 1.0
        viewport_size = self.viewport().size()
        return min(
            max(1, viewport_size.width()) / max(1, self.source_image.width()),
            max(1, viewport_size.height()) / max(1, self.source_image.height()),
        )

    def fit_to_window(self) -> None:
        if not self.source_image or self.source_image.isNull():
            return
        self.auto_fit = True
        self.resetTransform()
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.fit_scale = self._fit_scale_for_viewport()
        self.view_scale = self.fit_scale
        self.center_x = self.source_image.width() / 2
        self.center_y = self.source_image.height() / 2
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        self.render_current_view()

    def _clamp_center(self) -> None:
        if not self.source_image or self.source_image.isNull():
            return
        viewport_width = max(1, self.viewport().width())
        viewport_height = max(1, self.viewport().height())
        visible_width = min(self.source_image.width(), viewport_width / max(self.view_scale, 0.0001))
        visible_height = min(self.source_image.height(), viewport_height / max(self.view_scale, 0.0001))
        half_width = visible_width / 2
        half_height = visible_height / 2
        self.center_x = min(max(self.center_x, half_width), self.source_image.width() - half_width)
        self.center_y = min(max(self.center_y, half_height), self.source_image.height() - half_height)

    def _source_at_view_pos(self, x: float, y: float) -> tuple[float, float]:
        source_x = self.crop_left + (x - self.pixmap_offset_x) / max(self.crop_scale, 0.0001)
        source_y = self.crop_top + (y - self.pixmap_offset_y) / max(self.crop_scale, 0.0001)
        if self.source_image:
            source_x = min(max(source_x, 0.0), float(self.source_image.width()))
            source_y = min(max(source_y, 0.0), float(self.source_image.height()))
        return source_x, source_y

    def render_current_view(self) -> None:
        if not self.source_image or self.source_image.isNull():
            return
        viewport_width = max(1, self.viewport().width())
        viewport_height = max(1, self.viewport().height())
        self.scene_obj.clear()
        self.resetTransform()
        self.scene_obj.setSceneRect(0, 0, viewport_width, viewport_height)
        self._clamp_center()

        source_width = self.source_image.width()
        source_height = self.source_image.height()
        scaled_width = max(1, int(round(source_width * self.view_scale)))
        scaled_height = max(1, int(round(source_height * self.view_scale)))

        if scaled_width <= viewport_width and scaled_height <= viewport_height:
            display = self.source_image.scaled(
                scaled_width,
                scaled_height,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.crop_left = 0
            self.crop_top = 0
            self.crop_scale = self.view_scale
            self.pixmap_offset_x = (viewport_width - scaled_width) / 2
            self.pixmap_offset_y = (viewport_height - scaled_height) / 2
        else:
            crop_width = min(source_width, max(1, int(viewport_width / max(self.view_scale, 0.0001)) + 2))
            crop_height = min(source_height, max(1, int(viewport_height / max(self.view_scale, 0.0001)) + 2))
            left = int(round(self.center_x - crop_width / 2))
            top = int(round(self.center_y - crop_height / 2))
            left = min(max(left, 0), max(0, source_width - crop_width))
            top = min(max(top, 0), max(0, source_height - crop_height))
            crop = self.source_image.copy(left, top, crop_width, crop_height)
            display_width = max(1, int(round(crop_width * self.view_scale)))
            display_height = max(1, int(round(crop_height * self.view_scale)))
            display = crop.scaled(
                display_width,
                display_height,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.crop_left = left
            self.crop_top = top
            self.crop_scale = self.view_scale
            self.pixmap_offset_x = (viewport_width - display_width) / 2
            self.pixmap_offset_y = (viewport_height - display_height) / 2

        self.pixmap_item = self.scene_obj.addPixmap(QPixmap.fromImage(display))
        self.pixmap_item.setPos(self.pixmap_offset_x, self.pixmap_offset_y)
        self.using_fit_preview = True

    def _set_zoom_around(self, new_scale: float, view_x: float, view_y: float) -> None:
        if not self.source_image or self.source_image.isNull():
            return
        source_x, source_y = self._source_at_view_pos(view_x, view_y)
        self.view_scale = new_scale
        self.auto_fit = abs(self.view_scale - self.fit_scale) < 0.0001
        self.center_x = source_x + (self.viewport().width() / 2 - view_x) / max(new_scale, 0.0001)
        self.center_y = source_y + (self.viewport().height() / 2 - view_y) / max(new_scale, 0.0001)
        self.render_current_view()

    def _show_source_image(
        self,
        scale_factor: float | None = None,
        focus_x_ratio: float | None = None,
        focus_y_ratio: float | None = None,
    ) -> None:
        if not self.source_image or self.source_image.isNull():
            return
        self.view_scale = scale_factor if scale_factor is not None else self.fit_scale
        self.scene_obj.clear()
        if focus_x_ratio is not None and focus_y_ratio is not None:
            self.center_x = focus_x_ratio * self.source_image.width()
            self.center_y = focus_y_ratio * self.source_image.height()
        else:
            self.center_x = self.source_image.width() / 2
            self.center_y = self.source_image.height() / 2
        self.render_current_view()

    def wheelEvent(self, event) -> None:
        if not self.has_image:
            event.accept()
            return
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.fit_scale = self._fit_scale_for_viewport()
        min_scale = max(0.01, self.fit_scale * 0.25)
        max_scale = 16.0
        target = min(max(self.view_scale * factor, min_scale), max_scale)
        self._set_zoom_around(target, event.position().x(), event.position().y())
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if self.has_image:
            self.fit_to_window()
        else:
            self.open_folder_requested.emit()
        event.accept()

    def mousePressEvent(self, event) -> None:
        if self.has_image and event.button() == Qt.MouseButton.LeftButton:
            self.dragging = True
            self.last_drag_pos = event.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if not self.has_image and event.button() == Qt.MouseButton.LeftButton:
            self.open_folder_requested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.has_image and self.dragging and self.last_drag_pos is not None:
            delta = event.position() - self.last_drag_pos
            self.last_drag_pos = event.position()
            self.center_x -= delta.x() / max(self.view_scale, 0.0001)
            self.center_y -= delta.y() / max(self.view_scale, 0.0001)
            self.auto_fit = False
            self.render_current_view()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.dragging:
            self.dragging = False
            self.last_drag_pos = None
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor if self.has_image else Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.has_image and self.auto_fit:
            self.fit_to_window()
        elif self.has_image:
            self.fit_scale = self._fit_scale_for_viewport()
            self.render_current_view()
        elif not self.has_image:
            self.scene_obj.setSceneRect(0, 0, max(self.width(), 800), max(self.height(), 500))
            self._center_message()

    def _center_message(self) -> None:
        if not self.message_item:
            return
        rect = self.sceneRect()
        item_rect = self.message_item.boundingRect()
        self.message_item.setPos(
            rect.center().x() - item_rect.width() / 2,
            rect.center().y() - item_rect.height() / 2,
        )


class PhotoCullerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1280, 820)
        self.setMinimumSize(860, 560)

        self.library = PhotoLibrary()
        self.pool = QThreadPool.globalInstance()
        self.signals = WorkerSignals()
        self.signals.main_loaded.connect(self.on_main_loaded)
        self.signals.thumb_loaded.connect(self.on_thumb_loaded)
        self.signals.pick_copy_finished.connect(self.on_pick_copy_finished)

        self.main_token = 0
        self.main_cache: OrderedDict[str, QImage] = OrderedDict()
        self.main_cache_bytes = 0
        self.pending_main_prefetch: set[str] = set()
        self.thumb_images: dict[str, QImage] = {}
        self.pending_thumbs: set[str] = set()
        self.pending_pick_names: set[str] = set()
        self.path_to_row: dict[str, int] = {}
        self.end_marker = False
        self.fullscreen = False
        self.hide_overlay_timer = QTimer(self)
        self.hide_overlay_timer.setSingleShot(True)
        self.hide_overlay_timer.timeout.connect(self.hide_fullscreen_overlay)

        self._build_ui()
        self._bind_shortcuts()
        self.view.set_message("打开照片文件夹", "点击这里，或使用左上角按钮")

    def _build_ui(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #050505; }
            QFrame#Toolbar, QFrame#ThumbPanel, QFrame#FullscreenOverlay { background: #14171a; }
            QLabel { color: #e8edf3; }
            QToolButton, QPushButton {
                background: #24282e;
                color: #f3f5f7;
                border: 1px solid #343a42;
                border-radius: 4px;
                padding: 4px 8px;
            }
            QToolButton:hover, QPushButton:hover { background: #303640; }
            QLineEdit {
                background: #090a0c;
                color: #f2f5f8;
                border: 1px solid #343a42;
                border-radius: 4px;
                padding: 3px 5px;
            }
            QListWidget {
                background: #101214;
                color: #cfd6df;
                border: none;
                outline: none;
            }
            QListWidget::item { padding: 2px; }
            QListWidget::item:selected {
                background: #4a3717;
                color: #ffffff;
                border: 1px solid #d7992a;
            }
            """
        )

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.toolbar = QFrame(self)
        self.toolbar.setObjectName("Toolbar")
        self.toolbar.setFixedHeight(38)
        tools = QHBoxLayout(self.toolbar)
        tools.setContentsMargins(6, 4, 6, 4)
        tools.setSpacing(5)

        self.open_button = self._tool_button("打开", self.open_folder)
        self.prev_button = self._tool_button("<", lambda: self.navigate(-1))
        self.next_button = self._tool_button(">", lambda: self.navigate(1))
        self.rotate_left_button = self._tool_button("左转", lambda: self.rotate(-90))
        self.rotate_right_button = self._tool_button("右转", lambda: self.rotate(90))
        self.pick_button = self._tool_button("Pick", self.pick_current)
        self.delete_button = self._tool_button("删除", self.delete_current)
        self.undo_button = self._tool_button("撤销", self.undo)
        self.fullscreen_button = self._tool_button("全屏", self.enter_fullscreen)

        for button in (
            self.open_button,
            self.prev_button,
            self.next_button,
            self.rotate_left_button,
            self.rotate_right_button,
            self.pick_button,
            self.delete_button,
            self.undo_button,
            self.fullscreen_button,
        ):
            tools.addWidget(button)

        self.file_label = QLabel("请选择照片文件夹")
        self.file_label.setMinimumWidth(120)
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        tools.addWidget(self.file_label, 1)

        self.index_label = QLabel("0 / 0")
        self.index_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.index_label.setMinimumWidth(76)
        tools.addWidget(self.index_label)

        self.jump_edit = QLineEdit()
        self.jump_edit.setPlaceholderText("序号")
        self.jump_edit.setFixedWidth(54)
        self.jump_edit.returnPressed.connect(self.jump_to_entry)
        tools.addWidget(self.jump_edit)
        tools.addWidget(self._tool_button("跳转", self.jump_to_entry))

        layout.addWidget(self.toolbar)

        self.view = ImageView(self)
        self.view.open_folder_requested.connect(self.open_folder)
        self.view.viewport().installEventFilter(self)
        layout.addWidget(self.view, 1)

        self.thumb_panel = QFrame(self)
        self.thumb_panel.setObjectName("ThumbPanel")
        self.thumb_panel.setFixedHeight(104)
        thumb_layout = QHBoxLayout(self.thumb_panel)
        thumb_layout.setContentsMargins(6, 5, 6, 5)
        thumb_layout.setSpacing(6)

        thumb_layout.addWidget(self._tool_button("<", lambda: self.scroll_thumbnails(-1)))
        self.thumb_list = QListWidget()
        self.thumb_list.setViewMode(QListView.ViewMode.IconMode)
        self.thumb_list.setFlow(QListView.Flow.LeftToRight)
        self.thumb_list.setWrapping(False)
        self.thumb_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.thumb_list.setMovement(QListView.Movement.Static)
        self.thumb_list.setUniformItemSizes(True)
        self.thumb_list.setLayoutMode(QListView.LayoutMode.Batched)
        self.thumb_list.setBatchSize(64)
        self.thumb_list.setIconSize(QSize(*THUMB_ICON_SIZE))
        self.thumb_list.setGridSize(QSize(*THUMB_GRID_SIZE))
        self.thumb_list.setHorizontalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.thumb_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.thumb_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.thumb_list.itemClicked.connect(self.on_thumbnail_clicked)
        self.thumb_list.horizontalScrollBar().valueChanged.connect(self.schedule_visible_thumbnails)
        self.thumb_list.viewport().installEventFilter(self)
        thumb_layout.addWidget(self.thumb_list, 1)
        thumb_layout.addWidget(self._tool_button(">", lambda: self.scroll_thumbnails(1)))
        layout.addWidget(self.thumb_panel)
        self.thumb_panel.hide()

        self.fullscreen_overlay = QFrame(self)
        self.fullscreen_overlay.setObjectName("FullscreenOverlay")
        overlay_layout = QHBoxLayout(self.fullscreen_overlay)
        overlay_layout.setContentsMargins(8, 4, 8, 4)
        overlay_layout.setSpacing(6)
        overlay_layout.addWidget(self._tool_button("退出全屏 Esc", self.exit_fullscreen))
        self.fullscreen_file_label = QLabel("")
        overlay_layout.addWidget(self.fullscreen_file_label, 1)
        self.fullscreen_index_label = QLabel("0 / 0")
        overlay_layout.addWidget(self.fullscreen_index_label)
        self.fullscreen_overlay.hide()

    def _tool_button(self, text: str, callback) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setAutoRaise(False)
        button.clicked.connect(callback)
        return button

    def _bind_shortcuts(self) -> None:
        bindings = [
            ("Left", lambda: self.navigate(-1)),
            ("Right", lambda: self.navigate(1)),
            ("Delete", self.delete_current),
            ("Return", self.pick_current),
            ("Ctrl+Z", self.undo),
            ("F11", self.toggle_fullscreen),
            ("F", self.toggle_fullscreen),
            ("Esc", self.exit_fullscreen),
            ("0", self.view.fit_to_window),
            ("Q", lambda: self.rotate(-90)),
            ("E", lambda: self.rotate(90)),
            ("PgUp", lambda: self.scroll_thumbnails(-1)),
            ("PgDown", lambda: self.scroll_thumbnails(1)),
        ]
        for key, callback in bindings:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(callback)

    def eventFilter(self, obj, event) -> bool:
        if hasattr(self, "thumb_list") and obj is self.thumb_list.viewport() and event.type() == QEvent.Type.Wheel:
            delta = event.angleDelta().y() or event.angleDelta().x()
            step = -1 if delta > 0 else 1
            self.scroll_thumbnails(step)
            return True

        if self.fullscreen and event.type() == QEvent.Type.MouseMove:
            point = self.mapFromGlobal(event.globalPosition().toPoint())
            if point.y() <= 3:
                self.show_fullscreen_overlay()
            elif point.y() > 44 and self.fullscreen_overlay.isVisible() and not self.hide_overlay_timer.isActive():
                self.hide_overlay_timer.start(800)
        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.fullscreen_overlay.setGeometry(0, 0, self.width(), 34)

    def closeEvent(self, event) -> None:
        self.clear_main_cache()
        self.thumb_images.clear()
        self.pool.waitForDone(1500)
        super().closeEvent(event)

    def set_status(self, text: str) -> None:
        self.statusBar().showMessage(text, 3500)

    def open_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择照片文件夹")
        if folder:
            self.load_folder(folder)

    def load_folder(self, folder: str | Path) -> None:
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            self.library.load(folder)
        except Exception as exc:
            QMessageBox.critical(self, "打开失败", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()

        self.thumb_images.clear()
        self.pending_thumbs.clear()
        self.clear_main_cache()
        self.end_marker = False
        self.populate_thumbnails()
        self.show_current(reset=True)
        self.set_status(f"已打开：{folder}")

    def populate_thumbnails(self) -> None:
        self.thumb_list.setUpdatesEnabled(False)
        self.thumb_list.clear()
        self.path_to_row.clear()
        for row, item in enumerate(self.library.items):
            path = str(item.jpg)
            list_item = QListWidgetItem(self.icon_for_item(item), item.jpg.name)
            list_item.setData(Qt.ItemDataRole.UserRole, path)
            list_item.setToolTip(path)
            self.thumb_list.addItem(list_item)
            self.path_to_row[path] = row
        self.thumb_list.setUpdatesEnabled(True)
        self.thumb_panel.setVisible(bool(self.library.items) and not self.fullscreen)
        self.schedule_visible_thumbnails()

    def rebuild_path_to_row(self) -> None:
        self.path_to_row = {str(item.jpg): index for index, item in enumerate(self.library.items)}

    def icon_for_item(self, item: PhotoItem):
        image = self.thumb_images.get(str(item.jpg))
        return self.compose_thumb_icon(image, item.picked)

    def compose_thumb_icon(self, image: QImage | None, picked: bool):
        pixmap = QPixmap(*THUMB_ICON_SIZE)
        pixmap.fill(QColor("#0b0d10"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setPen(QPen(QColor("#2d333b"), 1))
        painter.drawRect(0, 0, THUMB_ICON_SIZE[0] - 1, THUMB_ICON_SIZE[1] - 1)

        if image and not image.isNull():
            scaled = image.scaled(
                QSize(THUMB_ICON_SIZE[0] - 8, THUMB_ICON_SIZE[1] - 8),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (THUMB_ICON_SIZE[0] - scaled.width()) // 2
            y = (THUMB_ICON_SIZE[1] - scaled.height()) // 2
            painter.drawImage(x, y, scaled)
        else:
            painter.setPen(QColor("#53606d"))
            painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "...")

        if picked:
            painter.setBrush(QBrush(QColor("#05c46b")))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(THUMB_ICON_SIZE[0] - 24, 6, 17, 17)
            painter.setPen(QPen(QColor("#06130c"), 2))
            painter.drawLine(THUMB_ICON_SIZE[0] - 20, 15, THUMB_ICON_SIZE[0] - 16, 19)
            painter.drawLine(THUMB_ICON_SIZE[0] - 16, 19, THUMB_ICON_SIZE[0] - 10, 10)
        painter.end()
        return QIcon(pixmap)

    def update_row_icon(self, row: int) -> None:
        if 0 <= row < len(self.library.items):
            item = self.library.items[row]
            widget_item = self.thumb_list.item(row)
            if widget_item:
                widget_item.setIcon(self.icon_for_item(item))

    def schedule_visible_thumbnails(self) -> None:
        QTimer.singleShot(0, self.request_visible_thumbnails)

    def request_visible_thumbnails(self) -> None:
        count = len(self.library.items)
        if count == 0:
            return
        grid_width = max(self.thumb_list.gridSize().width(), THUMB_GRID_SIZE[0])
        left = self.thumb_list.horizontalScrollBar().value()
        width = max(self.thumb_list.viewport().width(), grid_width)
        start = max(0, left // grid_width - THUMB_VISIBLE_PADDING)
        end = min(count - 1, (left + width) // grid_width + THUMB_VISIBLE_PADDING)

        current = self.library.current_index
        if 0 <= current < count:
            start = min(start, max(0, current - 2))
            end = max(end, min(count - 1, current + 2))

        for row in range(start, end + 1):
            item = self.library.items[row]
            path = str(item.jpg)
            if path in self.thumb_images or path in self.pending_thumbs:
                continue
            self.pending_thumbs.add(path)
            self.pool.start(
                ImageLoadTask(
                    self.signals,
                    path,
                    rotation=item.rotation,
                    max_dimension=max(THUMB_ICON_SIZE) * 2,
                    thumbnail=True,
                )
            )

    def on_thumb_loaded(self, path: str, image: QImage, error: str) -> None:
        self.pending_thumbs.discard(path)
        if error or image.isNull():
            return
        self.thumb_images[path] = image
        row = self.path_to_row.get(path)
        if row is not None:
            self.update_row_icon(row)

    def clear_main_cache(self) -> None:
        self.main_cache.clear()
        self.main_cache_bytes = 0
        self.pending_main_prefetch.clear()

    def cached_image_bytes(self, image: QImage) -> int:
        try:
            return int(image.sizeInBytes())
        except Exception:
            return image.width() * image.height() * 4

    def store_prefetched_main(self, path: str, image: QImage) -> None:
        if image.isNull() or path not in self.path_to_row:
            return
        if path in self.main_cache:
            self.main_cache_bytes -= self.cached_image_bytes(self.main_cache.pop(path))
        self.main_cache[path] = image
        self.main_cache_bytes += self.cached_image_bytes(image)
        while self.main_cache_bytes > MAIN_CACHE_MAX_BYTES and self.main_cache:
            _old_path, old_image = self.main_cache.popitem(last=False)
            self.main_cache_bytes -= self.cached_image_bytes(old_image)

    def schedule_main_prefetch(self) -> None:
        if not self.library.items or self.end_marker:
            return
        start = self.library.current_index + 1
        stop = min(len(self.library.items), start + MAIN_PREFETCH_AHEAD)
        for row in range(start, stop):
            path = str(self.library.items[row].jpg)
            if path in self.main_cache or path in self.pending_main_prefetch:
                continue
            self.pending_main_prefetch.add(path)
            self.pool.start(
                ImageLoadTask(
                    self.signals,
                    path,
                    token=PREFETCH_TOKEN,
                    rotation=self.library.items[row].rotation,
                    max_dimension=MAIN_MAX_DIMENSION,
                    thumbnail=False,
                )
            )

    def on_prefetch_loaded(self, path: str, image: QImage, error: str) -> None:
        self.pending_main_prefetch.discard(path)
        if error or image.isNull():
            return
        self.store_prefetched_main(path, image)

    def ensure_current_thumbnail_visible(self) -> None:
        index = self.library.current_index
        if 0 <= index < self.thumb_list.count():
            self.thumb_list.setCurrentRow(index)
            grid_width = max(self.thumb_list.gridSize().width(), THUMB_GRID_SIZE[0])
            viewport_width = max(self.thumb_list.viewport().width(), grid_width)
            target = index * grid_width - max(0, (viewport_width - grid_width) // 2)
            bar = self.thumb_list.horizontalScrollBar()
            bar.setValue(max(bar.minimum(), min(bar.maximum(), target)))
            self.schedule_visible_thumbnails()

    def on_thumbnail_clicked(self, item: QListWidgetItem) -> None:
        self.jump_to(self.thumb_list.row(item))

    def scroll_thumbnails(self, direction: int) -> None:
        bar = self.thumb_list.horizontalScrollBar()
        page = max(self.thumb_list.viewport().width() - THUMB_GRID_SIZE[0], THUMB_GRID_SIZE[0])
        bar.setValue(bar.value() + direction * page)
        self.schedule_visible_thumbnails()

    def show_current(self, reset: bool = False) -> None:
        self.end_marker = False
        item = self.library.current()
        if item is None:
            self.main_token += 1
            self.view.set_message("没有 JPG/JPEG 照片", "请选择包含 JPG/JPEG 的文件夹")
            self.file_label.setText("请选择照片文件夹")
            self.fullscreen_file_label.setText("")
            self.index_label.setText("0 / 0")
            self.fullscreen_index_label.setText("0 / 0")
            self.thumb_panel.hide()
            return

        self.thumb_panel.setVisible(not self.fullscreen)
        raw_label = " + NEF" if item.raw else ""
        pick_label = "  已 Pick" if item.picked else ""
        file_text = f"{item.jpg.name}{raw_label}{pick_label}"
        index_text = f"{self.library.current_index + 1} / {len(self.library.items)}"
        self.file_label.setText(file_text)
        self.fullscreen_file_label.setText(file_text)
        self.index_label.setText(index_text)
        self.fullscreen_index_label.setText(index_text)
        self.pick_button.setText("取消 Pick" if item.picked else "Pick")
        self.library.save_state()
        self.ensure_current_thumbnail_visible()

        self.main_token += 1
        token = self.main_token
        path = str(item.jpg)
        cached = self.main_cache.pop(path, None)
        if cached is not None:
            self.main_cache_bytes -= self.cached_image_bytes(cached)
            self.view.set_image(cached)
            self.schedule_main_prefetch()
            return

        self.view.set_message("加载中", item.jpg.name)
        self.pool.start(
            ImageLoadTask(
                self.signals,
                path,
                token=token,
                rotation=item.rotation,
                max_dimension=MAIN_MAX_DIMENSION,
                thumbnail=False,
            )
        )

    def on_main_loaded(self, token: int, path: str, image: QImage, error: str) -> None:
        if token == PREFETCH_TOKEN:
            self.on_prefetch_loaded(path, image, error)
            return
        if token != self.main_token:
            return
        if error or image.isNull():
            self.view.set_message("无法打开图片", Path(path).name)
            self.set_status(error or "无法打开图片")
            return
        self.view.set_image(image)
        self.schedule_main_prefetch()

    def show_end_marker(self) -> None:
        if not self.library.items:
            self.show_current(reset=True)
            return
        self.end_marker = True
        self.main_token += 1
        total = len(self.library.items)
        self.thumb_panel.setVisible(not self.fullscreen)
        self.file_label.setText("结束标记")
        self.fullscreen_file_label.setText("结束标记")
        self.index_label.setText(f"{total} / {total} · 结束")
        self.fullscreen_index_label.setText(f"{total} / {total} · 结束")
        self.pick_button.setText("Pick")
        self.view.set_message("已到最后一张", "本文件夹筛选结束；按 ← 返回最后一张，或输入序号跳转")
        self.ensure_current_thumbnail_visible()
        self.set_status("已到最后一张")

    def navigate(self, delta: int) -> None:
        if not self.library.items:
            return
        if self.end_marker:
            if delta < 0:
                self.show_current(reset=True)
            else:
                self.set_status("已到结束标记")
            return
        if delta > 0 and self.library.current_index >= len(self.library.items) - 1:
            self.show_end_marker()
            return
        self.library.jump(self.library.current_index + delta)
        self.show_current(reset=True)

    def jump_to(self, index: int) -> None:
        if not self.library.items:
            return
        self.library.jump(index)
        self.show_current(reset=True)

    def jump_to_entry(self) -> None:
        raw = self.jump_edit.text().strip()
        if not raw:
            return
        try:
            target = int(raw)
        except ValueError:
            self.set_status("跳转序号需要输入数字")
            return
        self.jump_to(target - 1)

    def rotate(self, degrees: int) -> None:
        if self.end_marker:
            self.set_status("结束标记不能旋转，按 ← 返回最后一张")
            return
        item = self.library.current()
        if item is None:
            return
        path = str(item.jpg)
        item.rotation = (item.rotation + degrees) % 360
        self.thumb_images.pop(path, None)
        cached = self.main_cache.pop(path, None)
        if cached is not None:
            self.main_cache_bytes -= self.cached_image_bytes(cached)
        self.update_row_icon(self.library.current_index)
        self.pending_thumbs.discard(path)
        self.show_current(reset=True)
        self.request_visible_thumbnails()
        self.set_status("已旋转查看方向，不修改原图文件")

    def pick_current(self) -> None:
        if self.end_marker:
            self.set_status("已到结束标记，按 ← 返回最后一张")
            return
        item = self.library.current()
        if item is None:
            return
        affected_index = self.library.current_index
        affected_name = item.jpg.name.lower()

        if item.picked:
            self.set_status(self.library.unpick_current())
            self.update_row_icon(affected_index)
            self.show_current(reset=False)
            return

        message, action = self.library.prepare_pick_current()
        self.set_status(message)
        self.update_row_icon(affected_index)
        if action:
            self.pending_pick_names.add(affected_name)
            self.pool.start(FileCopyTask(self.signals, action))
            if affected_index >= len(self.library.items) - 1:
                self.show_end_marker()
            else:
                self.show_current(reset=True)
        else:
            self.show_current(reset=True)

    def on_pick_copy_finished(self, action: dict, ok: bool, error: str) -> None:
        name = str(action.get("name", "")).lower()
        self.pending_pick_names.discard(name)

        if name not in self.library.picked_names:
            for _source, destination in action.get("copies", []):
                self.library.safe_unlink_pick_copy(destination)
            return

        if not ok:
            for _source, destination in action.get("copies", []):
                self.library.safe_unlink_pick_copy(destination)
            index = self.library.index_for_name(name)
            self.library.picked_names.discard(name)
            self.library.pick_copies.pop(name, None)
            if 0 <= index < len(self.library.items):
                self.library.items[index].picked = False
                self.update_row_icon(index)
            self.library.save_state()
            self.set_status(f"Pick 复制失败：{error}")
            if self.library.current() and self.library.current().jpg.name.lower() == name:
                self.show_current(reset=False)
            return

        self.set_status("Pick 复制完成：JPG + NEF 已进入 PICK")

    def delete_current(self, checked: bool = False, retry_count: int = 5) -> None:
        if self.end_marker:
            self.set_status("已到结束标记，按 ← 返回最后一张")
            return
        if self.library.current() is None:
            return
        affected_index = self.library.current_index
        affected_path = str(self.library.current().jpg)
        try:
            message = self.library.delete_current()
        except PermissionError as exc:
            if retry_count > 0:
                self.set_status("当前图片正在释放，稍后自动重试删除")
                QTimer.singleShot(160, lambda: self.delete_current(False, retry_count - 1))
            else:
                self.set_status(f"删除失败：{exc}")
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) == 32 and retry_count > 0:
                self.set_status("当前图片正在释放，稍后自动重试删除")
                QTimer.singleShot(160, lambda: self.delete_current(False, retry_count - 1))
            else:
                self.set_status(f"删除失败：{exc}")
            return
        self.set_status(message)
        self.thumb_images.pop(affected_path, None)
        cached = self.main_cache.pop(affected_path, None)
        if cached is not None:
            self.main_cache_bytes -= self.cached_image_bytes(cached)
        self.pending_thumbs.discard(affected_path)
        taken = self.thumb_list.takeItem(affected_index)
        del taken
        self.rebuild_path_to_row()
        self.thumb_list.doItemsLayout()
        deleted_was_last = affected_index >= len(self.library.items)
        if self.library.items and deleted_was_last:
            self.show_end_marker()
        else:
            self.show_current(reset=True)
        QTimer.singleShot(0, self.ensure_current_thumbnail_visible)
        QTimer.singleShot(80, self.ensure_current_thumbnail_visible)

    def undo(self) -> None:
        action = self.library.last_action
        action_type = action.get("type") if action else None
        self.set_status(self.library.undo())
        if action_type == "pick":
            self.update_row_icon(self.library.current_index)
        else:
            self.populate_thumbnails()
        self.show_current(reset=True)

    def toggle_fullscreen(self) -> None:
        if self.fullscreen:
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self) -> None:
        if self.fullscreen:
            return
        self.fullscreen = True
        self.toolbar.hide()
        self.thumb_panel.hide()
        self.statusBar().hide()
        self.showFullScreen()
        self.hide_fullscreen_overlay()
        QTimer.singleShot(80, self.view.fit_to_window)

    def exit_fullscreen(self) -> None:
        if not self.fullscreen:
            return
        self.fullscreen = False
        self.showNormal()
        self.toolbar.show()
        if self.library.items:
            self.thumb_panel.show()
        self.statusBar().show()
        self.hide_fullscreen_overlay()
        QTimer.singleShot(80, self.view.fit_to_window)

    def show_fullscreen_overlay(self) -> None:
        if not self.fullscreen:
            return
        self.hide_overlay_timer.stop()
        self.fullscreen_overlay.setGeometry(0, 0, self.width(), 34)
        self.fullscreen_overlay.show()
        self.fullscreen_overlay.raise_()

    def hide_fullscreen_overlay(self) -> None:
        self.hide_overlay_timer.stop()
        self.fullscreen_overlay.hide()

    def save_screenshot(self, output: str | Path) -> None:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.grab().save(str(path))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local JPG/NEF photo culling viewer")
    parser.add_argument("folder", nargs="?", help="启动时打开的照片文件夹")
    parser.add_argument("--folder", dest="folder_option", help="启动时打开的照片文件夹")
    parser.add_argument("--screenshot", help="自动保存窗口截图")
    parser.add_argument("--screenshot-delay-ms", type=int, default=1700, help="自动截图延迟，用于验收测试")
    parser.add_argument("--close-after-ms", type=int, default=0, help="自动关闭窗口，用于验收测试")
    parser.add_argument("--start-fullscreen", action="store_true", help="启动后进入全屏")
    parser.add_argument("--rotate", choices=["left", "right"], help="启动后旋转当前照片，用于验收测试")
    parser.add_argument("--test-action", choices=["pick", "delete"], help="启动后执行一次操作，用于验收测试")
    parser.add_argument("--test-zoom-steps", type=int, default=0, help="启动后模拟缩放，用于验收测试")
    parser.add_argument("--test-jump-index", type=int, default=0, help="启动后跳转到指定序号，用于验收测试")
    parser.add_argument("--test-show-fullscreen-overlay", action="store_true", help="全屏后显示顶部浮层，用于验收测试")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_TITLE)
    window = PhotoCullerWindow()
    window.show()

    folder = args.folder_option or args.folder
    if folder and Path(folder).exists():
        window.load_folder(folder)

    def startup_actions() -> None:
        if args.test_jump_index > 0:
            window.jump_to(args.test_jump_index - 1)
        if args.rotate == "left":
            window.rotate(-90)
        elif args.rotate == "right":
            window.rotate(90)
        if args.test_action == "pick":
            window.pick_current()
        elif args.test_action == "delete":
            window.delete_current()
        if args.start_fullscreen:
            window.enter_fullscreen()
        if args.test_show_fullscreen_overlay:
            QTimer.singleShot(500, window.show_fullscreen_overlay)
        if args.test_zoom_steps and window.view.has_image:
            for _ in range(abs(args.test_zoom_steps)):
                factor = 1.15 if args.test_zoom_steps > 0 else 1 / 1.15
                window.view.auto_fit = False
                window.view.scale(factor, factor)

    QTimer.singleShot(300, startup_actions)
    if args.screenshot:
        QTimer.singleShot(max(0, args.screenshot_delay_ms), lambda: window.save_screenshot(args.screenshot))
    if args.close_after_ms > 0:
        QTimer.singleShot(args.close_after_ms, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
