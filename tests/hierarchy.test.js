const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const page = { id: "page", type: "PAGE", name: "UI Page", selection: [] };
const uiMessages = [];
const context = {
  __html__: "",
  console,
  setTimeout,
  fetch: async () => ({ ok: true, json: async () => ({ ok: true }) }),
  figma: {
    currentPage: page,
    fileKey: "test",
    showUI() {},
    closePlugin() {},
    openExternal() {},
    ui: { postMessage(message) { uiMessages.push(message); }, onmessage: null }
  }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(__dirname + "/../figma_plugin/code.js", "utf8"), context);

function node(id, type, name, parent, extra) {
  return Object.assign({ id, type, name, parent, opacity: 1, effects: [], fills: [], strokes: [], clipsContent: false }, extra || {});
}

const frame = node("frame", "FRAME", "Screen", page, { children: [] });
const group = node("group", "GROUP", "Header", frame, { children: [] });
const leaf = node("leaf", "VECTOR", "Logo", group);
frame.children.push(group);
group.children.push(leaf);

assert.deepStrictEqual(JSON.parse(JSON.stringify(context.collectionPath(frame))), [{ id: "page", name: "UI Page" }]);
const units = [];
context.collectExportUnits(frame, context.collectionPath(frame), frame.id, units);
assert.strictEqual(units.length, 1);
assert.strictEqual(units[0].node, leaf);
assert.deepStrictEqual(JSON.parse(JSON.stringify(units[0].collectionPath)), [
  { id: "page", name: "UI Page" },
  { id: "frame", name: "Screen" },
  { id: "group", name: "Header" }
]);
assert.strictEqual(units[0].rootId, "frame");

const paintedFrame = node("painted", "FRAME", "Card", page, {
  children: [leaf],
  fills: [{ type: "SOLID", visible: true, opacity: 1 }]
});
const atomicUnits = [];
context.collectExportUnits(paintedFrame, [{ id: "page", name: "UI Page" }], paintedFrame.id, atomicUnits);
assert.strictEqual(atomicUnits.length, 1);
assert.strictEqual(atomicUnits[0].node, paintedFrame);
assert.deepStrictEqual(JSON.parse(JSON.stringify(atomicUnits[0].collectionPath)), [
  { id: "page", name: "UI Page" },
  { id: "painted", name: "Card" }
]);

assert.strictEqual(context.hasSelectedAncestor(leaf, new Set([frame.id, leaf.id])), true);
assert.strictEqual(context.hasSelectedAncestor(frame, new Set([frame.id, leaf.id])), false);
assert.strictEqual(context.isNewerVersion("0.6.3", "0.6.2"), true);
assert.strictEqual(context.isNewerVersion("0.6.2", "0.6.3"), false);

context.fetch = async () => ({
  ok: true,
  json: async () => ({
    tag_name: "v0.7.0",
    assets: [{ name: "figma_plugin-0.7.0.zip", browser_download_url: "https://example.test/update.zip" }]
  })
});
context.checkPluginUpdate().then(() => {
  const updateMessage = uiMessages.find(message => message.type === "update-status" && message.available);
  assert.ok(updateMessage);
  assert.strictEqual(updateMessage.version, "0.7.0");
  console.log("Figma hierarchy and updater tests passed");
}).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
