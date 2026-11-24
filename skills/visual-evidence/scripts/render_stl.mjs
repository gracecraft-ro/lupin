#!/usr/bin/env node
// Render a binary STL to eight PNG views: iso, iso-cutaway, plus-x, minus-x,
// plus-y, minus-y, plus-z, minus-z. Orthographic. Built-in Node modules only.
// ASCII STL is not supported.
//
// Usage: node render_stl.mjs FILE.stl OUTDIR [--size 800] [--frame OTHER.stl]
// --frame sets the scale and centre from the union of both files. For before
// and after, give each side the other as --frame. A size change then shows.
// iso-cutaway removes the half of the part nearest the iso camera, cut through
// the centre. The cut face is drawn in a warm colour, so wall thickness shows.
// Each part of the surface has a pastel colour set by its place in the part,
// so a spot keeps its colour from every view.
// Then tile the views with visual_evidence.py sheet, which adds the captions.

import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { deflateSync } from "node:zlib";
import { basename, extname, join } from "node:path";

const SUPERSAMPLE = 2; // render at 2x size, then average 2x2 blocks
const MARGIN = 0.86; // the model fills this share of the square image
const CREASE_COS = Math.cos((30 * Math.PI) / 180); // no smoothing across sharper edges

// Colours are 0 to 255, except RIM and the light colours, which are linear (0 to 1).
// Light is summed in linear values, then compressed to display values.
const BG_CENTRE = [251, 250, 247]; // the ground is lighter at the centre...
const BG_EDGE = [234, 231, 225]; // ...and warmer toward the corners
const CUT_FACE = [236, 166, 112];
// Part colour is a pastel set by place in the part (sRGB, 0 to 1). Each axis sets one
// channel: x red, y green, z blue. A spot keeps its colour from every view.
const PASTEL_LOW = [0.62, 0.62, 0.62];
const PASTEL_HIGH = [0.98, 0.98, 0.98];
const EXPOSURE = 2.2; // scales the summed light before the roll-off
const SKY_LIGHT = [0.7, 0.72, 0.76]; // ambient light from above
const GROUND_LIGHT = [0.5, 0.44, 0.38]; // warm bounce from below
const RIM = [0.8, 0.88, 1.0]; // cool light on edges turned away from the camera
const RIM_GAIN = 0.35;
const RIM_POWER = 3;
const SPEC_GAIN = 0.3;
const SPEC_POWER = 40;
const EDGE_STRENGTH = 0.4; // how dark the ink line gets where a surface meets a farther one
const EDGE_STEP = 0.01; // depth gap, as a share of the part size, that draws a line
const AO_STRENGTH = 0.4; // how dark the most occluded pixels get
const AO_RADIUS = 0.03; // share of the image size
const AO_RANGE = 0.1; // depth gap, as a share of the part size, that counts as full occlusion
const AO_BIAS = 0.04; // smaller depth gaps are slope, not occlusion
const W_UP = [0, 0, 1];

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

// Light directions are in camera space: x right, y up on screen, z toward the camera.
// The same lights light every view, so each view is lit the same way.
const LIGHTS = [
  { dir: normalize([-0.45, 0.6, 0.66]), colour: [1.0, 0.93, 0.82], gain: 0.7 }, // key, upper left
  { dir: normalize([0.8, -0.25, 0.55]), colour: [0.85, 0.9, 1.0], gain: 0.2 }, // fill, right
  { dir: normalize([-0.95, 0.12, 0.28]), colour: [1.0, 0.97, 0.9], gain: 0.55 }, // raking, low from the left
].map((light) => ({ ...light, half: normalize([light.dir[0], light.dir[1], light.dir[2] + 1]) }));

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

// Outward faces give a positive signed volume. A file wound the other way is turned around.
function orientOutward(tris) {
  let volume = 0;
  for (let f = 0; f < tris.length; f += 9) {
    const a = [tris[f], tris[f + 1], tris[f + 2]];
    const b = [tris[f + 3], tris[f + 4], tris[f + 5]];
    const c = [tris[f + 6], tris[f + 7], tris[f + 8]];
    volume += dot(a, cross(b, c));
  }
  if (volume >= 0) return;
  for (let f = 0; f < tris.length; f += 9) {
    for (let k = 0; k < 3; k++) {
      const t = tris[f + 3 + k];
      tris[f + 3 + k] = tris[f + 6 + k];
      tris[f + 6 + k] = t;
    }
  }
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

// Linear albedo at a point. The pastel runs from PASTEL_LOW to PASTEL_HIGH across the bounds.
function albedoAt(p, bounds) {
  return [0, 1, 2].map((k) => {
    const span = bounds.hi[k] - bounds.lo[k];
    const t = span > 0 ? (p[k] - bounds.lo[k]) / span : 0.5;
    return (PASTEL_LOW[k] + (PASTEL_HIGH[k] - PASTEL_LOW[k]) * t) ** 2.2;
  });
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

// Box around every vertex in the given arrays, as lo and hi corners.
function boundsOf(arrays) {
  const lo = [Infinity, Infinity, Infinity];
  const hi = [-Infinity, -Infinity, -Infinity];
  for (const arr of arrays) {
    for (let i = 0; i < arr.length; i += 3) {
      for (let k = 0; k < 3; k++) {
        lo[k] = Math.min(lo[k], arr[i + k]);
        hi[k] = Math.max(hi[k], arr[i + k]);
      }
    }
  }
  return { lo, hi };
}

function centreOf(bounds) {
  return [0, 1, 2].map((k) => (bounds.lo[k] + bounds.hi[k]) / 2);
}

// Keep the part on the far side of the cut: where dot(p, fwd) >= planeDepth.
// Triangles that cross the cut are split. Normals are interpolated along the cut.
function clipFar(tris, normals, fwd, planeDepth) {
  const outTris = [];
  const outNormals = [];
  for (let f = 0; f < tris.length / 9; f++) {
    const verts = [0, 1, 2].map((k) => {
      const p = [tris[f * 9 + k * 3], tris[f * 9 + k * 3 + 1], tris[f * 9 + k * 3 + 2]];
      const n = [normals[f * 9 + k * 3], normals[f * 9 + k * 3 + 1], normals[f * 9 + k * 3 + 2]];
      return { p, n, s: dot(p, fwd) - planeDepth };
    });
    const poly = [];
    for (let i = 0; i < 3; i++) {
      const a = verts[i];
      const b = verts[(i + 1) % 3];
      if (a.s >= 0) poly.push(a);
      if ((a.s >= 0) !== (b.s >= 0)) {
        const t = a.s / (a.s - b.s);
        poly.push({
          p: a.p.map((v, k) => v + (b.p[k] - v) * t),
          n: normalize(a.n.map((v, k) => v + (b.n[k] - v) * t)),
        });
      }
    }
    for (let k = 1; k + 1 < poly.length; k++) {
      for (const v of [poly[0], poly[k], poly[k + 1]]) {
        outTris.push(...v.p);
        outNormals.push(...v.n);
      }
    }
  }
  return { tris: Float64Array.from(outTris), normals: Float64Array.from(outNormals) };
}

// Write the nearest triangle at each pixel. back is 1 when that face points away
// from the camera. After a cut, that means the ray starts inside the part.
function fillTriangle(buf, size, xs, ys, ds, ns, alb, back) {
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
      if (z >= buf.depth[idx]) continue;
      buf.depth[idx] = z;
      buf.back[idx] = back;
      for (let c = 0; c < 3; c++) buf.alb[idx * 3 + c] = w0 * alb[0][c] + w1 * alb[1][c] + w2 * alb[2][c];
      buf.nx[idx] = w0 * ns[0][0] + w1 * ns[1][0] + w2 * ns[2][0];
      buf.ny[idx] = w0 * ns[0][1] + w1 * ns[1][1] + w2 * ns[2][1];
      buf.nz[idx] = w0 * ns[0][2] + w1 * ns[1][2] + w2 * ns[2][2];
    }
  }
}

// Screen-space occlusion. A pixel is darker when a nearby pixel is much closer to the camera.
function occlusion(depth, size, radius, range, bias) {
  const ao = new Float32Array(size * size).fill(1);
  const offsets = [];
  for (let k = 0; k < 8; k++) {
    const a = (k * Math.PI) / 4;
    offsets.push([Math.round(radius * Math.cos(a)), Math.round(radius * Math.sin(a))]);
  }
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const d = depth[y * size + x];
      if (d === Infinity) continue;
      let occluded = 0;
      for (const [dx, dy] of offsets) {
        const nx = x + dx;
        const ny = y + dy;
        if (nx < 0 || ny < 0 || nx >= size || ny >= size) continue;
        const nd = depth[ny * size + nx];
        if (nd === Infinity) continue;
        occluded += Math.min(1, Math.max(0, (d - nd - bias) / range));
      }
      ao[y * size + x] = 1 - (AO_STRENGTH * occluded) / offsets.length;
    }
  }
  return ao;
}

function clamp255(v) {
  return Math.max(0, Math.min(255, Math.round(v)));
}

// Background colour at one pixel of the size x size buffer.
function backgroundAt(x, y, size) {
  const t = Math.min(1, Math.hypot(x + 0.5 - size / 2, y + 0.5 - size / 2) / (size * Math.SQRT1_2));
  const s = t * t * (3 - 2 * t); // smoothstep: flat near the centre, eased toward the corners
  return [0, 1, 2].map((c) => Math.round(BG_CENTRE[c] + (BG_EDGE[c] - BG_CENTRE[c]) * s));
}

// 1 where a pixel borders a much nearer or farther surface, or the background.
function edgeMask(depth, size, step) {
  const edge = new Float32Array(size * size);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const d = depth[y * size + x];
      if (d === Infinity) continue;
      for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        const nx = x + dx;
        const ny = y + dy;
        if (nx < 0 || ny < 0 || nx >= size || ny >= size) continue;
        const nd = depth[ny * size + nx];
        if (nd === Infinity || Math.abs(d - nd) > step) {
          edge[y * size + x] = 1;
          break;
        }
      }
    }
  }
  return edge;
}

// Colour of one lit pixel. n is the unit normal in camera space, z toward the camera.
function litColour(n, upCam, ao, edge, albedo) {
  const hemi = 0.5 + 0.5 * dot(n, upCam);
  const light = [0, 1, 2].map((c) => SKY_LIGHT[c] * hemi + GROUND_LIGHT[c] * (1 - hemi));
  let spec = 0;
  for (const l of LIGHTS) {
    const ndl = Math.max(0, dot(n, l.dir));
    for (let c = 0; c < 3; c++) light[c] += l.colour[c] * l.gain * ndl;
    spec += l.gain * Math.pow(Math.max(0, dot(n, l.half)), SPEC_POWER);
  }
  const rim = RIM_GAIN * Math.pow(1 - Math.max(0, n[2]), RIM_POWER);
  return [0, 1, 2].map((c) => {
    const linear = (albedo[c] * EXPOSURE * light[c] + SPEC_GAIN * spec + RIM[c] * rim) * ao;
    const toned = linear / (1 + linear); // 0 to 1; bright faces roll off instead of clipping
    return clamp255(255 * Math.pow(toned * (1 - EDGE_STRENGTH * edge), 1 / 2.2));
  });
}

function renderView(tris, normals, fwd, up, size, frame, cutDepth, bounds) {
  const toCam = [-fwd[0], -fwd[1], -fwd[2]];
  const [right, trueUp] = basis(fwd, up);
  const b = screenBounds(tris, right, trueUp);
  if (frame) {
    const f = screenBounds(frame, right, trueUp);
    b.minX = Math.min(b.minX, f.minX);
    b.maxX = Math.max(b.maxX, f.maxX);
    b.minY = Math.min(b.minY, f.minY);
    b.maxY = Math.max(b.maxY, f.maxY);
  }
  const extent = Math.max(b.maxX - b.minX, b.maxY - b.minY);
  const scale = (MARGIN * size) / extent;
  const cx = (b.minX + b.maxX) / 2;
  const cy = (b.minY + b.maxY) / 2;

  const solid = cutDepth === null ? { tris, normals } : clipFar(tris, normals, fwd, cutDepth);
  const pixels = size * size;
  const buf = {
    depth: new Float64Array(pixels).fill(Infinity),
    back: new Uint8Array(pixels),
    nx: new Float32Array(pixels),
    ny: new Float32Array(pixels),
    nz: new Float32Array(pixels),
    alb: new Float32Array(pixels * 3),
  };
  for (let f = 0; f < solid.tris.length / 9; f++) {
    const v = [0, 1, 2].map((k) => [solid.tris[f * 9 + k * 3], solid.tris[f * 9 + k * 3 + 1], solid.tris[f * 9 + k * 3 + 2]]);
    const n = [0, 1, 2].map((k) => [solid.normals[f * 9 + k * 3], solid.normals[f * 9 + k * 3 + 1], solid.normals[f * 9 + k * 3 + 2]]);
    const faceNormal = normalize(cross(sub(v[1], v[0]), sub(v[2], v[0])));
    const back = dot(faceNormal, fwd) > 0 ? 1 : 0;
    fillTriangle(
      buf,
      size,
      v.map((p) => (dot(p, right) - cx) * scale + size / 2),
      v.map((p) => size / 2 - (dot(p, trueUp) - cy) * scale),
      v.map((p) => dot(p, fwd)), // smaller is nearer the camera
      n.map((q) => [dot(q, right), dot(q, trueUp), dot(q, toCam)]),
      v.map((p) => albedoAt(p, bounds)),
      back,
    );
  }

  const ao = occlusion(
    buf.depth,
    size,
    Math.max(2, Math.round(AO_RADIUS * size)),
    AO_RANGE * extent,
    AO_BIAS * extent,
  );
  const edge = edgeMask(buf.depth, size, EDGE_STEP * extent);
  const upCam = [dot(W_UP, right), dot(W_UP, trueUp), dot(W_UP, toCam)];
  const rgb = new Uint8Array(pixels * 3);
  for (let i = 0; i < pixels; i++) {
    const o = i * 3;
    if (buf.depth[i] === Infinity) {
      rgb.set(backgroundAt(i % size, Math.floor(i / size), size), o);
    } else if (cutDepth !== null && buf.back[i]) {
      rgb.set(CUT_FACE, o);
    } else {
      let n = normalize([buf.nx[i], buf.ny[i], buf.nz[i]]);
      if (n[2] < 0) n = [-n[0], -n[1], -n[2]]; // face the camera
      const albedo = [buf.alb[o], buf.alb[o + 1], buf.alb[o + 2]];
      rgb.set(litColour(n, upCam, ao[i], edge[i], albedo), o);
    }
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
  orientOutward(tris);
  const frame = frameFile ? readBinarySTL(readFileSync(frameFile)) : null;
  const normals = cornerNormals(tris);
  // Bounds cover both files, so before and after share one colour mapping and one cut.
  const bounds = boundsOf(frame ? [tris, frame] : [tris]);
  // The cut runs through the centre of the frame, across the iso view.
  const isoFwd = normalize(VIEWS.iso[0]);
  const cutDepth = dot(centreOf(bounds), isoFwd);
  mkdirSync(outDir, { recursive: true });
  const stem = basename(file, extname(file));
  const jobs = [];
  for (const [name, [fwd, up]] of Object.entries(VIEWS)) {
    jobs.push([name, fwd, up, null]);
    if (name === "iso") jobs.push(["iso-cutaway", fwd, up, cutDepth]);
  }
  for (const [name, fwd, up, cut] of jobs) {
    const big = renderView(tris, normals, normalize(fwd), up, size * SUPERSAMPLE, frame, cut, bounds);
    const rgb = downsample(big, size);
    const path = join(outDir, `${stem}-${name}.png`);
    writeFileSync(path, encodePNG(size, size, rgb));
    console.log(`wrote ${path}`);
  }
}

main();
