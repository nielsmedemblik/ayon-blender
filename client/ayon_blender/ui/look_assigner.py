"""Qt window that lets artists browse and apply looks to loaded assets."""

from __future__ import annotations

import importlib
import inspect
import traceback
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Union

import bpy
from qtpy import QtCore, QtWidgets

from ayon_api import (
    get_products,
    get_representations,
    get_versions,
)
from ayon_core.pipeline import get_current_project_name, remove_container
from ayon_core.pipeline.load import get_representation_path_from_context

from ayon_blender.api import pipeline
from ayon_blender.api.constants import AYON_PROPERTY
from ayon_blender.plugins.load.load_look import BlendLookLoader

try:
    from ayon_core.style import load_stylesheet as ayon_load_stylesheet
    from ayon_core.resources import get_ayon_icon_filepath
except ImportError:
    ayon_load_stylesheet = None
    get_ayon_icon_filepath = None


LOOK_ASSIGNER_KEY = "lookAssigner"


def _iter_collection_objects(collection: bpy.types.Collection):
    stack = [collection]
    seen: set[bpy.types.Collection] = set()
    while stack:
        coll = stack.pop()
        if coll in seen:
            continue
        seen.add(coll)
        for child in coll.children:
            stack.append(child)
        for obj in coll.objects:
            yield obj


def _iter_object_hierarchy(obj: bpy.types.Object):
    yield obj
    for child in obj.children_recursive:
        yield child


def _collect_mesh_members(node: bpy.types.ID) -> List[bpy.types.Object]:
    meshes: List[bpy.types.Object] = []
    seen: set[bpy.types.Object] = set()

    def _add(obj: bpy.types.Object):
        if obj in seen:
            return
        seen.add(obj)
        if obj.type == 'MESH':
            meshes.append(obj)

    if isinstance(node, bpy.types.Object):
        for child in _iter_object_hierarchy(node):
            _add(child)
    elif isinstance(node, bpy.types.Collection):
        for child in _iter_collection_objects(node):
            if isinstance(child, bpy.types.Object):
                for descendant in _iter_object_hierarchy(child):
                    _add(descendant)
    return meshes


@dataclass
class SceneAsset:
    """Description of a loaded asset in the scene."""

    display_name: str
    container: Dict
    node: bpy.types.ID
    representation: Dict
    folder: Dict
    product: Dict
    folder_id: Optional[str]
    product_id: Optional[str]

    def mesh_members(self) -> List[bpy.types.Object]:
        return _collect_mesh_members(self.node)

    @property
    def namespace(self) -> str:
        return self.container.get("namespace") or ""

    @property
    def folder_name(self) -> str:
        return self.folder.get("name") or self.folder.get("path", "") or (
            self.folder_id or ""
        )

    @property
    def product_name(self) -> str:
        return (
            self.product.get("name")
            or self.container.get("name", "")
            or (self.product_id or "")
        )


@dataclass
class LookEntry:
    """Represents a single look version that can be assigned."""

    product: Dict
    version: Dict
    manifest_representation: Dict
    library_representation: Dict

    def label(self) -> str:
        product_name = self.product.get("name") or "Look"
        variant = (self.product.get("attrib") or {}).get("variant")
        version_label = self.version.get("name") or ""
        version_number = self.version.get("version")
        parts = [product_name]
        if variant:
            parts.append(f"({variant})")
        if version_label:
            parts.append(version_label)
        if version_number is not None:
            parts.append(f"v{int(version_number):03d}")
        return " ".join(filter(None, parts))

    def build_context(self, project_name: str) -> Dict:
        representation_context = self.manifest_representation.get("context") or {}
        folder_ctx = representation_context.get("folder") or {}
        product_ctx = representation_context.get("product") or self.product
        return {
            "project": {"name": project_name},
            "folder": folder_ctx,
            "product": product_ctx,
            "version": self.version,
            "representation": self.manifest_representation,
        }


class LookAssignerController:
    """Handles discovery of assets and looks and applies selections."""

    def __init__(self):
        self.project_name = get_current_project_name()
        self._assets: List[SceneAsset] = []
        self._look_cache: Dict[str, List[LookEntry]] = {}
        self._loader = BlendLookLoader()

    @property
    def assets(self) -> Sequence[SceneAsset]:
        return self._assets

    def refresh_assets(self) -> Sequence[SceneAsset]:
        self._look_cache.clear()
        containers = [
            container
            for container in pipeline.ls()
            if container.get("loader") != BlendLookLoader.__name__
        ]
        rep_ids = {
            container.get("representation")
            for container in containers
            if container.get("representation")
        }
        rep_map: Dict[str, Dict] = {}
        if rep_ids:
            rep_fields = {
                "id",
                "name",
                "context",
                "versionId",
                "attrib",
                "data",
            }
            representations = list(
                get_representations(
                    self.project_name,
                    representation_ids=rep_ids,
                    fields=rep_fields,
                )
            )
            rep_map = {rep["id"]: rep for rep in representations}

        version_ids = {
            rep.get("versionId")
            for rep in rep_map.values()
            if rep.get("versionId")
        }
        version_map: Dict[str, Dict] = {}
        if version_ids:
            version_fields = {
                "id",
                "name",
                "productId",
                "version",
                "attrib",
                "data",
            }
            versions = list(
                get_versions(
                    self.project_name,
                    version_ids=version_ids,
                    fields=version_fields,
                )
            )
            version_map = {version["id"]: version for version in versions}

        product_ids = {
            version.get("productId")
            for version in version_map.values()
            if version.get("productId")
        }
        product_map: Dict[str, Dict] = {}
        if product_ids:
            product_fields = {
                "id",
                "name",
                "folderId",
                "productType",
                "attrib",
            }
            products = list(
                get_products(
                    self.project_name,
                    product_ids=product_ids,
                    fields=product_fields,
                )
            )
            product_map = {product["id"]: product for product in products}

        assets: List[SceneAsset] = []
        for container in containers:
            rep_entity = rep_map.get(container.get("representation"))
            if not rep_entity:
                continue
            version_entity = version_map.get(rep_entity.get("versionId", ""))
            product_entity = (
                product_map.get(version_entity.get("productId"))
                if version_entity
                else None
            )
            context = rep_entity.get("context") or {}
            folder_ctx = dict(context.get("folder") or {})
            product_ctx = dict(context.get("product") or {})
            folder_id = folder_ctx.get("id")
            product_id = (
                version_entity.get("productId") if version_entity else None
            )
            if not folder_id and product_entity:
                folder_id = product_entity.get("folderId")
            if folder_id and "id" not in folder_ctx:
                folder_ctx["id"] = folder_id
            if product_entity:
                product_ctx.setdefault("name", product_entity.get("name"))
                product_ctx.setdefault("attrib", product_entity.get("attrib"))
            if product_id and "id" not in product_ctx:
                product_ctx["id"] = product_id

            node = container.get("node")
            if not isinstance(node, (bpy.types.Object, bpy.types.Collection)):
                continue
            display_name = container.get("objectName") or container.get("name")
            if not display_name:
                display_name = node.name
            assets.append(
                SceneAsset(
                    display_name=display_name,
                    container=container,
                    node=node,
                    representation=rep_entity,
                    folder=folder_ctx,
                    product=product_ctx,
                    folder_id=folder_id,
                    product_id=product_id,
                )
            )
        self._assets = assets
        return assets

    def get_looks_for_asset(self, asset: SceneAsset) -> Sequence[LookEntry]:
        folder_id = asset.folder_id
        if not folder_id:
            return []
        if folder_id in self._look_cache:
            return self._look_cache[folder_id]
        products = list(
            get_products(
                self.project_name,
                folder_ids={folder_id},
                product_types={"look"},
                fields={
                    "id",
                    "name",
                    "folderId",
                    "productType",
                    "attrib",
                },
            )
        )
        look_entries: List[LookEntry] = []
        for product in products:
            versions = list(
                get_versions(
                    self.project_name,
                    product_ids={product["id"]},
                    hero=True,
                    standard=True,
                    fields={
                        "id",
                        "name",
                        "productId",
                        "version",
                        "attrib",
                        "data",
                    },
                )
            )
            if not versions:
                continue
            version_ids = {version["id"] for version in versions}
            repres = list(
                get_representations(
                    self.project_name,
                    version_ids=version_ids,
                    representation_names={"json", "blend", "materials"},
                    fields={
                        "id",
                        "name",
                        "files",
                        "context",
                        "versionId",
                        "attrib",
                        "data",
                    },
                )
            )
            repres_by_version: Dict[str, Dict[str, Dict]] = {}
            for repres_entity in repres:
                repres_by_version.setdefault(
                    repres_entity["versionId"], {}
                )[repres_entity["name"]] = repres_entity
            for version in versions:
                repr_pair = repres_by_version.get(version["id"])
                if not repr_pair:
                    continue
                manifest = repr_pair.get("json")
                library = (
                    repr_pair.get("blend")
                    or repr_pair.get("materials")
                )
                if not manifest or not library:
                    continue
                look_entries.append(
                    LookEntry(
                        product=product,
                        version=version,
                        manifest_representation=manifest,
                        library_representation=library,
                    )
                )
        look_entries.sort(
            key=lambda item: (
                (item.product.get("name") or "").lower(),
                -(item.version.get("version") or 0),
            )
        )
        self._look_cache[folder_id] = look_entries
        return look_entries

    def apply_look(self, asset: SceneAsset, look: LookEntry) -> Dict:
        meshes = asset.mesh_members()
        if not meshes:
            raise RuntimeError(
                f"No mesh objects found for asset '{asset.display_name}'."
            )
        context = look.build_context(self.project_name)
        libpath = get_representation_path_from_context(context)
        if hasattr(libpath, "normalized"):
            libpath = libpath.normalized()
        container_name = f"{asset.display_name}_look"
        imported_materials, _ = self._loader._process(
            str(libpath),
            container_name,
            meshes,
            context,
        )
        self._store_current_look(asset, look, imported_materials)
        return {
            "materials": [mat.name for mat in imported_materials if mat],
            "asset": asset.display_name,
            "look": look.label(),
        }

    def _store_current_look(
        self,
        asset: SceneAsset,
        look: LookEntry,
        imported_materials: Sequence[bpy.types.Material],
    ):
        node = asset.node
        if not hasattr(node, "keys"):
            return
        data = node.get(AYON_PROPERTY)
        if data is None:
            node[AYON_PROPERTY] = {}
            data = node.get(AYON_PROPERTY)
        look_info = data.setdefault(LOOK_ASSIGNER_KEY, {})
        look_info.clear()
        look_info.update({
            "productId": look.product.get("id"),
            "productName": look.product.get("name"),
            "versionId": look.version.get("id"),
            "version": look.version.get("version"),
            "representationId": look.manifest_representation.get("id"),
            "materials": [
                mat.name for mat in imported_materials if mat is not None
            ],
        })

    def unassign_unused_looks(self) -> Dict[str, Union[List[str], int]]:
        removed_containers = 0
        for container in pipeline.ls():
            if container.get("loader") != BlendLookLoader.__name__:
                continue
            try:
                remove_container(container)
                removed_containers += 1
            except Exception:
                traceback.print_exc()
        cleanup = self._cleanup_unused_materials()
        cleanup["containers"] = removed_containers
        return cleanup

    @staticmethod
    def _cleanup_unused_materials() -> Dict[str, List[str]]:
        removed_materials: List[str] = []
        removed_images: List[str] = []
        for material in list(bpy.data.materials):
            if material.users == 0 and ":" in material.name:
                removed_materials.append(material.name)
                bpy.data.materials.remove(material)
        for image in list(bpy.data.images):
            if image.users == 0 and ":" in image.name:
                removed_images.append(image.name)
                bpy.data.images.remove(image)
        return {
            "materials": removed_materials,
            "images": removed_images,
        }


class LookAssignerWindow(QtWidgets.QDialog):
    """Qt window that allows artists to switch looks quickly."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AYON Look Assigner")
        self.resize(960, 540)
        self.controller = LookAssignerController()
        self._build_ui()
        _apply_ayon_style(self)
        self.refresh_assets()

    # ---- UI construction -------------------------------------------------
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        toolbar = QtWidgets.QHBoxLayout()
        self.refresh_button = QtWidgets.QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_assets)

        toolbar.addWidget(self.refresh_button)
        toolbar.addStretch(1)

        layout.addLayout(toolbar)

        splitter = QtWidgets.QSplitter()
        splitter.setOrientation(QtCore.Qt.Horizontal)

        self.asset_list = QtWidgets.QListWidget()
        self.asset_list.currentItemChanged.connect(self._on_asset_selection_changed)
        splitter.addWidget(self.asset_list)

        self.look_list = QtWidgets.QListWidget()
        self.look_list.itemDoubleClicked.connect(self.assign_selected_look)
        splitter.addWidget(self.look_list)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        layout.addWidget(splitter)

        footer = QtWidgets.QHBoxLayout()
        self.info_label = QtWidgets.QLabel("")
        self.info_label.setWordWrap(True)

        footer_buttons = QtWidgets.QHBoxLayout()
        self.assign_button = QtWidgets.QPushButton("Assign Selected Look")
        self.assign_button.clicked.connect(self.assign_selected_look)
        self.assign_button.setEnabled(False)

        self.unassign_button = QtWidgets.QPushButton("Unassign Looks")
        self.unassign_button.clicked.connect(self._unassign_looks)

        footer_buttons.addWidget(self.assign_button)
        footer_buttons.addWidget(self.unassign_button)

        footer.addLayout(footer_buttons)
        footer.addWidget(self.info_label, stretch=1)

        layout.addLayout(footer)

    # ---- Data management -------------------------------------------------
    def refresh_assets(self):
        try:
            self.controller.refresh_assets()
        except Exception as exc:
            traceback.print_exc()
            QtWidgets.QMessageBox.critical(
                self,
                "Failed to refresh assets",
                str(exc),
            )
            return
        self._populate_asset_list()

    def _populate_asset_list(self):
        self.asset_list.clear()
        for asset in self.controller.assets:
            item = QtWidgets.QListWidgetItem(self._format_asset_label(asset))
            item.setData(QtCore.Qt.UserRole, asset)
            self.asset_list.addItem(item)
        self._update_status(f"Found {self.asset_list.count()} assets.")
        self.look_list.clear()
        self.assign_button.setEnabled(False)

    def _format_asset_label(self, asset: SceneAsset) -> str:
        parts = [
            asset.display_name,
            f"[{asset.product_name}]",
        ]
        if asset.namespace:
            parts.append(f"({asset.namespace})")
        return " ".join(parts)

    def _on_asset_selection_changed(self, current, _previous):
        self.look_list.clear()
        asset = current.data(QtCore.Qt.UserRole) if current else None
        if not asset:
            self.assign_button.setEnabled(False)
            return
        try:
            looks = self.controller.get_looks_for_asset(asset)
        except Exception as exc:
            traceback.print_exc()
            QtWidgets.QMessageBox.critical(
                self,
                "Failed to query looks",
                str(exc),
            )
            return
        if not looks:
            self._update_status("No looks found for this asset.")
            self.assign_button.setEnabled(False)
            return
        for look in looks:
            item = QtWidgets.QListWidgetItem(look.label())
            item.setData(QtCore.Qt.UserRole, look)
            self.look_list.addItem(item)
        self.assign_button.setEnabled(True)
        self._update_status(f"{len(looks)} look(s) available.")

    # ---- Actions --------------------------------------------------------
    def assign_selected_look(self):
        asset_item = self.asset_list.currentItem()
        look_item = self.look_list.currentItem()
        if not asset_item or not look_item:
            return
        asset = asset_item.data(QtCore.Qt.UserRole)
        look = look_item.data(QtCore.Qt.UserRole)
        try:
            result = self.controller.apply_look(asset, look)
        except Exception as exc:
            traceback.print_exc()
            QtWidgets.QMessageBox.critical(
                self,
                "Failed to assign look",
                str(exc),
            )
            return
        self._update_status(
            f"Assigned '{result['look']}' to {result['asset']} "
            f"({len(result['materials'])} materials)."
        )

    def _unassign_looks(self):
        result = self.controller.unassign_unused_looks()
        removed_containers = result.get("containers", 0)
        removed_mats = len(result.get("materials", []))
        removed_images = len(result.get("images", []))
        self._update_status(
            f"Removed {removed_containers} look container(s), "
            f"{removed_mats} material(s) and {removed_images} image(s)."
        )

    def _update_status(self, message: str):
        self.info_label.setText(message)


_LOOK_ASSIGNER_WINDOW: Optional[LookAssignerWindow] = None


def get_look_assigner_window() -> LookAssignerWindow:
    global _LOOK_ASSIGNER_WINDOW
    if _LOOK_ASSIGNER_WINDOW is None:
        _LOOK_ASSIGNER_WINDOW = LookAssignerWindow()
    return _LOOK_ASSIGNER_WINDOW


_DEFAULT_QSS = """
QWidget {
    background-color: #2b2b2b;
    color: #f0f0f0;
    font-family: 'Segoe UI', 'Roboto', 'Helvetica Neue', sans-serif;
    font-size: 12px;
}
QLineEdit, QListWidget, QTextEdit, QPlainTextEdit, QTreeView, QTableView {
    background-color: #3a3a3a;
    border: 1px solid #4a4a4a;
    border-radius: 2px;
    padding: 4px;
}
QPushButton {
    background-color: #444;
    border: 1px solid #5a5a5a;
    padding: 6px 12px;
    border-radius: 2px;
}
QPushButton:hover {
    background-color: #555;
}
QPushButton:pressed {
    background-color: #666;
}
QSplitter::handle {
    background-color: #4a4a4a;
    width: 2px;
}
"""


def _apply_ayon_style(widget: QtWidgets.QWidget) -> bool:
    """Apply AYON's Qt styling when available."""

    if ayon_load_stylesheet:
        try:
            widget.setStyleSheet(ayon_load_stylesheet())
            return True
        except Exception:
            pass

    def _call_style(func):
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):
            signature = None

        if signature:
            params = signature.parameters
            if len(params) == 0:
                func()
                return True
            if len(params) == 1:
                func(widget)
                return True

        try:
            func(widget)
            return True
        except TypeError:
            try:
                func()
                return True
            except Exception:
                return False
        except Exception:
            return False

    def _try_from_module(module_name, attr_names, stylesheet_attrs=()):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            return False
        for attr in attr_names:
            func = getattr(module, attr, None)
            if callable(func) and _call_style(func):
                return True
        for attr in stylesheet_attrs:
            getter = getattr(module, attr, None)
            if callable(getter):
                try:
                    stylesheet = getter()
                except Exception:
                    stylesheet = None
                if stylesheet:
                    widget.setStyleSheet(stylesheet)
                    return True
        return False

    style_sources = (
        (
            "ayon_core.tools.utils.host_tools",
            ("apply_style", "apply_stylesheet", "apply_qt_style"),
            ("get_stylesheet", "get_qt_stylesheet"),
        ),
        (
            "ayon_core.style",
            ("apply_style", "apply_ayon_style", "apply_stylesheet"),
            ("get_stylesheet",),
        ),
    )

    for module_name, func_names, sheet_names in style_sources:
        if _try_from_module(module_name, func_names, sheet_names):
            return True

    app = QtWidgets.QApplication.instance()
    if app:
        app_stylesheet = app.styleSheet()
        if app_stylesheet:
            widget.setStyleSheet(app_stylesheet)
            return True

    resource_paths = (
        ":/ayon/style/style.qss",
        ":/style/style.qss",
        ":/ayon/resources/styles/dark.qss",
    )
    for path in resource_paths:
        file = QtCore.QFile(path)
        if file.exists() and file.open(QtCore.QIODevice.ReadOnly):
            data = bytes(file.readAll()).decode("utf-8")
            file.close()
            if data:
                widget.setStyleSheet(data)
                return True

    widget.setStyleSheet(_DEFAULT_QSS)
    return False
