# Figma to Blender Bridge 0.5.0 — Collection Hierarchy

Push selected Figma layers into Blender 5.1+ as editable curve objects over a localhost-only connection.

## Install the Blender extension

1. In Blender, open **Edit → Preferences → Extensions**.
2. Use the menu in the top-right and choose **Install from Disk**.
3. Select `figma_blender_bridge-0.5.0.zip`.
4. Enable **Figma to Blender Bridge** if it is not already enabled.
5. In the 3D View, open the sidebar and choose the **Figma** tab. It should say it is listening.

## Install the Figma development plugin

1. In the Figma desktop app, open any design file.
2. Choose **Plugins → Development → Import plugin from manifest**.
3. Select the `manifest.json` inside the `figma_plugin` folder.
4. Run **Plugins → Development → Push to Blender**.

## Use it

1. Keep Blender open with the receiver running.
2. Select one or more layers in Figma.
3. Run **Push to Blender**, then click **Push selection**.
4. The result appears under the **Figma Bridge** collection, organized by Figma page, frame, and group.
5. Imported objects are selected automatically. Press **Home** in Blender if they are outside the current view.

The Blender panel controls scale, extrusion, bevel, and whether a repeated push replaces objects from the same Figma layer.

The Figma panel and Blender N-panel show their version numbers. In Blender, Extrude and Bevel update all imported curves immediately. The panel also includes controls to convert all imported curves to meshes, clear their material slots, and open the installed add-on folder for updates.

## Current scope

- Plain frames and groups are traversed so their hierarchy becomes nested Blender Collections.
- Nested artwork uses absolute Figma bounds, preserving placement when exported separately.
- Every imported SVG unit is fitted to its Figma rendered bounding box, preserving alignment and frame proportions across separate SVG imports.
- Painted, clipped, masked, translucent, or effect-bearing containers remain atomic to retain their SVG appearance.
- Duplicate group names in separate hierarchy branches remain separate Blender Collections.
- Text is outlined by default for reliable appearance.
- Re-pushing a selected root replaces all earlier Blender objects from that root, including deleted or reorganized descendants.
- Truly empty collections left by replacement or hierarchy changes are removed automatically.
- Temporary `item_N.svg` collections created by Blender's SVG importer are removed after their curves are relocated; older empty leftovers are cleaned on the next push.
- No Blender parenting is created.
- No Empty objects are created.
- Imported curve objects and curve datablocks use their source Figma layer name.
- Communication stays on `localhost:51982`; there is no cloud service.

## Known limitations

- Blender's SVG importer determines which SVG paint features become curve materials; advanced gradients, masks, blend modes, and effects may be simplified.
- Containers that must remain atomic cannot expose their internal layers as Blender Collections without changing their rendered appearance.
- Editable text is experimental when **Outline text** is disabled and depends on matching fonts.
- This MVP is one-way: Figma to Blender.

If Figma cannot connect, confirm Blender is open, the extension is enabled, and another application is not using port 51982.
