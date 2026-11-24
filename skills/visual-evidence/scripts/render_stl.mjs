#!/usr/bin/env node
// Render a binary STL to seven PNG views: iso, plus-x, minus-x, plus-y,
// minus-y, plus-z, minus-z. Orthographic, grey on white, one light for all
// views. Built-in Node modules only. ASCII STL is not supported.
//
// Usage: node render_stl.mjs FILE.stl OUTDIR [--size 800] [--frame OTHER.stl]
// --frame sets the scale and centre from the union of both files. For before
// and after, give each side the other as --frame. A size change then shows.
// Then tile the views with visual_evidence.py sheet, which adds the captions.

import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { deflateSync } from "node:zlib";
import { basename, extname, join } from "node:path";

const SUPERSAMPLE = 2; // render at 2x size, then average 2x2 blocks
const MARGIN = 0.86; // the model fills this share of the square image
const CREASE_COS = Math.cos((30 * Math.PI) / 180); // no smoothing across sharper edges
const AMBIENT = 0.3;
const DIFFUSE = 0.55;
const BASE_GREY = 140;

// Each view: the direction the camera looks, and the up direction.
const VIEWS = {
  "plus-x": [[-1, 0, 0], [0, 0, 1]],
  "minus-x": [[1, 0, 0], [0, 0, 1]],
  "plus-y": [[0, -1, 0], [0, 0, 1]],
  "minus-y": [[0, 1, 0], [0, 0, 1]],
  "plus-z": [[0, 0, -1], [0, 1, 0]],
  "minus-z": [[0, 0, 1], [0, 1, 0]],
  iso: [[-1, -1, -1], [0, 0, 1]],
};

function die(message) {
  console.error(`render_stl: ${message}`);
  process.exit(1);
}

function sub(a, b) {
  return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
}

function dot(a, b) {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

function cross(a, b) {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}

function normalize(v) {
  const len = Math.hypot(v[0], v[1], v[2]);
  return len > 0 ? [v[0] / len, v[1] / len, v[2] / len] : [0, 0, 0];
}

const LIGHT = normalize([0.4, 0.55, 1]); // fixed in world space

// Returns Float64Array of 9 numbers per triangle: three corners, x y z each.
function readBinarySTL(buf) {
  if (buf.length < 84) die("file is too short for a binary STL");
  const count = buf.readUInt32LE(80);
  if (count === 0) die("STL has no triangles");
  if (buf.length !== 84 + count * 50) die("not a binary STL (ASCII STL is not supported)");
  const tris = new Float64Array(count * 9);
  let offset = 84;
  for (let i = 0; i < count; i++) {
    offset += 12; // skip the stored normal; it is recomputed from the corners
    for (let k = 0; k < 9; k++) {
      tris[i * 9 + k] = buf.readFloatLE(offset);
      offset += 4;
    }
    offset += 2; // attribute byte count
  }
  return tris;
}

// Per-corner normals. A corner blends the faces that share its position,
// but skips faces across a sharp edge, so edges stay crisp.
function cornerNormals(tris) {
  const count = tris.length / 9;
  const faceNormal = new Float64Array(count * 3);
  const facesAt = new Map();
  for (let f = 0; f < count; f++) {
    const t = tris.subarray(f * 9, f * 9 + 9);
    const a = [t[0], t[1], t[2]];
    const b = [t[3], t[4], t[5]];
    const c = [t[6], t[7], t[8]];
    const n = normalize(cross(sub(b, a), sub(c, a)));
    faceNormal.set(n, f * 3);
    for (let k = 0; k < 3; k++) {
      const key = [t[k * 3], t[k * 3 + 1], t[k * 3 + 2]].map((v) => Math.round(v * 1e4)).join(",");
      if (!facesAt.has(key)) facesAt.set(key, []);
      facesAt.get(key).push(f);
    }
  }
  const out = new Float64Array(count * 9);
  for (let f = 0; f < count; f++) {
    const fn = [faceNormal[f * 3], faceNormal[f * 3 + 1], faceNormal[f * 3 + 2]];
    for (let k = 0; k < 3; k++) {
      const t = tris.subarray(f * 9, f * 9 + 9);
      const key = [t[k * 3], t[k * 3 + 1], t[k * 3 + 2]].map((v) => Math.round(v * 1e4)).join(",");
      let sum = [0, 0, 0];
      for (const g of facesAt.get(key)) {
        const gn = [faceNormal[g * 3], faceNormal[g * 3 + 1], faceNormal[g * 3 + 2]];
        if (dot(gn, fn) >= CREASE_COS) sum = [sum[0] + gn[0], sum[1] + gn[1], sum[2] + gn[2]];
      }
      out.set(normalize(sum), f * 9 + k * 3);
    }
  }
  return out;
}

// Right and up vectors for a camera that looks along fwd.
function basis(fwd, up) {
  const right = normalize(cross(fwd, up));
  const trueUp = normalize(cross(right, fwd));
  return [right, trueUp];
}

// Screen-space box of the vertices. Used to fit the part in the frame.
function screenBounds(tris, right, up) {
  const b = { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity };
  for (let i = 0; i < tris.length; i += 3) {
    const p = [tris[i], tris[i + 1], tris[i + 2]];
    const x = dot(p, right);
    const y = dot(p, up);
    b.minX = Math.min(b.minX, x);
    b.maxX = Math.max(b.maxX, x);
    b.minY = Math.min(b.minY, y);
    b.maxY = Math.max(b.maxY, y);
  }
  return b;
}

function fillTriangle(xs, ys, ds, is, depth, rgb, size) {
  const den = (ys[1] - ys[2]) * (xs[0] - xs[2]) + (xs[2] - xs[1]) * (ys[0] - ys[2]);
  if (Math.abs(den) < 1e-12) return; // degenerate
  const minX = Math.max(0, Math.floor(Math.min(...xs)));
  const maxX = Math.min(size - 1, Math.ceil(Math.max(...xs)));
  const minY = Math.max(0, Math.floor(Math.min(...ys)));
  const maxY = Math.min(size - 1, Math.ceil(Math.max(...ys)));
  for (let py = minY; py <= maxY; py++) {
    for (let px = minX; px <= maxX; px++) {
      const cx = px + 0.5;
      const cy = py + 0.5;
      const w0 = ((ys[1] - ys[2]) * (cx - xs[2]) + (xs[2] - xs[1]) * (cy - ys[2])) / den;
      const w1 = ((ys[2] - ys[0]) * (cx - xs[2]) + (xs[0] - xs[2]) * (cy - ys[2])) / den;
      const w2 = 1 - w0 - w1;
      if (w0 < -1e-9 || w1 < -1e-9 || w2 < -1e-9) continue;
      const z = w0 * ds[0] + w1 * ds[1] + w2 * ds[2];
      const idx = py * size + px;
      if (z >= depth[idx]) continue;
      depth[idx] = z;
      const intensity = Math.min(1, w0 * is[0] + w1 * is[1] + w2 * is[2]);
      const v = Math.round(BASE_GREY * intensity);
      rgb[idx * 3] = v;
      rgb[idx * 3 + 1] = v;
      rgb[idx * 3 + 2] = v;
    }
  }
}

function renderView(tris, normals, fwd, up, size, frame) {
  const [right, trueUp] = basis(fwd, up);
  const b = screenBounds(tris, right, trueUp);
  if (frame) {
    const f = screenBounds(frame, right, trueUp);
    b.minX = Math.min(b.minX, f.minX);
    b.maxX = Math.max(b.maxX, f.maxX);
    b.minY = Math.min(b.minY, f.minY);
    b.maxY = Math.max(b.maxY, f.maxY);
  }
  const count = tris.length / 9;
  const sx = new Float64Array(count * 3);
  const sy = new Float64Array(count * 3);
  const sd = new Float64Array(count * 3);
  const si = new Float64Array(count * 3);
  for (let i = 0; i < count * 3; i++) {
    const p = [tris[i * 3], tris[i * 3 + 1], tris[i * 3 + 2]];
    sx[i] = dot(p, right);
    sy[i] = dot(p, trueUp);
    sd[i] = dot(p, fwd); // smaller is nearer the camera
    // Light faces the camera. Flip a normal that points away.
    let n = [normals[i * 3], normals[i * 3 + 1], normals[i * 3 + 2]];
    if (dot(n, fwd) > 0) n = [-n[0], -n[1], -n[2]];
    si[i] = Math.min(1, AMBIENT + DIFFUSE * Math.max(0, dot(n, LIGHT)));
  }
  const scale = (MARGIN * size) / Math.max(b.maxX - b.minX, b.maxY - b.minY);
  const cx = (b.minX + b.maxX) / 2;
  const cy = (b.minY + b.maxY) / 2;
  const px = new Float64Array(count * 3);
  const py = new Float64Array(count * 3);
  for (let i = 0; i < count * 3; i++) {
    px[i] = (sx[i] - cx) * scale + size / 2;
    py[i] = size / 2 - (sy[i] - cy) * scale;
  }
  const rgb = new Uint8Array(size * size * 3).fill(255);
  const depth = new Float64Array(size * size).fill(Infinity);
  for (let f = 0; f < count; f++) {
    const c = [f * 3, f * 3 + 1, f * 3 + 2];
    fillTriangle(
      c.map((i) => px[i]),
      c.map((i) => py[i]),
      c.map((i) => sd[i]),
      c.map((i) => si[i]),
      depth,
      rgb,
      size,
    );
  }
  return rgb;
}

// Average each 2x2 block of the supersampled buffer.
function downsample(rgb, size) {
  const out = new Uint8Array(size * size * 3);
  const big = size * SUPERSAMPLE;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      for (let c = 0; c < 3; c++) {
        let sum = 0;
        for (let dy = 0; dy < SUPERSAMPLE; dy++) {
          for (let dx = 0; dx < SUPERSAMPLE; dx++) {
            const bx = x * SUPERSAMPLE + dx;
            const by = y * SUPERSAMPLE + dy;
            sum += rgb[(by * big + bx) * 3 + c];
          }
        }
        out[(y * size + x) * 3 + c] = Math.round(sum / (SUPERSAMPLE * SUPERSAMPLE));
      }
    }
  }
  return out;
}

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(bytes) {
  let c = 0xffffffff;
  for (const b of bytes) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

// A PNG chunk is: length, type, data, then a CRC over type and data.
function pngChunk(type, data) {
  const typed = Buffer.concat([Buffer.from(type, "ascii"), Buffer.from(data)]);
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length, 0);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(typed), 0);
  return Buffer.concat([length, typed, crc]);
}

function encodePNG(width, height, rgb) {
  const raw = Buffer.alloc((width * 3 + 1) * height);
  for (let y = 0; y < height; y++) {
    raw[y * (width * 3 + 1)] = 0; // filter type: none
    Buffer.from(rgb.buffer, y * width * 3, width * 3).copy(raw, y * (width * 3 + 1) + 1);
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 2; // colour type: RGB
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    pngChunk("IHDR", ihdr),
    pngChunk("IDAT", deflateSync(raw)),
    pngChunk("IEND", Buffer.alloc(0)),
  ]);
}

function parseArgs(argv) {
  const positional = [];
  let size = 800;
  let frameFile = null;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--size") {
      size = Number(argv[++i]);
      if (!Number.isInteger(size) || size < 16) die("--size needs an integer of at least 16");
    } else if (argv[i] === "--frame") {
      frameFile = argv[++i];
      if (!frameFile) die("--frame needs a file path");
    } else {
      positional.push(argv[i]);
    }
  }
  if (positional.length !== 2) {
    die("usage: node render_stl.mjs FILE.stl OUTDIR [--size 800] [--frame OTHER.stl]");
  }
  return { file: positional[0], outDir: positional[1], size, frameFile };
}

function main() {
  const { file, outDir, size, frameFile } = parseArgs(process.argv.slice(2));
  const tris = readBinarySTL(readFileSync(file));
  const frame = frameFile ? readBinarySTL(readFileSync(frameFile)) : null;
  const normals = cornerNormals(tris);
  mkdirSync(outDir, { recursive: true });
  const stem = basename(file, extname(file));
  for (const [name, [fwd, up]] of Object.entries(VIEWS)) {
    const big = renderView(tris, normals, normalize(fwd), up, size * SUPERSAMPLE, frame);
    const rgb = downsample(big, size);
    const path = join(outDir, `${stem}-${name}.png`);
    writeFileSync(path, encodePNG(size, size, rgb));
    console.log(`wrote ${path}`);
  }
}

main();
