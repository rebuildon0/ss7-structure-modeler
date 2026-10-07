import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs";
import rhino3dm from "https://cdn.jsdelivr.net/npm/rhino3dm@8.35.0/rhino3dm.module.min.js";

const PYODIDE_URL = "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/";
const MAX_CSV_BYTES = 100 * 1024 * 1024;

const pyodideReady = (async () => {
  const pyodide = await loadPyodide({ indexURL: PYODIDE_URL });
  await pyodide.loadPackage("Pillow");
  const response = await fetch(new URL("./convert_ss7_general.py", import.meta.url));
  if (!response.ok) throw new Error("変換プログラムを読み込めませんでした。");
  pyodide.FS.writeFile("/home/pyodide/convert_ss7_general.py", await response.text());
  pyodide.runPython("import convert_ss7_general as converter");
  return pyodide;
})();

const rhinoReady = rhino3dm({
  locateFile: (file) => `https://cdn.jsdelivr.net/npm/rhino3dm@8.35.0/${file}`,
});

function colorForKind(kind, colors) {
  const color = colors[kind] || [180, 180, 180];
  return { r: color[0], g: color[1], b: color[2], a: 255 };
}

function make3dm(model, rhino, colors, kindFromLayer) {
  const document = new rhino.File3dm();
  const settings = document.settings();
  settings.modelUnitSystem = rhino.UnitSystem.Millimeters;
  settings.modelAbsoluteTolerance = 0.01;
  settings.modelAngleToleranceDegrees = 1;

  const layers = document.layers();
  const layerIndexes = new Map();
  for (const name of [...new Set(model.objects.map((object) => object.layer))].sort()) {
    const layer = new rhino.Layer();
    layer.name = name;
    layer.color = colorForKind(kindFromLayer(name), colors);
    layerIndexes.set(name, layers.add(layer));
  }

  for (const object of model.objects) {
    const mesh = new rhino.Mesh();
    const vertices = mesh.vertices();
    for (const [x, y, z] of object.vertices) vertices.add(x, y, z);
    const faces = mesh.faces();
    for (const face of object.faces) {
      if (face.length === 3) faces.addTriFace(...face);
      else if (face.length === 4) faces.addQuadFace(...face);
      else throw new Error(`未対応の面形状: ${object.name}`);
    }
    mesh.normals().computeNormals();
    mesh.compact();
    const attributes = new rhino.ObjectAttributes();
    attributes.name = object.name;
    attributes.layerIndex = layerIndexes.get(object.layer);
    for (const [key, value] of Object.entries({
      Kind: object.kind,
      Level: object.level,
      Symbol: object.symbol,
      Source: object.source,
      SectionMM: object.section_mm.join("x"),
    })) attributes.setUserString(`SS7.${key}`, String(value));
    const adjustments = object.placement_adjustments || {};
    if (Object.keys(adjustments).length) {
      attributes.setUserString("SS7.PlacementAdjustments", JSON.stringify(adjustments));
    }
    if (object.kind === "BEAMS" && Object.keys(adjustments).length) {
      for (const [key, value] of Object.entries({
        BeamLevelControl: adjustments.beam_level_control,
        BeamLevelDimensionMM: adjustments.beam_level_dimension_mm,
        BeamCenterOffsetZMM: adjustments.beam_center_offset_z_mm,
        BeamLevelSource: adjustments.beam_level_source,
      })) attributes.setUserString(`SS7.${key}`, String(value));
    }
    if (object.kind === "COLUMNS" && adjustments.column_length) {
      attributes.setUserString("SS7.ColumnBottomExtensionMM", String(adjustments.column_length.bottom_extension_mm));
      attributes.setUserString("SS7.ColumnTopExtensionMM", String(adjustments.column_length.top_extension_mm));
    }
    document.objects().addMesh(mesh, attributes);
  }

  const bytes = document.toByteArray();
  const check = rhino.File3dm.fromByteArray(bytes);
  if (!check) throw new Error("作成した3DMを再読込できませんでした。");
  const objectCount = check.objects().count;
  const layerCount = check.layers().count;
  const unitMillimeters = check.settings().modelUnitSystem === rhino.UnitSystem.Millimeters;
  let invalidCount = 0;
  let openMeshCount = 0;
  for (let index = 0; index < objectCount; index += 1) {
    const geometry = check.objects().get(index).geometry();
    if (!geometry.isValid) invalidCount += 1;
    if (!geometry.isClosed) openMeshCount += 1;
  }
  const verification = {
    written: true,
    valid: objectCount === model.objects.length && layerCount === layerIndexes.size && unitMillimeters && invalidCount === 0,
    object_count_expected: model.objects.length,
    object_count_readback: objectCount,
    layer_count_expected: layerIndexes.size,
    layer_count_readback: layerCount,
    units_readback: unitMillimeters ? "Millimeters" : String(check.settings().modelUnitSystem),
    invalid_geometry_count: invalidCount,
    open_mesh_count: openMeshCount,
  };
  check.destroy();
  document.destroy();
  return { bytes, verification };
}

function readOutput(pyodide, filename, label, description, type) {
  const path = `/tmp/ss7-web-output/${filename}`;
  try {
    return { filename, label, description, type, bytes: pyodide.FS.readFile(path) };
  } catch {
    return null;
  }
}

async function convert(buffer, prefix) {
  if (!buffer.byteLength || buffer.byteLength > MAX_CSV_BYTES) {
    throw new Error("CSVは1バイト以上、100MB以下にしてください。");
  }
  const pyodide = await pyodideReady;
  const rhino = await rhinoReady;
  const safePrefix = prefix.replace(/[^A-Za-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 80) || "ss7";
  pyodide.runPython("import shutil; shutil.rmtree('/tmp/ss7-web-output', ignore_errors=True)");
  pyodide.FS.mkdir("/tmp/ss7-web-output");
  pyodide.FS.writeFile("/tmp/ss7-web-input.csv", new Uint8Array(buffer));
  pyodide.globals.set("web_prefix", safePrefix);
  try {
    const modelJson = pyodide.runPython(`
import json
from pathlib import Path
web_source = Path('/tmp/ss7-web-input.csv')
web_output = Path('/tmp/ss7-web-output')
web_model = converter.make_model(web_source)
(web_output / f'{web_prefix}_model_data.json').write_text(json.dumps(web_model, ensure_ascii=False, indent=2), encoding='utf-8')
converter.write_obj(web_model, web_output / f'{web_prefix}_rhino_model.obj')
converter.write_dxf(web_model, web_output / f'{web_prefix}_rhino_model.dxf')
converter.write_preview(web_model, web_output / f'{web_prefix}_rhino_preview.png')
json.dumps(web_model, ensure_ascii=False)
`);
    const model = JSON.parse(modelJson);
    const colors = JSON.parse(pyodide.runPython("json.dumps(converter.COLORS)"));
    const kinds = JSON.parse(pyodide.runPython("json.dumps({name: converter.kind_from_layer(name) for name in {obj['layer'] for obj in web_model['objects']}})"));
    const { bytes: modelBytes, verification } = make3dm(model, rhino, colors, (name) => kinds[name]);
    pyodide.FS.writeFile(`/tmp/ss7-web-output/${safePrefix}_rhino_model.3dm`, modelBytes);
    pyodide.globals.set("web_verification_json", JSON.stringify(verification));
    pyodide.runPython(`
web_verification = json.loads(web_verification_json)
(web_output / f'{web_prefix}_3dm_verify.json').write_text(json.dumps(web_verification, ensure_ascii=False, indent=2), encoding='utf-8')
web_artifacts = {
  'json': str(web_output / f'{web_prefix}_model_data.json'),
  'obj': str(web_output / f'{web_prefix}_rhino_model.obj'),
  'mtl': str(web_output / f'{web_prefix}_rhino_model.mtl'),
  'dxf': str(web_output / f'{web_prefix}_rhino_model.dxf'),
  'preview': str(web_output / f'{web_prefix}_rhino_preview.png'),
  '3dm': str(web_output / f'{web_prefix}_rhino_model.3dm'),
  '3dm_verify': str(web_output / f'{web_prefix}_3dm_verify.json'),
}
converter.write_report(web_model, web_output / f'{web_prefix}_conversion_report.md', web_source, web_artifacts, web_verification)
`);
    const artifacts = [
      { filename: `${safePrefix}_rhino_model.3dm`, label: "Rhino 3DM", description: "Rhinoで開くメインファイル", type: "model/vnd.3dm", bytes: modelBytes },
      readOutput(pyodide, `${safePrefix}_rhino_model.dxf`, "DXF", "3DMを使わない場合の予備形式", "application/dxf"),
      readOutput(pyodide, `${safePrefix}_rhino_model.obj`, "OBJ", "汎用3D形式", "text/plain"),
      readOutput(pyodide, `${safePrefix}_rhino_model.mtl`, "MTL", "OBJの素材定義", "text/plain"),
      readOutput(pyodide, `${safePrefix}_conversion_report.md`, "変換レポート", "反映内容と注意点", "text/markdown"),
      readOutput(pyodide, `${safePrefix}_model_data.json`, "監査用JSON", "全形状の変換記録", "application/json"),
      readOutput(pyodide, `${safePrefix}_3dm_verify.json`, "3DM検証JSON", "再読込検証の記録", "application/json"),
    ].filter(Boolean);
    const preview = readOutput(pyodide, `${safePrefix}_rhino_preview.png`, "プレビュー", "", "image/png");
    return {
      projectName: model.project_name || safePrefix,
      counts: model.object_counts || {},
      stories: model.preflight?.stories_bottom_up || [],
      layers: model.preflight?.layers_bottom_up || [],
      skippedSections: model.skipped_sections || [],
      fallbackSections: model.fallback_sections || [],
      verification,
      artifacts,
      preview,
    };
  } finally {
    pyodide.runPython("for _name in ('web_prefix', 'web_verification_json'): globals().pop(_name, None)");
    pyodide.runPython("import shutil; shutil.rmtree('/tmp/ss7-web-output', ignore_errors=True)");
    pyodide.FS.unlink("/tmp/ss7-web-input.csv");
  }
}

self.onmessage = async (event) => {
  if (event.data.type !== "convert") return;
  try {
    self.postMessage({ type: "status", message: "変換エンジンを読み込んでいます" });
    const result = await convert(event.data.buffer, event.data.prefix);
    const transfer = result.artifacts.map((item) => item.bytes.buffer);
    if (result.preview) transfer.push(result.preview.bytes.buffer);
    self.postMessage({ type: "complete", result }, transfer);
  } catch (error) {
    const lines = String(error?.message || "").trim().split(/\r?\n/);
    self.postMessage({ type: "error", message: lines[lines.length - 1] || "変換できませんでした。" });
  }
};
