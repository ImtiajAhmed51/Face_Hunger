import { useEffect, useRef, useState, type PointerEvent } from "react";
import { mutate } from "../api";
import { useAction } from "../context";
import { refreshData } from "../hooks";
import { useT, type MessageKey } from "../i18n";
import {
  ASPECTS, LABELS, cropFromDrag, defaultCrop, flipCrop, isUsableCrop, moveCrop, orientedSize, rotateCrop,
  type Aspect, type Crop, type Edit,
} from "../editGeometry";
import { Icon } from "./Icon";

export interface EditDetail {
  edit: Edit; version: number; edited: boolean; sidecar: string | null;
  history: { id: number; at: string; source: string; summary: string }[];
}

/**
 * Draws the photo with rotation/flip applied on a canvas. While cropping it dims everything
 * outside the draft rectangle and lets the pointer draw or move it; otherwise it shows only
 * the cropped region. The original file and its preview are never changed.
 */
export function EditCanvas({ src, alt, edit, cropping, draft, aspect, onDraft }: {
  src: string; alt: string; edit: Edit; cropping: boolean; draft: Crop | null; aspect: Aspect; onDraft: (crop: Crop) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [image, setImage] = useState<HTMLImageElement | null>(null);
  const drag = useRef<{ mode: "draw" | "move"; ax: number; ay: number; start: Crop | null } | null>(null);

  useEffect(() => {
    const img = new Image();
    img.onload = () => setImage(img);
    img.src = src;
    return () => { img.onload = null; };
  }, [src]);

  const [fw, fh] = image ? orientedSize(image.naturalWidth, image.naturalHeight, edit.rotation) : [4, 3];
  const shown = cropping ? null : edit.crop;

  useEffect(() => {
    const node = canvas.current;
    if (!node || !image) return;
    const region = shown ?? { x: 0, y: 0, w: 1, h: 1 };
    node.width = Math.max(1, Math.round(fw * region.w));
    node.height = Math.max(1, Math.round(fh * region.h));
    const ctx = node.getContext("2d");
    if (!ctx) return;
    ctx.save();
    ctx.translate(-region.x * fw, -region.y * fh);
    ctx.translate(fw / 2, fh / 2);
    ctx.scale(edit.flip_h ? -1 : 1, edit.flip_v ? -1 : 1);
    ctx.rotate((edit.rotation * Math.PI) / 180);
    ctx.drawImage(image, -image.naturalWidth / 2, -image.naturalHeight / 2);
    ctx.restore();
    if (cropping && draft) {
      ctx.fillStyle = "rgba(0,0,0,0.55)";
      const [x, y, w, h] = [draft.x * fw, draft.y * fh, draft.w * fw, draft.h * fh];
      ctx.fillRect(0, 0, fw, y); ctx.fillRect(0, y + h, fw, fh - y - h);
      ctx.fillRect(0, y, x, h); ctx.fillRect(x + w, y, fw - x - w, h);
      ctx.strokeStyle = "#fff"; ctx.lineWidth = Math.max(2, fw / 400);
      ctx.strokeRect(x, y, w, h);
      ctx.lineWidth = Math.max(1, fw / 900);
      for (const f of [1 / 3, 2 / 3]) { // rule of thirds
        ctx.beginPath(); ctx.moveTo(x + w * f, y); ctx.lineTo(x + w * f, y + h); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(x, y + h * f); ctx.lineTo(x + w, y + h * f); ctx.stroke();
      }
    }
  }, [image, edit.rotation, edit.flip_h, edit.flip_v, shown, cropping, draft, fw, fh]);

  const point = (e: PointerEvent) => {
    const box = canvas.current!.getBoundingClientRect();
    return [(e.clientX - box.left) / box.width, (e.clientY - box.top) / box.height] as const;
  };
  const inside = (c: Crop | null, x: number, y: number) => !!c && x > c.x && x < c.x + c.w && y > c.y && y < c.y + c.h;

  return (
    <canvas ref={canvas} className={`edit-canvas ${cropping ? "cropping" : ""}`} role="img" aria-label={alt}
      onPointerDown={(e) => {
        if (!cropping) return;
        const [x, y] = point(e);
        drag.current = inside(draft, x, y) ? { mode: "move", ax: x, ay: y, start: draft } : { mode: "draw", ax: x, ay: y, start: null };
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        const d = drag.current;
        if (!d) return;
        const [x, y] = point(e);
        onDraft(d.mode === "move" && d.start ? moveCrop(d.start, x - d.ax, y - d.ay) : cropFromDrag(d.ax, d.ay, x, y, aspect, fw, fh));
      }}
      onPointerUp={() => { drag.current = null; }} />
  );
}

/** Rating, labels, flags, rotate/flip, crop with aspect presets, history and revert. */
export function EditPanel({ mediaId, detail, cropping, draft, aspect, frame, onCropping, onDraft, onAspect }: {
  mediaId: number; detail: EditDetail; cropping: boolean; draft: Crop | null; aspect: Aspect; frame: [number, number];
  onCropping: (on: boolean) => void; onDraft: (crop: Crop | null) => void; onAspect: (aspect: Aspect) => void;
}) {
  const t = useT();
  const action = useAction();
  const edit = detail.edit;
  const save = (patch: Partial<Edit>) => void action.run(async () => { await mutate(`/media/${mediaId}/edits`, patch, "PATCH"); refreshData(); }, undefined, false);
  const revert = (historyId?: number) => void action.run(async () => {
    await mutate(`/media/${mediaId}/edits/revert`, historyId ? { history_id: historyId } : undefined);
    refreshData();
  }, undefined, false);
  const [fw, fh] = orientedSize(frame[0], frame[1], edit.rotation);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.target instanceof HTMLElement && e.target.closest("input,textarea,select,[contenteditable=true]")) return;
      if (/^[0-5]$/.test(e.key)) { e.preventDefault(); save({ rating: Number(e.key) }); }
      else if (e.key === "]") { e.preventDefault(); save({ rotation: (edit.rotation + 90) % 360, crop: rotateCrop(edit.crop, true) }); }
      else if (e.key === "[") { e.preventDefault(); save({ rotation: (edit.rotation + 270) % 360, crop: rotateCrop(edit.crop, false) }); }
      else if (e.key.toLowerCase() === "p") { e.preventDefault(); save({ flag: edit.flag === "pick" ? null : "pick" }); }
      else if (e.key.toLowerCase() === "x") { e.preventDefault(); save({ flag: edit.flag === "reject" ? null : "reject" }); }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  return (
    <section className="metadata-section edit-panel" aria-labelledby={`edit-${mediaId}`}>
      <h3 id={`edit-${mediaId}`}>{t("edit.title")}</h3>
      <div className="edit-row" role="radiogroup" aria-label={t("edit.rating")}>
        {[1, 2, 3, 4, 5].map((n) => (
          <button key={n} role="radio" aria-checked={edit.rating === n} className={`star ${edit.rating >= n ? "on" : ""}`}
            aria-label={t("edit.stars", { count: n })} onClick={() => save({ rating: edit.rating === n ? 0 : n })}>★</button>
        ))}
      </div>
      <div className="edit-row" role="radiogroup" aria-label={t("edit.label")}>
        {LABELS.map((name) => (
          <button key={name} role="radio" aria-checked={edit.label === name} className={`swatch swatch-${name.toLowerCase()}`}
            aria-label={t(`edit.label.${name}` as MessageKey)} title={t(`edit.label.${name}` as MessageKey)}
            onClick={() => save({ label: edit.label === name ? null : name })} />
        ))}
        <button className="button small" aria-pressed={edit.flag === "pick"} onClick={() => save({ flag: edit.flag === "pick" ? null : "pick" })}>{t("edit.pick")}</button>
        <button className="button small" aria-pressed={edit.flag === "reject"} onClick={() => save({ flag: edit.flag === "reject" ? null : "reject" })}>{t("edit.reject")}</button>
      </div>
      <div className="edit-row">
        <button className="button small" onClick={() => save({ rotation: (edit.rotation + 270) % 360, crop: rotateCrop(edit.crop, false) })}>{t("edit.rotateLeft")}</button>
        <button className="button small" onClick={() => save({ rotation: (edit.rotation + 90) % 360, crop: rotateCrop(edit.crop, true) })}>{t("edit.rotateRight")}</button>
        <button className="button small" aria-pressed={edit.flip_h} onClick={() => save({ flip_h: !edit.flip_h, crop: flipCrop(edit.crop, true) })}>{t("edit.flipH")}</button>
        <button className="button small" aria-pressed={edit.flip_v} onClick={() => save({ flip_v: !edit.flip_v, crop: flipCrop(edit.crop, false) })}>{t("edit.flipV")}</button>
      </div>
      <div className="edit-row">
        {!cropping ? (
          <>
            <button className="button small" onClick={() => { onDraft(edit.crop ?? defaultCrop(aspect, fw, fh)); onCropping(true); }}>
              <Icon name="edit" size={16} />{t("edit.crop")}
            </button>
            {edit.crop && <button className="button small" onClick={() => save({ crop: null })}>{t("edit.removeCrop")}</button>}
          </>
        ) : (
          <>
            <label className="field-inline">{t("edit.aspect")}
              <select value={aspect} onChange={(e) => { const a = e.target.value as Aspect; onAspect(a); onDraft(defaultCrop(a, fw, fh)); }}>
                {ASPECTS.map((a) => <option key={a} value={a}>{a === "free" ? t("edit.aspectFree") : a === "original" ? t("edit.aspectOriginal") : a}</option>)}
              </select>
            </label>
            <button className="button small primary" disabled={!isUsableCrop(draft)} onClick={() => { if (draft) save({ crop: draft }); onCropping(false); }}>{t("edit.applyCrop")}</button>
            <button className="button small" onClick={() => onCropping(false)}>{t("edit.cancel")}</button>
          </>
        )}
      </div>
      {cropping && <p className="small-text muted">{t("edit.cropHelp")}</p>}
      <p className="small-text muted">{t("edit.keys")}</p>
      {detail.history.length > 0 && (
        <details className="edit-history">
          <summary>{t("edit.history", { count: detail.history.length })}</summary>
          <ul>
            {detail.history.slice(0, 20).map((h) => (
              <li key={h.id}>
                <span>{h.summary}{h.source === "external" ? ` · ${t("edit.external")}` : ""}</span>
                <button className="text-link" disabled={action.busy} onClick={() => revert(h.id)}>{t("edit.undoStep")}</button>
              </li>
            ))}
          </ul>
        </details>
      )}
      {(detail.edited || edit.rating > 0 || edit.label || edit.flag) && (
        <button className="button small" disabled={action.busy} onClick={() => { onCropping(false); revert(); }}>
          <Icon name="restore" size={16} />{t("edit.revert")}
        </button>
      )}
      <p className="small-text muted">{t("edit.safe")}</p>
    </section>
  );
}
