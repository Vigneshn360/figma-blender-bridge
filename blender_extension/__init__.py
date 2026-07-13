"""Figma to Blender Bridge — Blender 5.1 extension."""

from __future__ import annotations

import json
import os
import queue
import re
import tempfile
import threading
import tomllib
import traceback
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import bpy
from bpy.props import BoolProperty, FloatProperty
from mathutils import Matrix, Vector


BRIDGE_PORT = 51982
BRIDGE_VERSION = "0.6.0"
GITHUB_REPOSITORY = "Vigneshn360/figma-blender-bridge"
GITHUB_LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
COLLECTION_NAME = "Figma Bridge"
COLLECTION_PATH_KEY = "figma_bridge_collection_path"
_SERVER: ThreadingHTTPServer | None = None
_SERVER_THREAD: threading.Thread | None = None
_INBOX: queue.Queue[dict[str, Any]] = queue.Queue()
_RESULTS: dict[str, dict[str, Any]] = {}
_LAST_STATUS = "Stopped"
_UPDATE_STATUS = "Updates not checked"
_UPDATE_RELEASE: dict[str, str] | None = None


def _set_status(message: str) -> None:
    global _LAST_STATUS
    _LAST_STATUS = message
    print(f"[Figma Bridge] {message}")


class _BridgeHandler(BaseHTTPRequestHandler):
    server_version = f"FigmaBlenderBridge/{BRIDGE_VERSION}"

    def _headers(self, status: int = 200, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._headers(204)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/result/"):
            request_id = self.path.removeprefix("/result/")
            result = _RESULTS.get(request_id, {"ok": True, "state": "pending"})
            self._headers()
            self.wfile.write(json.dumps(result).encode())
            return
        if self.path != "/status":
            self._headers(404)
            self.wfile.write(b'{"ok":false,"error":"not found"}')
            return
        self._headers()
        self.wfile.write(
            json.dumps({"ok": True, "name": "Figma to Blender Bridge", "version": BRIDGE_VERSION}).encode()
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/push":
            self._headers(404)
            self.wfile.write(b'{"ok":false,"error":"not found"}')
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 25 * 1024 * 1024:
                raise ValueError("Payload must be between 1 byte and 25 MB")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if payload.get("protocol") != 1 or not isinstance(payload.get("items"), list):
                raise ValueError("Unsupported or malformed bridge payload")
            request_id = str(payload.get("requestId", ""))
            if not request_id:
                raise ValueError("Missing requestId")
            _RESULTS[request_id] = {"ok": True, "state": "pending"}
            _INBOX.put(payload)
            self._headers(202)
            self.wfile.write(json.dumps({"ok": True, "queued": len(payload["items"]), "requestId": request_id}).encode())
        except Exception as exc:
            self._headers(400)
            self.wfile.write(json.dumps({"ok": False, "error": str(exc)}).encode())

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[Figma Bridge HTTP] {fmt % args}")


def _ensure_collection() -> bpy.types.Collection:
    collection = bpy.data.collections.get(COLLECTION_NAME)
    if collection is None:
        collection = bpy.data.collections.new(COLLECTION_NAME)
        bpy.context.scene.collection.children.link(collection)
    return collection


def _delete_previous(source_id: str, root_id: str = "") -> None:
    for obj in list(bpy.data.objects):
        same_source = obj.get("figma_bridge_source_id") == source_id
        same_root = root_id and obj.get("figma_bridge_root_id") == root_id
        if same_source or same_root:
            bpy.data.objects.remove(obj, do_unlink=True)


def _remove_empty_bridge_collections(root: bpy.types.Collection) -> None:
    for child in list(root.children):
        _remove_empty_bridge_collections(child)
        if not child.objects and not child.children:
            root.children.unlink(child)
            if child.users == 0:
                bpy.data.collections.remove(child)


def _import_svg(filepath: str) -> tuple[list[bpy.types.Object], list[bpy.types.Collection]]:
    before = set(bpy.data.objects)
    collections_before = set(bpy.data.collections)
    errors = []
    for operator in (bpy.ops.wm.curve_import, bpy.ops.import_curve.svg):
        try:
            operator(filepath=filepath)
            objects = [obj for obj in bpy.data.objects if obj not in before]
            if objects:
                collections = [collection for collection in bpy.data.collections if collection not in collections_before]
                return objects, collections
        except Exception as exc:
            errors.append(str(exc))
    detail = " | ".join(errors) if errors else "No objects were created"
    raise RuntimeError(f"SVG import failed. Ensure the built-in SVG importer is enabled. {detail}")


def _remove_empty_import_collections(collections: list[bpy.types.Collection]) -> None:
    pending = list(collections)
    while pending:
        removed_any = False
        for collection in list(pending):
            if not collection.objects and not collection.children:
                bpy.data.collections.remove(collection)
                pending.remove(collection)
                removed_any = True
        if not removed_any:
            break


def _remove_legacy_svg_collections() -> None:
    pattern = re.compile(r"^item_\d+\.svg(?:\.\d+)?$")
    leftovers = [
        collection
        for collection in bpy.data.collections
        if pattern.match(collection.name) and not collection.objects and not collection.children
    ]
    _remove_empty_import_collections(leftovers)


def _bridge_objects() -> list[bpy.types.Object]:
    return [obj for obj in bpy.data.objects if obj.get("figma_bridge_source_id")]


def _update_extrude(self: bpy.types.PropertyGroup, _context: bpy.types.Context) -> None:
    for obj in _bridge_objects():
        if obj.type == "CURVE":
            obj.data.extrude = self.extrude


def _update_bevel(self: bpy.types.PropertyGroup, _context: bpy.types.Context) -> None:
    for obj in _bridge_objects():
        if obj.type == "CURVE":
            obj.data.bevel_depth = self.bevel


def _move_to_collection(obj: bpy.types.Object, collection: bpy.types.Collection) -> None:
    for current in list(obj.users_collection):
        current.objects.unlink(obj)
    collection.objects.link(obj)


def _fit_objects_to_bounds(
    objects: list[bpy.types.Object],
    item: dict[str, Any],
    origin_x: float,
    origin_y: float,
    scale: float,
) -> None:
    """Match an imported SVG group to Figma's absolute rendered bounds."""
    corners = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
    if not corners:
        return
    min_x = min(corner.x for corner in corners)
    max_x = max(corner.x for corner in corners)
    min_y = min(corner.y for corner in corners)
    max_y = max(corner.y for corner in corners)
    actual_width = max_x - min_x
    actual_height = max_y - min_y

    unit = 0.001 * scale
    target_width = max(0.0, float(item.get("width", 0.0))) * unit
    target_height = max(0.0, float(item.get("height", 0.0))) * unit
    target_left = (float(item.get("x", origin_x)) - origin_x) * unit
    target_top = -(float(item.get("y", origin_y)) - origin_y) * unit

    scale_x = target_width / actual_width if actual_width > 1e-9 and target_width > 0.0 else 1.0
    scale_y = target_height / actual_height if actual_height > 1e-9 and target_height > 0.0 else scale_x
    transform = (
        Matrix.Translation(Vector((target_left, target_top, 0.0)))
        @ Matrix.Diagonal(Vector((scale_x, scale_y, 1.0, 1.0)))
        @ Matrix.Translation(Vector((-min_x, -max_y, 0.0)))
    )
    for obj in objects:
        obj.matrix_world = transform @ obj.matrix_world


def _ensure_collection_path(root: bpy.types.Collection, path: list[Any]) -> bpy.types.Collection:
    current = root
    identity_parts = []
    for raw_name in path:
        if isinstance(raw_name, dict):
            name = str(raw_name.get("name", "")).strip()
            source_key = str(raw_name.get("id", name)).strip()
        else:
            name = str(raw_name).strip()
            source_key = name
        if not name:
            continue
        identity_parts.append(source_key or name)
        identity = json.dumps(identity_parts, ensure_ascii=False, separators=(",", ":"))
        child = next(
            (candidate for candidate in current.children if candidate.get(COLLECTION_PATH_KEY) == identity),
            None,
        )
        if child is None:
            child = next(
                (candidate for candidate in current.children if candidate.name == name and not candidate.get(COLLECTION_PATH_KEY)),
                None,
            )
        if child is None:
            child = bpy.data.collections.new(name)
            current.children.link(child)
        child[COLLECTION_PATH_KEY] = identity
        child["figma_bridge_source_name"] = name
        current = child
    return current


def _process_payload(payload: dict[str, Any]) -> int:
    scene = bpy.context.scene
    settings = scene.figma_bridge_settings
    collection = _ensure_collection()
    imported_count = 0
    deleted_roots: set[str] = set()
    origin_x = float(payload.get("bounds", {}).get("x", 0.0))
    origin_y = float(payload.get("bounds", {}).get("y", 0.0))
    with tempfile.TemporaryDirectory(prefix="figma_bridge_") as temp_dir:
        for index, item in enumerate(payload["items"]):
            source_id = str(item.get("id", ""))
            svg = item.get("svg")
            if not source_id or not isinstance(svg, str) or "<svg" not in svg:
                continue

            filepath = os.path.join(temp_dir, f"item_{index}.svg")
            Path(filepath).write_text(svg, encoding="utf-8")
            objects, import_collections = _import_svg(filepath)
            if settings.replace_existing:
                root_id = str(item.get("rootId", ""))
                if root_id and root_id not in deleted_roots:
                    _delete_previous(source_id, root_id)
                    deleted_roots.add(root_id)
                elif not root_id:
                    _delete_previous(source_id)
            base_name = str(item.get("name") or f"Figma {index + 1}")
            target_collection = _ensure_collection_path(collection, item.get("collectionPath", []))
            for part, obj in enumerate(objects, start=1):
                obj.name = base_name if len(objects) == 1 else f"{base_name} {part}"
                if obj.data is not None:
                    obj.data.name = obj.name
                obj["figma_bridge_source_id"] = source_id
                obj["figma_bridge_source_name"] = base_name
                obj["figma_bridge_root_id"] = str(item.get("rootId", source_id))
                obj["figma_bridge_file_key"] = str(payload.get("fileKey", ""))
                if obj.type == "CURVE":
                    obj.data.dimensions = "2D"
                    obj.data.extrude = settings.extrude
                    obj.data.bevel_depth = settings.bevel
                    obj.data.resolution_u = 12
                _move_to_collection(obj, target_collection)
                imported_count += 1
            _fit_objects_to_bounds(objects, item, origin_x, origin_y, settings.scale)
            _remove_empty_import_collections(import_collections)

    if settings.replace_existing:
        _remove_empty_bridge_collections(collection)
    _remove_legacy_svg_collections()

    imported = [obj for obj in bpy.data.objects if obj.get("figma_bridge_source_id")]
    bpy.ops.object.select_all(action="DESELECT")
    for obj in imported:
        obj.select_set(True)
    if imported:
        bpy.context.view_layer.objects.active = imported[-1]

    return imported_count


def _drain_inbox() -> float:
    try:
        while True:
            payload = _INBOX.get_nowait()
            request_id = str(payload.get("requestId", ""))
            try:
                count = _process_payload(payload)
                _set_status(f"Imported {count} object(s)")
                _RESULTS[request_id] = {"ok": True, "state": "complete", "imported": count}
            except Exception as exc:
                traceback.print_exc()
                _set_status(f"Import failed: {exc}")
                _RESULTS[request_id] = {"ok": False, "state": "error", "error": str(exc)}
    except queue.Empty:
        pass
    return 0.2


def start_server() -> None:
    global _SERVER, _SERVER_THREAD
    if _SERVER is not None:
        return
    try:
        _SERVER = ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), _BridgeHandler)
        _SERVER.daemon_threads = True
        _SERVER_THREAD = threading.Thread(target=_SERVER.serve_forever, name="FigmaBridge", daemon=True)
        _SERVER_THREAD.start()
        _set_status(f"Listening on 127.0.0.1:{BRIDGE_PORT}")
    except OSError as exc:
        _SERVER = None
        _SERVER_THREAD = None
        _set_status(f"Could not start: {exc}")


def stop_server() -> None:
    global _SERVER, _SERVER_THREAD
    if _SERVER is not None:
        _SERVER.shutdown()
        _SERVER.server_close()
    _SERVER = None
    _SERVER_THREAD = None
    _set_status("Stopped")


class FIGMA_BRIDGE_Settings(bpy.types.PropertyGroup):
    scale: FloatProperty(name="Scale", default=1.0, min=0.0001, soft_max=10.0)
    extrude: FloatProperty(
        name="Extrude",
        default=0.0,
        min=0.0,
        soft_max=1.0,
        subtype="DISTANCE",
        update=_update_extrude,
    )
    bevel: FloatProperty(
        name="Bevel",
        default=0.0,
        min=0.0,
        soft_max=0.1,
        subtype="DISTANCE",
        update=_update_bevel,
    )
    replace_existing: BoolProperty(name="Replace previous push", default=True)


class FIGMA_BRIDGE_OT_toggle(bpy.types.Operator):
    bl_idname = "figma_bridge.toggle_server"
    bl_label = "Toggle Figma Bridge"
    bl_options = {"INTERNAL"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        if _SERVER is None:
            start_server()
        else:
            stop_server()
        return {"FINISHED"}


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.strip().lstrip("vV").split("."))


def _latest_update() -> dict[str, str] | None:
    request = urllib.request.Request(
        GITHUB_LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"FigmaBlenderBridge/{BRIDGE_VERSION}",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        release = json.loads(response.read().decode("utf-8"))
    version = str(release.get("tag_name", "")).lstrip("vV")
    if not version or _version_tuple(version) <= _version_tuple(BRIDGE_VERSION):
        return None
    for asset in release.get("assets", []):
        name = str(asset.get("name", ""))
        if name.startswith("figma_blender_bridge-") and name.endswith(".zip"):
            return {
                "version": version,
                "name": name,
                "url": str(asset.get("browser_download_url", "")),
            }
    raise RuntimeError(f"Release v{version} has no Blender extension ZIP")


def _download_and_validate_update(release: dict[str, str]) -> Path:
    destination = Path(tempfile.gettempdir()) / release["name"]
    request = urllib.request.Request(
        release["url"],
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": f"FigmaBlenderBridge/{BRIDGE_VERSION}",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        destination.write_bytes(response.read())
    with zipfile.ZipFile(destination) as archive:
        manifest = tomllib.loads(archive.read("blender_manifest.toml").decode("utf-8"))
    if manifest.get("id") != "figma_blender_bridge":
        raise RuntimeError("Downloaded package has the wrong extension ID")
    if str(manifest.get("version")) != release["version"]:
        raise RuntimeError("Downloaded package version does not match the GitHub release")
    return destination


def _extension_repo_module() -> str:
    extension_path = Path(__file__).resolve()
    for repo in bpy.context.preferences.extensions.repos:
        try:
            if extension_path.is_relative_to(Path(repo.directory).resolve()):
                return repo.module
        except (OSError, ValueError):
            continue
    return "user_default"


class FIGMA_BRIDGE_OT_check_updates(bpy.types.Operator):
    bl_idname = "figma_bridge.check_updates"
    bl_label = "Check for Updates"
    bl_description = "Check GitHub Releases for a newer bridge version"

    def execute(self, context: bpy.types.Context) -> set[str]:
        global _UPDATE_RELEASE, _UPDATE_STATUS
        try:
            _UPDATE_RELEASE = _latest_update()
            if _UPDATE_RELEASE:
                _UPDATE_STATUS = f"Version {_UPDATE_RELEASE['version']} is available"
                self.report({"INFO"}, _UPDATE_STATUS)
            else:
                _UPDATE_STATUS = f"Version {BRIDGE_VERSION} is current"
                self.report({"INFO"}, "Figma Bridge is up to date")
            return {"FINISHED"}
        except Exception as exc:
            _UPDATE_RELEASE = None
            _UPDATE_STATUS = f"Update check failed: {exc}"
            self.report({"ERROR"}, _UPDATE_STATUS)
            return {"CANCELLED"}


class FIGMA_BRIDGE_OT_install_update(bpy.types.Operator):
    bl_idname = "figma_bridge.install_update"
    bl_label = "Install Update"
    bl_description = "Download and install the available GitHub release"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _UPDATE_RELEASE is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        global _UPDATE_STATUS
        try:
            if _UPDATE_RELEASE is None:
                raise RuntimeError("Check for updates first")
            filepath = _download_and_validate_update(_UPDATE_RELEASE)
            result = bpy.ops.extensions.package_install_files(
                filepath=str(filepath),
                repo=_extension_repo_module(),
                enable_on_install=True,
                overwrite=True,
            )
            if "FINISHED" not in result:
                raise RuntimeError("Blender did not install the downloaded package")
            _UPDATE_STATUS = f"Version {_UPDATE_RELEASE['version']} installed; restart Blender"
            self.report({"INFO"}, _UPDATE_STATUS)
            return {"FINISHED"}
        except Exception as exc:
            _UPDATE_STATUS = f"Update failed: {exc}"
            self.report({"ERROR"}, _UPDATE_STATUS)
            return {"CANCELLED"}


class FIGMA_BRIDGE_OT_convert_all_to_mesh(bpy.types.Operator):
    bl_idname = "figma_bridge.convert_all_to_mesh"
    bl_label = "Convert All to Mesh"
    bl_description = "Convert every bridge-imported curve to a mesh"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        curves = [obj for obj in _bridge_objects() if obj.type == "CURVE"]
        if not curves:
            self.report({"INFO"}, "No imported curve objects to convert")
            return {"CANCELLED"}
        if context.object and context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        for obj in curves:
            obj.select_set(True)
        context.view_layer.objects.active = curves[-1]
        bpy.ops.object.convert(target="MESH")
        self.report({"INFO"}, f"Converted {len(curves)} imported curve object(s) to mesh")
        return {"FINISHED"}


class FIGMA_BRIDGE_OT_remove_all_materials(bpy.types.Operator):
    bl_idname = "figma_bridge.remove_all_materials"
    bl_label = "Remove All Materials"
    bl_description = "Clear material slots from every bridge-imported object"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        changed = 0
        for obj in _bridge_objects():
            if obj.data is not None and hasattr(obj.data, "materials") and obj.data.materials:
                obj.data.materials.clear()
                changed += 1
        self.report({"INFO"}, f"Cleared materials from {changed} imported object(s)")
        return {"FINISHED"}


class FIGMA_BRIDGE_PT_panel(bpy.types.Panel):
    bl_label = "Figma Bridge"
    bl_idname = "FIGMA_BRIDGE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Figma"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        settings = context.scene.figma_bridge_settings
        layout.label(text=_LAST_STATUS, icon="LINKED" if _SERVER else "UNLINKED")
        layout.operator(
            FIGMA_BRIDGE_OT_toggle.bl_idname,
            text="Stop Receiver" if _SERVER else "Start Receiver",
            icon="PAUSE" if _SERVER else "PLAY",
        )
        layout.separator()
        layout.prop(settings, "scale")
        layout.prop(settings, "extrude")
        layout.prop(settings, "bevel")
        layout.prop(settings, "replace_existing")
        layout.separator()
        layout.operator(FIGMA_BRIDGE_OT_convert_all_to_mesh.bl_idname, icon="MESH_DATA")
        layout.operator(FIGMA_BRIDGE_OT_remove_all_materials.bl_idname, icon="MATERIAL")
        layout.separator()
        layout.label(text=_UPDATE_STATUS, icon="INFO")
        row = layout.row(align=True)
        row.operator(FIGMA_BRIDGE_OT_check_updates.bl_idname, icon="FILE_REFRESH")
        if _UPDATE_RELEASE:
            row.operator(
                FIGMA_BRIDGE_OT_install_update.bl_idname,
                text=f"Install {_UPDATE_RELEASE['version']}",
                icon="IMPORT",
            )
        layout.label(text=f"Version {BRIDGE_VERSION}  •  Port {BRIDGE_PORT}")


CLASSES = (
    FIGMA_BRIDGE_Settings,
    FIGMA_BRIDGE_OT_toggle,
    FIGMA_BRIDGE_OT_check_updates,
    FIGMA_BRIDGE_OT_install_update,
    FIGMA_BRIDGE_OT_convert_all_to_mesh,
    FIGMA_BRIDGE_OT_remove_all_materials,
    FIGMA_BRIDGE_PT_panel,
)


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.figma_bridge_settings = bpy.props.PointerProperty(type=FIGMA_BRIDGE_Settings)
    if not bpy.app.timers.is_registered(_drain_inbox):
        bpy.app.timers.register(_drain_inbox, first_interval=0.2, persistent=True)
    start_server()


def unregister() -> None:
    stop_server()
    if bpy.app.timers.is_registered(_drain_inbox):
        bpy.app.timers.unregister(_drain_inbox)
    del bpy.types.Scene.figma_bridge_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
