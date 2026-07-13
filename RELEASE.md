# Figma to Blender Bridge 0.5.0

Local release finalized on 13 July 2026.

## Included

- `figma_plugin/` — Figma development plugin
- `figma_blender_bridge-0.5.0.zip` — installable Blender extension
- `blender_extension/` — readable Blender extension source
- `tests/` — hierarchy regression test
- `README.md` — installation, usage, behavior, and limitations

## Verified behavior

- Figma page, frame, and group hierarchy is preserved as nested Blender Collections.
- Imported artwork matches Figma rendered bounds and alignment.
- Blender SVG scratch collections are removed automatically.
- Re-pushing a root replaces stale descendants.
- Extrude and Bevel update imported curves after import.
- Imported curves can be converted to meshes from the N-panel.
- Material slots can be cleared from all imported objects.
- Both panels display version information.

## Validation

- JavaScript hierarchy regression test: passed
- Blender extension Python compilation: passed
- Blender extension archive manifest/version: verified as 0.5.0

Version 0.4.2 remains in the development folder as the pre-UI stable fallback.
