# Figma to Blender Bridge 0.6.2

Local release finalized on 13 July 2026.

## Included

- `figma_plugin/` — Figma development plugin
- `figma_blender_bridge-0.6.2.zip` — installable Blender extension
- `figma_plugin-0.6.2.zip` — downloadable Figma development plugin update
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
- The Blender panel checks public GitHub Releases and installs validated update packages.
- Group Z Offset live-stacks distinct imported groups along the Z axis.
- The Figma panel checks GitHub Releases and opens its packaged plugin update for download.

## Validation

- JavaScript hierarchy regression test: passed
- Blender extension Python compilation: passed
- Blender extension archive manifest/version: verified as 0.6.2

Version 0.4.2 remains in the development folder as the pre-UI stable fallback.
