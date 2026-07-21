"""Figma to Blender Bridge — Blender 5.1 extension."""

from __future__ import annotations

import json
import math
import os
import queue
import tempfile
import threading
import time
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
BRIDGE_VERSION = "0.7.1"
GITHUB_REPOSITORY = "Vigneshn360/figma-blender-bridge"
GITHUB_LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
COLLECTION_NAME = "Figma Bridge"
COLLECTION_PATH_KEY = "figma_bridge_collection_path"
OWNED_KEY = "figma_bridge_owned"
NAMESPACE_KEY = "figma_bridge_namespace"
MAX_PAYLOAD_BYTES = 25 * 1024 * 1024
MAX_ITEMS = 500
MAX_SVG_BYTES = 10 * 1024 * 1024
MAX_QUEUE_SIZE = 8
RESULT_TTL_SECONDS = 300.0
_SERVER: ThreadingHTTPServer | None = None
_SERVER_THREAD: threading.Thread | None = None
_INBOX: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=MAX_QUEUE_SIZE)
_RESULTS: dict[str, dict[str, Any]] = {}
_RESULTS_LOCK = threading.Lock()
_LAST_STATUS = "Stopped"
_UPDATE_STATUS = "Updates not checked"
_UPDATE_RELEASE: dict[str, str] | None = None
_PENDING_UPDATE: tuple[str, str, str] | None = None
_FONT_CACHE: dict[tuple[str, str], bpy.types.VectorFont | None] = {}


def _set_status(message: str) -> None:
    global _LAST_STATUS
    _LAST_STATUS = message
    print(f"[Figma Bridge] {message}")


def _set_result(request_id: str, **values: Any) -> None:
    with _RESULTS_LOCK:
        _RESULTS[request_id] = {**values, "updatedAt": time.monotonic()}


def _prune_results() -> None:
    cutoff = time.monotonic() - RESULT_TTL_SECONDS
    with _RESULTS_LOCK:
        expired = [key for key, value in _RESULTS.items() if float(value.get("updatedAt", 0.0)) < cutoff]
        for key in expired:
            _RESULTS.pop(key, None)


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key != "updatedAt"}


def _validate_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("protocol") != 1:
        raise ValueError("Unsupported or malformed bridge payload")
    items = payload.get("items")
    if not isinstance(items, list) or not items or len(items) > MAX_ITEMS:
        raise ValueError(f"Payload must contain between 1 and {MAX_ITEMS} items")
    request_id = str(payload.get("requestId", "")).strip()
    if not request_id or len(request_id) > 200:
        raise ValueError("Missing or invalid requestId")
    seen_ids: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"Item {index + 1} must be an object")
        source_id = str(item.get("id", "")).strip()
        if not source_id or source_id in seen_ids:
            raise ValueError(f"Item {index + 1} has a missing or duplicate id")
        seen_ids.add(source_id)
        kind = str(item.get("kind", "svg"))
        if kind == "text":
            text = item.get("text")
            if not isinstance(text, dict) or not isinstance(text.get("characters"), str):
                raise ValueError(f"Item {index + 1} does not contain valid editable text")
            try:
                font_size = float(text.get("fontSize", 0.0))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Item {index + 1} has an invalid text fontSize") from exc
            if not math.isfinite(font_size) or font_size <= 0.0:
                raise ValueError(f"Item {index + 1} has an invalid text fontSize")
        elif kind == "svg":
            svg = item.get("svg")
            if not isinstance(svg, str) or "<svg" not in svg.lower():
                raise ValueError(f"Item {index + 1} does not contain valid SVG text")
            if len(svg.encode("utf-8")) > MAX_SVG_BYTES:
                raise ValueError(f"Item {index + 1} SVG exceeds 10 MB")
        else:
            raise ValueError(f"Item {index + 1} has unsupported kind {kind!r}")
        if not isinstance(item.get("collectionPath", []), list):
            raise ValueError(f"Item {index + 1} collectionPath must be an array")
        numbers: dict[str, float] = {}
        for key in ("x", "y", "width", "height"):
            try:
                value = float(item.get(key, 0.0))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Item {index + 1} has an invalid {key}") from exc
            if value != value or abs(value) == float("inf"):
                raise ValueError(f"Item {index + 1} has a non-finite {key}")
            numbers[key] = value
        if numbers["width"] < 0.0 or numbers["height"] < 0.0:
            raise ValueError(f"Item {index + 1} has negative dimensions")
    return payload


class _BridgeHandler(BaseHTTPRequestHandler):
    server_version = f"FigmaBlenderBridge/{BRIDGE_VERSION}"

    def _headers(self, status: int = 200, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._headers(204)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/result/"):
            request_id = self.path.removeprefix("/result/")
            _prune_results()
            with _RESULTS_LOCK:
                result = _RESULTS.get(request_id)
                if result and result.get("state") in {"complete", "error"}:
                    _RESULTS.pop(request_id, None)
            if result is None:
                self._headers(404)
                self.wfile.write(b'{"ok":false,"state":"missing","error":"unknown or expired request"}')
                return
            self._headers()
            self.wfile.write(json.dumps(_public_result(result)).encode())
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
            if "application/json" not in self.headers.get("Content-Type", "").lower():
                raise ValueError("Content-Type must be application/json")
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_PAYLOAD_BYTES:
                raise ValueError("Payload must be between 1 byte and 25 MB")
            payload = _validate_payload(json.loads(self.rfile.read(length).decode("utf-8")))
            request_id = str(payload.get("requestId", ""))
            _prune_results()
            with _RESULTS_LOCK:
                if request_id in _RESULTS:
                    raise ValueError("Duplicate requestId")
            _set_result(request_id, ok=True, state="pending", current=0, total=len(payload["items"]))
            try:
                _INBOX.put_nowait(payload)
            except queue.Full as exc:
                with _RESULTS_LOCK:
                    _RESULTS.pop(request_id, None)
                raise ValueError("Blender import queue is full; try again shortly") from exc
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
    collection[OWNED_KEY] = True
    return collection


def _namespace(file_key: str, page_id: str) -> str:
    return json.dumps([file_key or "local-file", page_id or "unknown-page"], ensure_ascii=False, separators=(",", ":"))


def _delete_previous(source_id: str, root_id: str, file_key: str, page_id: str) -> None:
    matches = []
    for obj in list(bpy.data.objects):
        stored_file = str(obj.get("figma_bridge_file_key", ""))
        stored_page = str(obj.get("figma_bridge_page_id", ""))
        same_namespace = stored_file == file_key and (not stored_page or stored_page == page_id)
        same_source = same_namespace and obj.get("figma_bridge_source_id") == source_id
        same_root = same_namespace and root_id and obj.get("figma_bridge_root_id") == root_id
        if same_source or same_root:
            matches.append(obj)
    _discard_objects(matches)


def _discard_objects(objects: list[bpy.types.Object]) -> None:
    data_blocks = [obj.data for obj in objects if obj.data is not None]
    for obj in list(objects):
        if obj.name in bpy.data.objects:
            bpy.data.objects.remove(obj, do_unlink=True)
    for data in data_blocks:
        if data.users == 0 and isinstance(data, bpy.types.Curve):
            bpy.data.curves.remove(data)


def _remove_empty_bridge_collections(root: bpy.types.Collection) -> None:
    for child in list(root.children):
        _remove_empty_bridge_collections(child)
        if child.get(OWNED_KEY) and not child.objects and not child.children:
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
            _discard_objects([obj for obj in bpy.data.objects if obj not in before])
            for collection in [item for item in bpy.data.collections if item not in collections_before]:
                if not collection.objects and not collection.children:
                    bpy.data.collections.remove(collection)
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


def _bridge_objects() -> list[bpy.types.Object]:
    return [obj for obj in bpy.data.objects if obj.get("figma_bridge_source_id")]


def _update_extrude(self: bpy.types.PropertyGroup, _context: bpy.types.Context) -> None:
    for obj in _bridge_objects():
        if obj.type in {"CURVE", "FONT"}:
            obj.data.extrude = self.extrude


def _update_bevel(self: bpy.types.PropertyGroup, _context: bpy.types.Context) -> None:
    for obj in _bridge_objects():
        if obj.type in {"CURVE", "FONT"}:
            obj.data.bevel_depth = self.bevel


def _group_identity(obj: bpy.types.Object) -> str:
    for collection in obj.users_collection:
        identity = collection.get(COLLECTION_PATH_KEY)
        if identity:
            return f"{collection.get(NAMESPACE_KEY, '')}:{identity}"
    return ""


def _update_group_z_offset(self: bpy.types.PropertyGroup, _context: bpy.types.Context) -> None:
    group_indices: dict[str, int] = {}
    used_indices: set[int] = set()
    next_index = 0
    for obj in _bridge_objects():
        identity = _group_identity(obj) or f"object:{obj.get('figma_bridge_source_id', obj.name)}"
        if identity not in group_indices:
            stored_index = obj.get("figma_bridge_group_index")
            candidate = int(stored_index) if stored_index is not None else next_index
            while candidate in used_indices:
                candidate += 1
            group_indices[identity] = candidate
            used_indices.add(candidate)
            next_index = max(next_index, candidate + 1)
        group_index = group_indices[identity]
        obj["figma_bridge_group_index"] = group_index
        obj.location.z = group_index * self.group_z_offset


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


def _normalized_font_name(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def _font_directories() -> list[Path]:
    directories = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
        Path.home() / ".fonts",
        Path.home() / ".local/share/fonts",
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / "Library/Fonts",
        Path("/Library/Fonts"),
        Path("/System/Library/Fonts"),
    ]
    return [directory for directory in directories if directory.is_dir()]


def _load_matching_font(family: str, style: str) -> bpy.types.VectorFont | None:
    key = (family.casefold(), style.casefold())
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    family_key = _normalized_font_name(family)
    style_key = _normalized_font_name(style)
    candidates: list[tuple[int, Path]] = []
    if family_key:
        for directory in _font_directories():
            try:
                files = directory.rglob("*")
                for filepath in files:
                    if filepath.suffix.lower() not in {".ttf", ".otf", ".ttc"}:
                        continue
                    stem = _normalized_font_name(filepath.stem)
                    if family_key not in stem:
                        continue
                    score = abs(len(stem) - len(family_key))
                    if style_key and style_key not in {"regular", "normal"}:
                        score += 0 if style_key in stem else 100
                    candidates.append((score, filepath))
            except OSError:
                continue
    font = None
    for _score, filepath in sorted(candidates, key=lambda item: (item[0], str(item[1]))):
        try:
            font = bpy.data.fonts.load(str(filepath), check_existing=True)
            break
        except (OSError, RuntimeError):
            continue
    _FONT_CACHE[key] = font
    return font


def _text_material(fill: dict[str, Any], opacity: float) -> bpy.types.Material:
    rgba = tuple(max(0.0, min(1.0, float(fill.get(channel, default)))) for channel, default in (
        ("r", 1.0), ("g", 1.0), ("b", 1.0), ("a", 1.0)
    ))
    alpha = rgba[3] * max(0.0, min(1.0, opacity))
    name = "Figma Text #{:02X}{:02X}{:02X}{:02X}".format(
        round(rgba[0] * 255), round(rgba[1] * 255), round(rgba[2] * 255), round(alpha * 255)
    )
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    material.diffuse_color = (rgba[0], rgba[1], rgba[2], alpha)
    return material


def _spacing_factor(value: Any, font_size: float, default: float, additive: bool = False) -> float:
    if not isinstance(value, dict):
        return default
    try:
        amount = float(value.get("value", 0.0))
    except (TypeError, ValueError):
        return default
    unit = str(value.get("unit", "")).upper()
    factor = amount / 100.0 if unit == "PERCENT" else amount / font_size if unit == "PIXELS" else default
    return max(0.0, (1.0 + factor) if additive and factor != default else factor)


def _create_text_object(
    item: dict[str, Any],
    origin_x: float,
    origin_y: float,
    scale: float,
    settings: bpy.types.PropertyGroup,
) -> bpy.types.Object:
    text = item["text"]
    name = str(item.get("name") or "Figma Text")
    font_size = float(text["fontSize"])
    unit = 0.001 * scale
    data = bpy.data.curves.new(name=name, type="FONT")
    obj = bpy.data.objects.new(name, data)
    data.body = str(text.get("characters", ""))
    data.size = font_size * unit
    data.dimensions = "2D"
    data.extrude = settings.extrude
    data.bevel_depth = settings.bevel
    data.resolution_u = 12
    data.align_x = {"LEFT": "LEFT", "CENTER": "CENTER", "RIGHT": "RIGHT", "JUSTIFIED": "JUSTIFY"}.get(
        str(text.get("textAlignHorizontal", "LEFT")), "LEFT"
    )
    data.align_y = {"TOP": "TOP", "CENTER": "CENTER", "BOTTOM": "BOTTOM"}.get(
        str(text.get("textAlignVertical", "TOP")), "TOP"
    )
    data.space_character = _spacing_factor(text.get("letterSpacing"), font_size, 1.0, additive=True)
    data.space_line = _spacing_factor(text.get("lineHeight"), font_size, 1.0)
    if str(text.get("textAutoResize", "NONE")) in {"NONE", "HEIGHT", "TRUNCATE"}:
        data.text_boxes[0].width = max(0.0, float(item.get("width", 0.0))) * unit
        data.text_boxes[0].height = max(0.0, float(item.get("height", 0.0))) * unit
    font = _load_matching_font(str(text.get("fontFamily", "")), str(text.get("fontStyle", "Regular")))
    if font is not None:
        data.font = font
    fill = text.get("fill")
    if isinstance(fill, dict):
        data.materials.append(_text_material(fill, float(text.get("opacity", 1.0))))
    transform_x = text.get("transformX")
    transform_y = text.get("transformY")
    source_x = float(item.get("x", origin_x)) if transform_x is None else float(transform_x)
    source_y = float(item.get("y", origin_y)) if transform_y is None else float(transform_y)
    obj.location = (
        (source_x - origin_x) * unit,
        -(source_y - origin_y) * unit,
        0.0,
    )
    obj.rotation_euler.z = math.radians(-float(text.get("rotation", 0.0)))
    obj["figma_bridge_font_family"] = str(text.get("fontFamily", ""))
    obj["figma_bridge_font_style"] = str(text.get("fontStyle", "Regular"))
    obj["figma_bridge_font_matched"] = font is not None
    return obj


def _ensure_collection_path(
    root: bpy.types.Collection,
    path: list[Any],
    namespace: str,
) -> bpy.types.Collection:
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
            (
                candidate
                for candidate in current.children
                if candidate.get(COLLECTION_PATH_KEY) == identity
                and candidate.get(NAMESPACE_KEY) == namespace
            ),
            None,
        )
        if child is None:
            # Migrate a 0.6.x bridge collection once, without adopting arbitrary user collections.
            child = next(
                (
                    candidate
                    for candidate in current.children
                    if candidate.get(COLLECTION_PATH_KEY) == identity
                    and not candidate.get(NAMESPACE_KEY)
                ),
                None,
            )
        if child is None:
            child = bpy.data.collections.new(name)
            current.children.link(child)
        child[COLLECTION_PATH_KEY] = identity
        child[NAMESPACE_KEY] = namespace
        child[OWNED_KEY] = True
        child["figma_bridge_source_name"] = name
        current = child
    return current


def _process_payload(payload: dict[str, Any]) -> int:
    scene = bpy.context.scene
    settings = scene.figma_bridge_settings
    collection = _ensure_collection()
    request_id = str(payload.get("requestId", ""))
    file_key = str(payload.get("fileKey", ""))
    page_id = str(payload.get("pageId", ""))
    source_namespace = _namespace(file_key, page_id)
    origin_x = float(payload.get("bounds", {}).get("x", 0.0))
    origin_y = float(payload.get("bounds", {}).get("y", 0.0))
    staging_collection = bpy.data.collections.new(f"Figma Bridge Staging {request_id[-12:]}")
    staging_collection[OWNED_KEY] = True
    staged: list[tuple[int, dict[str, Any], list[bpy.types.Object]]] = []
    staged_objects: list[bpy.types.Object] = []

    try:
        with tempfile.TemporaryDirectory(prefix="figma_bridge_") as temp_dir:
            total = len(payload["items"])
            for index, item in enumerate(payload["items"]):
                if item.get("kind", "svg") == "text":
                    objects = [_create_text_object(item, origin_x, origin_y, settings.scale, settings)]
                    import_collections = []
                else:
                    filepath = os.path.join(temp_dir, f"item_{index}.svg")
                    Path(filepath).write_text(str(item["svg"]), encoding="utf-8")
                    objects, import_collections = _import_svg(filepath)
                staged_objects.extend(objects)
                for obj in objects:
                    _move_to_collection(obj, staging_collection)
                if item.get("kind", "svg") != "text":
                    _fit_objects_to_bounds(objects, item, origin_x, origin_y, settings.scale)
                _remove_empty_import_collections(import_collections)
                staged.append((index, item, objects))
                _set_result(
                    request_id,
                    ok=True,
                    state="processing",
                    current=index + 1,
                    total=total,
                )
    except Exception:
        _discard_objects(staged_objects)
        if staging_collection.name in bpy.data.collections:
            bpy.data.collections.remove(staging_collection)
        raise

    group_indices: dict[str, int] = {}
    prepared: list[tuple[int, dict[str, Any], list[bpy.types.Object], bpy.types.Collection, int]] = []
    try:
        for index, item, objects in staged:
            target_collection = _ensure_collection_path(
                collection,
                item.get("collectionPath", []),
                source_namespace,
            )
            group_identity = f"{source_namespace}:{target_collection.get(COLLECTION_PATH_KEY, target_collection.name)}"
            if group_identity not in group_indices:
                group_indices[group_identity] = len(group_indices)
            prepared.append((index, item, objects, target_collection, group_indices[group_identity]))
    except Exception:
        _discard_objects(staged_objects)
        if staging_collection.name in bpy.data.collections:
            bpy.data.collections.remove(staging_collection)
        _remove_empty_bridge_collections(collection)
        raise

    if settings.replace_existing:
        deleted_roots: set[str] = set()
        for _index, item, _objects, _target_collection, _group_index in prepared:
            source_id = str(item["id"])
            root_id = str(item.get("rootId", ""))
            delete_key = root_id or source_id
            if delete_key not in deleted_roots:
                _delete_previous(source_id, root_id, file_key, page_id)
                deleted_roots.add(delete_key)

    imported_count = 0
    for index, item, objects, target_collection, group_index in prepared:
        source_id = str(item["id"])
        base_name = str(item.get("name") or f"Figma {index + 1}")
        for part, obj in enumerate(objects, start=1):
            obj.name = base_name if len(objects) == 1 else f"{base_name} {part}"
            if obj.data is not None:
                obj.data.name = obj.name
            obj["figma_bridge_source_id"] = source_id
            obj["figma_bridge_source_name"] = base_name
            obj["figma_bridge_root_id"] = str(item.get("rootId", source_id))
            obj["figma_bridge_file_key"] = file_key
            obj["figma_bridge_page_id"] = page_id
            obj["figma_bridge_namespace"] = source_namespace
            obj["figma_bridge_node_type"] = str(item.get("type", ""))
            obj["figma_bridge_collection_path"] = json.dumps(item.get("collectionPath", []), ensure_ascii=False)
            obj["figma_bridge_group_index"] = group_index
            if obj.type in {"CURVE", "FONT"}:
                obj.data.dimensions = "2D"
                obj.data.extrude = settings.extrude
                obj.data.bevel_depth = settings.bevel
                obj.data.resolution_u = 12
            _move_to_collection(obj, target_collection)
            obj.location.z = group_index * settings.group_z_offset
            imported_count += 1

    if staging_collection.name in bpy.data.collections:
        bpy.data.collections.remove(staging_collection)

    if settings.replace_existing:
        _remove_empty_bridge_collections(collection)

    bpy.ops.object.select_all(action="DESELECT")
    for obj in staged_objects:
        obj.select_set(True)
    if staged_objects:
        bpy.context.view_layer.objects.active = staged_objects[-1]

    return imported_count


def _drain_inbox() -> float:
    try:
        while True:
            payload = _INBOX.get_nowait()
            request_id = str(payload.get("requestId", ""))
            try:
                count = _process_payload(payload)
                _set_status(f"Imported {count} object(s)")
                _set_result(request_id, ok=True, state="complete", imported=count)
            except Exception as exc:
                traceback.print_exc()
                _set_status(f"Import failed: {exc}")
                _set_result(request_id, ok=False, state="error", error=str(exc))
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
    group_z_offset: FloatProperty(
        name="Group Z Offset",
        description="Distance between distinct imported Figma groups on the Z axis",
        default=0.0,
        soft_min=-1.0,
        soft_max=1.0,
        subtype="DISTANCE",
        update=_update_group_z_offset,
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


def _install_pending_update() -> None:
    """Install after the initiating operator has left Blender's RNA call stack.

    Installing this package unregisters and replaces this module. In particular,
    do not access extension globals or an Operator instance after the install call.
    """
    global _PENDING_UPDATE, _UPDATE_STATUS
    pending = _PENDING_UPDATE
    _PENDING_UPDATE = None
    if pending is None:
        return None
    filepath, repo, version = pending
    _UPDATE_STATUS = f"Installing version {version}; restart Blender when complete"
    stop_server()
    try:
        bpy.ops.extensions.package_install_files(
            filepath=filepath,
            repo=repo,
            enable_on_install=True,
            overwrite=True,
        )
    except Exception as exc:
        # A failed install leaves this module loaded, so recovery is safe here.
        _UPDATE_STATUS = f"Update failed: {exc}"
        traceback.print_exc()
        start_server()
    return None


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
        return _UPDATE_RELEASE is not None and _PENDING_UPDATE is None

    def execute(self, context: bpy.types.Context) -> set[str]:
        global _PENDING_UPDATE, _UPDATE_STATUS
        try:
            if _UPDATE_RELEASE is None:
                raise RuntimeError("Check for updates first")
            filepath = _download_and_validate_update(_UPDATE_RELEASE)
            version = _UPDATE_RELEASE["version"]
            _PENDING_UPDATE = (str(filepath), _extension_repo_module(), version)
            _UPDATE_STATUS = f"Version {version} downloaded; installation queued"
            self.report({"INFO"}, _UPDATE_STATUS)
            bpy.app.timers.register(_install_pending_update, first_interval=0.25)
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
        curves = [obj for obj in _bridge_objects() if obj.type in {"CURVE", "FONT"}]
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
        layout.prop(settings, "group_z_offset")
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
