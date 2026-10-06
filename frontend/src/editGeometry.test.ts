import { describe, expect, it } from 'vitest';
import { cropFromDrag, defaultCrop, flipCrop, moveCrop, orientedSize, rotateCrop } from './editGeometry';

describe('edit geometry', () => {
  it('centres aspect presets inside the frame', () => {
    const square = defaultCrop('1:1', 1600, 900);
    expect(square.h).toBeCloseTo(1);
    expect(square.w * 1600).toBeCloseTo(900);
    expect(square.x).toBeCloseTo((1 - square.w) / 2);
    const wide = defaultCrop('16:9', 900, 1600);
    expect(wide.w).toBeCloseTo(1);
    expect((wide.w * 900) / (wide.h * 1600)).toBeCloseTo(16 / 9);
    expect(defaultCrop('original', 400, 300)).toEqual({ x: 0, y: 0, w: 1, h: 1 });
  });
  it('constrains a drag to the aspect and the frame in every direction', () => {
    for (const [px, py] of [[0.9, 0.95], [0.05, 0.02], [0.95, 0.1], [0.1, 0.9]]) {
      const c = cropFromDrag(0.5, 0.5, px, py, '3:2', 1200, 800);
      expect((c.w * 1200) / (c.h * 800)).toBeCloseTo(1.5, 5);
      expect(c.x).toBeGreaterThanOrEqual(-1e-9); expect(c.y).toBeGreaterThanOrEqual(-1e-9);
      expect(c.x + c.w).toBeLessThanOrEqual(1 + 1e-9); expect(c.y + c.h).toBeLessThanOrEqual(1 + 1e-9);
    }
    expect(cropFromDrag(0.2, 0.3, 0.7, 0.4, 'free', 100, 100)).toEqual({ x: 0.2, y: 0.3, w: expect.closeTo(0.5), h: expect.closeTo(0.1) });
  });
  it('moves within bounds', () => {
    expect(moveCrop({ x: 0.5, y: 0.5, w: 0.4, h: 0.4 }, 0.5, -0.9)).toEqual({ x: 0.6, y: 0, w: 0.4, h: 0.4 });
  });
  it('keeps a crop over the same pixels through four rotations and double flips', () => {
    const crop = { x: 0.1, y: 0.2, w: 0.3, h: 0.4 };
    let c = crop;
    for (let i = 0; i < 4; i++) c = rotateCrop(c, true)!;
    for (const k of ['x', 'y', 'w', 'h'] as const) expect(c[k]).toBeCloseTo(crop[k]);
    const once = rotateCrop(crop, true)!;
    expect(once).toEqual({ x: expect.closeTo(0.4), y: 0.1, w: 0.4, h: 0.3 });
    const back = rotateCrop(once, false)!;
    for (const k of ['x', 'y', 'w', 'h'] as const) expect(back[k]).toBeCloseTo(crop[k]);
    expect(flipCrop(flipCrop(crop, true), true)!.x).toBeCloseTo(crop.x);
    expect(orientedSize(400, 300, 90)).toEqual([300, 400]);
  });
});
