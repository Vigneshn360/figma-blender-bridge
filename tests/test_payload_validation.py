"""Unit tests for pure Blender bridge payload helpers without importing bpy."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest
from typing import Any


SOURCE = Path(__file__).parents[1] / "blender_extension" / "__init__.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
NAMES = {"MAX_ITEMS", "MAX_SVG_BYTES"}
FUNCTIONS = {"_validate_payload", "_namespace"}
selected: list[ast.stmt] = []
for statement in TREE.body:
    if isinstance(statement, ast.Assign):
        targets = {target.id for target in statement.targets if isinstance(target, ast.Name)}
        if targets & NAMES:
            selected.append(statement)
    elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.name in FUNCTIONS:
        selected.append(statement)

namespace: dict[str, Any] = {"Any": Any, "json": __import__("json"), "math": __import__("math")}
exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), namespace)
validate_payload = namespace["_validate_payload"]
make_namespace = namespace["_namespace"]


def payload() -> dict[str, Any]:
    return {
        "protocol": 1,
        "requestId": "request-1",
        "items": [
            {
                "id": "10:20",
                "svg": "<svg></svg>",
                "collectionPath": [{"id": "page", "name": "Page"}],
                "x": 0,
                "y": 0,
                "width": 100,
                "height": 50,
            }
        ],
    }


class PayloadValidationTests(unittest.TestCase):
    def test_valid_payload(self) -> None:
        value = payload()
        self.assertIs(validate_payload(value), value)

    def test_duplicate_ids_are_rejected(self) -> None:
        value = payload()
        value["items"].append(dict(value["items"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate id"):
            validate_payload(value)

    def test_invalid_collection_path_is_rejected(self) -> None:
        value = payload()
        value["items"][0]["collectionPath"] = "Page/Frame"
        with self.assertRaisesRegex(ValueError, "collectionPath"):
            validate_payload(value)

    def test_non_finite_bounds_are_rejected(self) -> None:
        value = payload()
        value["items"][0]["width"] = float("nan")
        with self.assertRaisesRegex(ValueError, "non-finite"):
            validate_payload(value)

    def test_namespace_is_file_and_page_scoped(self) -> None:
        self.assertNotEqual(make_namespace("file-a", "page"), make_namespace("file-b", "page"))
        self.assertNotEqual(make_namespace("file-a", "page-1"), make_namespace("file-a", "page-2"))

    def test_editable_text_payload(self) -> None:
        value = payload()
        item = value["items"][0]
        item.pop("svg")
        item["kind"] = "text"
        item["text"] = {"characters": "Editable", "fontSize": 24}
        self.assertIs(validate_payload(value), value)

    def test_editable_text_requires_positive_font_size(self) -> None:
        value = payload()
        item = value["items"][0]
        item.pop("svg")
        item["kind"] = "text"
        item["text"] = {"characters": "Editable", "fontSize": 0}
        with self.assertRaisesRegex(ValueError, "fontSize"):
            validate_payload(value)


if __name__ == "__main__":
    unittest.main()
