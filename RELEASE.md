# Figma to Blender Bridge 0.7.1

Version 0.7.1 adds editable Figma text while retaining the safe import behavior introduced in 0.7.0.

## Install files

- `figma_blender_bridge-0.7.1.zip` — Blender 5.1+ extension
- `figma_plugin-0.7.1.zip` — Figma development plugin

## Editable text

- Uniform Figma text layers are transferred as native Blender `FONT` objects.
- Text remains editable through Blender's normal text editing tools.
- Transfers characters, requested font family/style, font size, horizontal and vertical alignment, line height, letter spacing, colour, opacity and rotation.
- Searches common Windows, macOS and Linux font directories for a matching installed font.
- Stores the requested Figma font metadata and whether a matching font was found on the Blender object.
- Mixed-font, mixed-size, stroked and effect-heavy text automatically falls back to outlined SVG.
- Atomic painted, masked or clipped containers remain SVG units, including any text inside them.
- Figma reports how many text layers were editable and how many were outlined.

## Compatibility

- Blender 5.1.0 or newer
- Figma plugin API 1.0.0
- Bridge protocol 1 with a backward-compatible optional `kind: "text"` item

Versions 0.6.3 and 0.7.0 remain unchanged.
