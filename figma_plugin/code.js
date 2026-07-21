const ENDPOINT = "http://localhost:51982";
const BRIDGE_VERSION = "0.7.1";
const GITHUB_LATEST_RELEASE_API = "https://api.github.com/repos/Vigneshn360/figma-blender-bridge/releases/latest";
let figmaUpdate = null;

figma.showUI(__html__, { width: 390, height: 390, themeColors: true });

function postStatus(type, message, details) {
  figma.ui.postMessage({ type, message, details: details || "" });
}

function versionTuple(value) {
  return String(value).replace(/^v/i, "").split(".").map(part => Number(part));
}

function isNewerVersion(candidate, current) {
  const left = versionTuple(candidate);
  const right = versionTuple(current);
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index++) {
    const difference = (left[index] || 0) - (right[index] || 0);
    if (difference !== 0) return difference > 0;
  }
  return false;
}

async function checkPluginUpdate() {
  try {
    figma.ui.postMessage({ type: "update-status", message: "Checking for Figma plugin updates…", available: false });
    const response = await fetch(GITHUB_LATEST_RELEASE_API);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const release = await response.json();
    const version = String(release.tag_name || "").replace(/^v/i, "");
    const asset = (release.assets || []).find(item => /^figma_plugin-.*\.zip$/.test(item.name || ""));
    if (version && isNewerVersion(version, BRIDGE_VERSION)) {
      if (!asset) throw new Error(`Release ${version} has no Figma plugin ZIP`);
      figmaUpdate = { version, url: asset.browser_download_url };
      figma.ui.postMessage({
        type: "update-status",
        message: `Figma plugin ${version} is available`,
        available: true,
        version
      });
    } else {
      figmaUpdate = null;
      figma.ui.postMessage({
        type: "update-status",
        message: `Figma plugin ${BRIDGE_VERSION} is current`,
        available: false
      });
    }
  } catch (error) {
    figmaUpdate = null;
    figma.ui.postMessage({
      type: "update-status",
      message: `Update check failed: ${String(error)}`,
      available: false
    });
  }
}

async function checkConnection() {
  try {
    const response = await fetch(`${ENDPOINT}/status`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const info = await response.json();
    postStatus("connection", "Blender is connected", `Receiver ${info.version || "unknown"} • Plugin ${BRIDGE_VERSION}`);
  } catch (error) {
    postStatus("connection-error", "Blender is not connected", String(error));
  }
}

function collectionPath(node) {
  const names = [{ id: figma.currentPage.id, name: figma.currentPage.name }];
  const ancestors = [];
  let current = node.parent;
  while (current && current.type !== "PAGE" && current.type !== "DOCUMENT") {
    if (current.name) ancestors.unshift({ id: current.id, name: current.name });
    current = current.parent;
  }
  return names.concat(ancestors);
}

function collectionRef(node) {
  return { id: node.id, name: node.name };
}

function isContainer(node) {
  return node.type === "GROUP" || node.type === "FRAME" || node.type === "SECTION";
}

function hasVisiblePaint(paints) {
  return Array.isArray(paints) && paints.some(paint => paint.visible !== false && paint.opacity !== 0);
}

function mustStayAtomic(node) {
  if (!isContainer(node)) return true;
  if (node.opacity !== undefined && node.opacity < 1) return true;
  if (node.effects && node.effects.length) return true;
  if (node.clipsContent) return true;
  if (hasVisiblePaint(node.fills) || hasVisiblePaint(node.strokes)) return true;
  if (node.children && node.children.some(child => child.isMask)) return true;
  return false;
}

function collectExportUnits(node, path, rootId, units) {
  if (isContainer(node) && !mustStayAtomic(node)) {
    const childPath = path.concat(node.name ? [collectionRef(node)] : []);
    for (const child of node.children) collectExportUnits(child, childPath, rootId, units);
    return;
  }
  const atomicPath = path.concat(isContainer(node) && node.name ? [collectionRef(node)] : []);
  units.push({ node, collectionPath: atomicPath, rootId });
}

function hasSelectedAncestor(node, selectedIds) {
  let current = node.parent;
  while (current && current.type !== "PAGE" && current.type !== "DOCUMENT") {
    if (selectedIds.has(current.id)) return true;
    current = current.parent;
  }
  return false;
}

function visibleSolidFill(fills) {
  if (!Array.isArray(fills)) return null;
  const visible = fills.filter(fill => fill.visible !== false && fill.opacity !== 0);
  if (visible.length !== 1 || visible[0].type !== "SOLID") return null;
  const fill = visible[0];
  return {
    r: fill.color.r,
    g: fill.color.g,
    b: fill.color.b,
    a: (fill.opacity === undefined ? 1 : fill.opacity)
  };
}

function displayedCharacters(node) {
  const value = String(node.characters || "");
  if (node.textCase === "UPPER") return value.toUpperCase();
  if (node.textCase === "LOWER") return value.toLowerCase();
  if (node.textCase === "TITLE") return value.replace(/\b\p{L}/gu, character => character.toUpperCase());
  return value;
}

function editableTextData(node) {
  if (node.type !== "TEXT" || node.hasMissingFont) return null;
  if (!node.fontName || typeof node.fontName !== "object" || typeof node.fontSize !== "number") return null;
  if (hasVisiblePaint(node.strokes) || (node.effects && node.effects.length)) return null;
  const fill = visibleSolidFill(node.fills);
  if (!fill) return null;
  const letterSpacing = node.letterSpacing && typeof node.letterSpacing === "object" ? node.letterSpacing : null;
  const lineHeight = node.lineHeight && typeof node.lineHeight === "object" ? node.lineHeight : null;
  const transform = Array.isArray(node.absoluteTransform) ? node.absoluteTransform : null;
  return {
    characters: displayedCharacters(node),
    fontFamily: String(node.fontName.family || ""),
    fontStyle: String(node.fontName.style || "Regular"),
    fontSize: node.fontSize,
    textAlignHorizontal: node.textAlignHorizontal || "LEFT",
    textAlignVertical: node.textAlignVertical || "TOP",
    textAutoResize: node.textAutoResize || "NONE",
    letterSpacing,
    lineHeight,
    rotation: typeof node.rotation === "number" ? node.rotation : 0,
    transformX: transform ? transform[0][2] : null,
    transformY: transform ? transform[1][2] : null,
    opacity: typeof node.opacity === "number" ? node.opacity : 1,
    fill
  };
}

async function pushSelection(options) {
  const selection = figma.currentPage.selection;
  if (!selection.length) {
    postStatus("error", "Select at least one layer in Figma.");
    return;
  }

  postStatus("working", `Preparing ${selection.length} selected layer(s)…`);
  try {
    const selectedIds = new Set(selection.map(node => node.id));
    const units = [];
    const roots = selection.filter(node => !hasSelectedAncestor(node, selectedIds));
    for (const root of roots) collectExportUnits(root, collectionPath(root), root.id, units);
    const items = [];
    let editableTextCount = 0;
    let outlinedTextCount = 0;
    for (const unit of units) {
      const node = unit.node;
      if (typeof node.exportAsync !== "function") continue;
      const bounds = node.absoluteRenderBounds || node.absoluteBoundingBox;
      if (!bounds) continue;
      const text = options.textMode !== "outline" ? editableTextData(node) : null;
      if (text) {
        editableTextCount += 1;
        items.push({
          kind: "text",
          id: node.id,
          rootId: unit.rootId,
          name: node.name,
          type: node.type,
          collectionPath: unit.collectionPath,
          x: bounds.x,
          y: bounds.y,
          width: bounds.width,
          height: bounds.height,
          text
        });
        continue;
      }
      if (node.type === "TEXT") outlinedTextCount += 1;
      const svg = await node.exportAsync({
        format: "SVG_STRING",
        svgOutlineText: true,
        svgIdAttribute: true,
        svgSimplifyStroke: false
      });
      items.push({
        kind: "svg",
        id: node.id,
        rootId: unit.rootId,
        name: node.name,
        type: node.type,
        collectionPath: unit.collectionPath,
        x: bounds.x,
        y: bounds.y,
        width: bounds.width,
        height: bounds.height,
        svg
      });
    }
    if (!items.length) throw new Error("The selection contains no exportable layers.");
    const minX = Math.min(...items.map(item => item.x));
    const minY = Math.min(...items.map(item => item.y));
    const maxX = Math.max(...items.map(item => item.x + item.width));
    const maxY = Math.max(...items.map(item => item.y + item.height));

    const requestId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const response = await fetch(`${ENDPOINT}/push`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        protocol: 1,
        requestId,
        source: "figma",
        fileKey: figma.fileKey || "",
        pageId: figma.currentPage.id,
        pageName: figma.currentPage.name,
        rootIds: roots.map(node => node.id),
        sentAt: new Date().toISOString(),
        bounds: { x: minX, y: minY, width: maxX - minX, height: maxY - minY },
        items
      })
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || `HTTP ${response.status}`);
    postStatus("working", "Blender is importing the artwork…", `0 of ${items.length} export units`);
    const imported = await waitForImport(requestId);
    const textSummary = `${editableTextCount} editable text, ${outlinedTextCount} outlined text`;
    postStatus("success", `Imported ${imported} Blender object(s).`, `${textSummary}. The imported objects are selected; press Home in Blender to frame them.`);
  } catch (error) {
    postStatus("error", "Could not push to Blender.", String(error));
  }
}

async function waitForImport(requestId) {
  let lastProgress = "";
  for (let attempt = 0; attempt < 600; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 100));
    const response = await fetch(`${ENDPOINT}/result/${encodeURIComponent(requestId)}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    if (result.state === "processing") {
      const progress = `${result.current || 0} of ${result.total || 0} export units`;
      if (progress !== lastProgress) {
        lastProgress = progress;
        postStatus("working", "Blender is importing the artwork…", progress);
      }
    }
    if (result.state === "complete") return result.imported;
    if (result.state === "error") throw new Error(result.error || "Blender import failed");
  }
  throw new Error("Blender did not finish the import within 60 seconds.");
}

figma.ui.onmessage = async (message) => {
  if (message.type === "check") await checkConnection();
  if (message.type === "push") await pushSelection(message.options || { textMode: "automatic" });
  if (message.type === "check-update") await checkPluginUpdate();
  if (message.type === "download-update" && figmaUpdate) figma.openExternal(figmaUpdate.url);
  if (message.type === "close") figma.closePlugin();
};

checkConnection();
checkPluginUpdate();
