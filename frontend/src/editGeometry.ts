/** Pure geometry for the edit tools (unit tested). All crops are fractions of the oriented frame. */
export interface Crop { x: number; y: number; w: number; h: number }
export interface Edit {
  rotation: number; flip_h: boolean; flip_v: boolean; crop: Crop | null;
  rating: number; label: string | null; flag: 'pick' | 'reject' | null;
}
export const EMPTY_EDIT: Edit = { rotation: 0, flip_h: false, flip_v: false, crop: null, rating: 0, label: null, flag: null };
export const ASPECTS = ['free', 'original', '1:1', '4:3', '3:2', '16:9', '3:4', '2:3', '9:16'] as const;
export type Aspect = (typeof ASPECTS)[number];
export const LABELS = ['Red', 'Yellow', 'Green', 'Blue', 'Purple'] as const;

/** Size of the frame after rotation. */
export function orientedSize(width: number, height: number, rotation: number): [number, number] {
  return rotation % 180 === 0 ? [width, height] : [height, width];
}

/** Width / height the crop must have in pixels, or null for a free crop. */
export function aspectRatio(aspect: Aspect, frameW: number, frameH: number): number | null {
  if (aspect === 'free') return null;
  if (aspect === 'original') return frameW / frameH;
  const [a, b] = aspect.split(':').map(Number);
  return a / b;
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** Largest centred crop with the given aspect. */
export function defaultCrop(aspect: Aspect, frameW: number, frameH: number): Crop {
  const ratio = aspectRatio(aspect, frameW, frameH);
  if (ratio === null) return { x: 0.1, y: 0.1, w: 0.8, h: 0.8 };
  const frame = frameW / frameH;
  const w = ratio >= frame ? 1 : ratio / frame;
  const h = ratio >= frame ? frame / ratio : 1;
  return { x: (1 - w) / 2, y: (1 - h) / 2, w, h };
}

/** Rectangle from a drag (anchor -> point, both fractions), constrained to the frame and aspect. */
export function cropFromDrag(ax: number, ay: number, px: number, py: number, aspect: Aspect, frameW: number, frameH: number): Crop {
  ax = clamp(ax, 0, 1); ay = clamp(ay, 0, 1); px = clamp(px, 0, 1); py = clamp(py, 0, 1);
  let w = Math.abs(px - ax), h = Math.abs(py - ay);
  const ratio = aspectRatio(aspect, frameW, frameH);
  if (ratio !== null) {
    const frac = ratio * (frameH / frameW); // w/h in fraction space
    if (w / Math.max(h, 1e-9) > frac) w = h * frac; else h = w / frac;
    const maxW = px >= ax ? 1 - ax : ax, maxH = py >= ay ? 1 - ay : ay;
    const scale = Math.min(1, maxW / Math.max(w, 1e-9), maxH / Math.max(h, 1e-9));
    w *= scale; h *= scale;
  }
  const x = px >= ax ? ax : ax - w, y = py >= ay ? ay : ay - h;
  return { x, y, w, h };
}

export function moveCrop(crop: Crop, dx: number, dy: number): Crop {
  return { ...crop, x: clamp(crop.x + dx, 0, 1 - crop.w), y: clamp(crop.y + dy, 0, 1 - crop.h) };
}

export const isUsableCrop = (crop: Crop | null) => !!crop && crop.w >= 0.02 && crop.h >= 0.02;

/** Rotating the frame keeps an existing crop over the same pixels. */
export function rotateCrop(crop: Crop | null, clockwise: boolean): Crop | null {
  if (!crop) return null;
  return clockwise
    ? { x: 1 - crop.y - crop.h, y: crop.x, w: crop.h, h: crop.w }
    : { x: crop.y, y: 1 - crop.x - crop.w, w: crop.h, h: crop.w };
}

export function flipCrop(crop: Crop | null, horizontal: boolean): Crop | null {
  if (!crop) return null;
  return horizontal ? { ...crop, x: 1 - crop.x - crop.w } : { ...crop, y: 1 - crop.y - crop.h };
}

export const hasGeometry = (e: Edit) => !!(e.rotation || e.flip_h || e.flip_v || e.crop);
