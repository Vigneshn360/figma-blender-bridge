"""Run with Blender in background mode to verify registration and server lifecycle."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import bpy


source = Path(__file__).parents[1] / "blender_extension" / "__init__.py"
spec = importlib.util.spec_from_file_location("figma_blender_bridge_smoke", source)
if spec is None or spec.loader is None:
    raise RuntimeError("Could not load bridge module")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

assert module.BRIDGE_VERSION == "0.7.1"
module.register()
assert hasattr(bpy.types.Scene, "figma_bridge_settings")
assert module._SERVER is not None

svg = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50"><rect width="100" height="50" fill="#f00"/></svg>'
base_item = {
    "id": "node-old",
    "rootId": "root-1",
    "name": "Stable artwork",
    "type": "RECTANGLE",
    "collectionPath": [{"id": "page-1", "name": "Page"}],
    "x": 0,
    "y": 0,
    "width": 100,
    "height": 50,
    "svg": svg,
}
base_payload = {
    "protocol": 1,
    "requestId": "smoke-initial",
    "fileKey": "file-1",
    "pageId": "page-1",
    "bounds": {"x": 0, "y": 0, "width": 100, "height": 50},
    "items": [base_item],
}
assert module._process_payload(base_payload) == 1
assert any(obj.get("figma_bridge_source_id") == "node-old" for obj in bpy.data.objects)

text_item = {
    "kind": "text",
    "id": "text-1",
    "rootId": "root-text",
    "name": "Editable heading",
    "type": "TEXT",
    "collectionPath": [{"id": "page-1", "name": "Page"}],
    "x": 10,
    "y": 20,
    "width": 240,
    "height": 60,
    "text": {
        "characters": "Edit me in Blender",
        "fontFamily": "Arial",
        "fontStyle": "Regular",
        "fontSize": 32,
        "textAlignHorizontal": "LEFT",
        "textAlignVertical": "TOP",
        "textAutoResize": "NONE",
        "letterSpacing": {"unit": "PIXELS", "value": 1},
        "lineHeight": {"unit": "PERCENT", "value": 120},
        "rotation": 0,
        "transformX": 12,
        "transformY": 24,
        "opacity": 1,
        "fill": {"r": 1, "g": 1, "b": 1, "a": 1},
    },
}
text_payload = {
    **base_payload,
    "requestId": "smoke-text",
    "items": [text_item],
}
assert module._process_payload(text_payload) == 1
text_object = next(obj for obj in bpy.data.objects if obj.get("figma_bridge_source_id") == "text-1")
assert text_object.type == "FONT"
assert text_object.data.body == "Edit me in Blender"
assert text_object.get("figma_bridge_font_family") == "Arial"
assert abs(text_object.location.x - 0.012) < 1e-6
assert abs(text_object.location.y + 0.024) < 1e-6

failed_payload = {
    **base_payload,
    "requestId": "smoke-failed-repush",
    "items": [
        {**base_item, "id": "node-new", "name": "Replacement"},
        {**base_item, "id": "node-broken", "name": "Broken", "svg": "<svg></svg>"},
    ],
}
try:
    module._process_payload(failed_payload)
except RuntimeError:
    pass
else:
    raise AssertionError("Malformed SVG unexpectedly imported")
assert any(obj.get("figma_bridge_source_id") == "node-old" for obj in bpy.data.objects)
assert not any(collection.name.startswith("Figma Bridge Staging") for collection in bpy.data.collections)

module.unregister()
assert not hasattr(bpy.types.Scene, "figma_bridge_settings")
assert module._SERVER is None
print("Blender registration and server lifecycle smoke test passed")
