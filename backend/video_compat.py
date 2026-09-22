"""Automatic lossless-first video conversion for browser playback.

Flow: probe → remux (stream copy) or lossless re-encode → verify →
finalize MP4 → soft-keep original (*.lfs_original) → update DB.
Original is never permanently deleted; only renamed aside after success.
"""

from __future__ import annotations

import json
import sys
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .media_processing import _ffmpeg_bin, _ffprobe_bin

log = logging.getLogger("video_compat")

# Browser-playable containers / codecs (no conversion needed)
_BROWSER_CONTAINERS = frozenset({"mp4", "mov", "m4v", "webm"})
_BROWSER_VIDEO = frozenset({"h264", "avc1", "avc", "vp8", "vp9", "av1", "av01"})
_BROWSER_AUDIO = frozenset({"aac", "mp3", "mp4a", "opus", "vorbis", "flac"})

# Stream-copy remux into MP4 when already H.264 (+ compatible audio)
_REMUX_VIDEO = frozenset({"h264", "avc1", "avc"})
_REMUX_AUDIO = frozenset({"aac", "mp3", "mp4a", "alac"})  # ac3 often fails in browsers

# Callback: (media_id, new_path, new_name, size, mtime_ns, width, height, duration) -> None
DbUpdateFn = Callable[[int, Path, str, int, int, Optional[int], Optional[int], Optional[float], Optional[Path]], None]


@dataclass
class ProbeInfo:
    format_name: str
    video_codec: Optional[str]
    audio_codec: Optional[str]
    width: Optional[int]
    height: Optional[int]
    duration: Optional[float]
    fps: Optional[float] = None
    color_primaries: Optional[str] = None
    color_transfer: Optional[str] = None
    color_space: Optional[str] = None
    color_range: Optional[str] = None
    pix_fmt: Optional[str] = None

    @property
    def primary_format(self) -> str:
        names = [n.strip().lower() for n in (self.format_name or "").split(",") if n.strip()]
        for preferred in (
            "mp4", "mov", "webm", "matroska", "avi", "asf", "flv",
            "mpeg", "mpegts", "rm", "3gp",
        ):
            if preferred in names:
                return preferred
        return names[0] if names else "unknown"

    def browser_compatible(self) -> bool:
        fmt = self.primary_format
        fmt_blob = (self.format_name or "").lower()
        if fmt not in _BROWSER_CONTAINERS and "mp4" not in fmt_blob:
            return False
        vc = (self.video_codec or "").lower()
        if not vc or vc not in _BROWSER_VIDEO:
            return False
        if vc in ("hevc", "h265", "hev1", "hvc1"):
            return False
        ac = (self.audio_codec or "").lower()
        if ac and ac not in _BROWSER_AUDIO:
            return False
        return True

    def can_remux_to_mp4(self) -> bool:
        vc = (self.video_codec or "").lower()
        ac = (self.audio_codec or "").lower()
        if vc not in _REMUX_VIDEO:
            return False
        if ac and ac not in _REMUX_AUDIO:
            return False
        return True


@dataclass
class ConversionState:
    status: str = "pending"  # pending|analyzing|converting|verifying|replacing|completed|failed|ready
    progress: float = 0.0  # 0–100
    stage: str = "Pending"
    error: Optional[str] = None
    ready: bool = False
    mode: Optional[str] = None  # remux | lossless | original
    indeterminate: bool = False


class VideoCompatService:
    """On-demand conversion with real FFmpeg progress, safe replace, per-media locks."""

    # Only one heavy FFmpeg job at a time — protects MacBook Air / shared machines.
    MAX_CONCURRENT = 1

    def __init__(self, work_dir: Path, on_replaced: Optional[DbUpdateFn] = None):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.on_replaced = on_replaced
        self._lock = threading.Lock()
        self._media_locks: dict[int, threading.Lock] = {}
        self._states: dict[int, ConversionState] = {}
        self._active: set[int] = set()  # media_ids with a worker thread
        self._running: set[int] = set()  # media_ids currently inside FFmpeg
        self._ffmpeg_slots = threading.Semaphore(self.MAX_CONCURRENT)
        self._cleanup_stale_temps()

    def _cleanup_stale_temps(self) -> None:
        """Remove incomplete temp outputs left after a crash."""
        try:
            for path in self.work_dir.glob("*.partial"):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            for path in self.work_dir.glob("*.converting.mp4"):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError:
            pass

    def _media_lock(self, media_id: int) -> threading.Lock:
        with self._lock:
            if media_id not in self._media_locks:
                self._media_locks[media_id] = threading.Lock()
            return self._media_locks[media_id]

    def _set_state(self, media_id: int, **kwargs) -> ConversionState:
        with self._lock:
            cur = self._states.get(media_id) or ConversionState()
            for k, v in kwargs.items():
                if hasattr(cur, k):
                    setattr(cur, k, v)
            self._states[media_id] = cur
            return cur

    def get_state(self, media_id: int) -> ConversionState:
        with self._lock:
            return self._states.get(media_id) or ConversionState()

    def status_response(self, media_id: int) -> dict:
        s = self.get_state(media_id)
        progress = round(float(s.progress), 1)
        if s.status in ("completed", "ready") or s.ready:
            return {
                "status": "completed",
                "progress": 100,
                "stage": s.stage or "Ready",
                "ready": True,
                "error": None,
                "mode": s.mode,
                "indeterminate": False,
            }
        if s.status == "failed":
            return {
                "status": "failed",
                "progress": 0,
                "stage": s.stage or "Conversion failed",
                "ready": False,
                "error": s.error or "Conversion failed",
                "mode": s.mode,
                "indeterminate": False,
            }
        return {
            "status": s.status,
            "progress": progress,
            "stage": s.stage,
            "ready": False,
            "error": s.error,
            "mode": s.mode,
            "indeterminate": bool(s.indeterminate),
        }

    def probe(self, path: Path) -> Optional[ProbeInfo]:
        ffprobe = _ffprobe_bin()
        if not ffprobe:
            return None
        try:
            proc = subprocess.run(
                [
                    ffprobe, "-v", "quiet", "-print_format", "json",
                    "-show_streams", "-show_format", str(path),
                ],
                capture_output=True, text=True, timeout=90, check=False,
            )
            if proc.returncode != 0 or not proc.stdout:
                return None
            data = json.loads(proc.stdout)
        except Exception as exc:
            log.warning("ffprobe failed for %s: %s", path, exc)
            return None

        fmt = (data.get("format") or {}).get("format_name") or ""
        duration = None
        try:
            duration = float((data.get("format") or {}).get("duration") or 0) or None
        except (TypeError, ValueError):
            pass

        video_codec = audio_codec = None
        width = height = None
        fps = None
        color_primaries = color_transfer = color_space = color_range = pix_fmt = None
        for stream in data.get("streams") or []:
            ctype = stream.get("codec_type")
            codec = (stream.get("codec_name") or "").lower()
            if ctype == "video" and video_codec is None:
                video_codec = codec
                try:
                    width = int(stream["width"]) if stream.get("width") else None
                    height = int(stream["height"]) if stream.get("height") else None
                except (TypeError, ValueError):
                    pass
                rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
                try:
                    num, den = rate.split("/")
                    val = float(num) / float(den) if float(den) else 0.0
                    if val > 0:
                        fps = val
                except Exception:
                    pass
                if duration is None and stream.get("duration"):
                    try:
                        duration = float(stream["duration"])
                    except (TypeError, ValueError):
                        pass
                color_primaries = stream.get("color_primaries") or None
                color_transfer = stream.get("color_transfer") or None
                color_space = stream.get("colorspace") or stream.get("color_space") or None
                color_range = stream.get("color_range") or None
                pix_fmt = stream.get("pix_fmt") or None
            elif ctype == "audio" and audio_codec is None:
                audio_codec = codec

        return ProbeInfo(
            format_name=fmt,
            video_codec=video_codec,
            audio_codec=audio_codec,
            width=width,
            height=height,
            duration=duration,
            fps=fps,
            color_primaries=color_primaries,
            color_transfer=color_transfer,
            color_space=color_space,
            color_range=color_range,
            pix_fmt=pix_fmt,
        )

    def needs_conversion(self, path: Path) -> tuple[bool, Optional[ProbeInfo]]:
        if not path.is_file():
            return True, None
        probe = self.probe(path)
        if probe is None:
            # Unknown — attempt conversion rather than fail early
            return True, None
        if probe.browser_compatible():
            return False, probe
        return True, probe

    def ensure_playable(
        self,
        media_id: int,
        source: Path,
        *,
        start_if_needed: bool = True,
    ) -> tuple[bool, ConversionState]:
        """Return (ready_to_serve_original_or_converted, state).

        If conversion is needed, starts a background job (unless already running).
        """
        if not source.is_file():
            state = self._set_state(
                media_id,
                status="failed",
                progress=0,
                stage="Source missing",
                error="Original file is missing on disk",
                ready=False,
            )
            return False, state

        existing = self.get_state(media_id)
        if existing.status in ("completed", "ready") and existing.ready:
            # Path may have been replaced — re-check file still exists
            if source.is_file() or Path(str(source).rsplit(".", 1)[0] + ".mp4").is_file():
                return True, existing

        with self._lock:
            if media_id in self._active:
                return False, self._states.get(media_id) or ConversionState(
                    status="converting", stage="Converting video", indeterminate=True
                )

        needs, probe = self.needs_conversion(source)
        if not needs:
            state = self._set_state(
                media_id,
                status="completed",
                progress=100,
                stage="Ready",
                error=None,
                ready=True,
                mode="original",
                indeterminate=False,
            )
            return True, state

        if not start_if_needed:
            state = self._set_state(
                media_id,
                status="pending",
                progress=0,
                stage="Pending conversion",
                ready=False,
            )
            return False, state

        ffmpeg = _ffmpeg_bin()
        if not ffmpeg:
            state = self._set_state(
                media_id,
                status="failed",
                progress=0,
                stage="Conversion failed",
                error="ffmpeg not found — install ffmpeg to convert this video",
                ready=False,
            )
            return False, state
        if not _ffprobe_bin():
            state = self._set_state(
                media_id,
                status="failed",
                progress=0,
                stage="Conversion failed",
                error="ffprobe not found — install ffmpeg (includes ffprobe)",
                ready=False,
            )
            return False, state

        with self._lock:
            if media_id in self._active:
                return False, self._states.get(media_id) or ConversionState(
                    status="converting", stage="Converting video"
                )
            self._active.add(media_id)
            others_running = len(self._running) > 0 or any(
                mid != media_id for mid in self._active
            )

        if others_running:
            self._set_state(
                media_id,
                status="pending",
                progress=0,
                stage="Queued — waiting for another conversion to finish",
                error=None,
                ready=False,
                indeterminate=True,
            )
        else:
            self._set_state(
                media_id,
                status="analyzing",
                progress=0,
                stage="Analyzing video",
                error=None,
                ready=False,
                indeterminate=True,
            )

        thread = threading.Thread(
            target=self._convert_worker,
            args=(media_id, source, probe),
            name=f"video-convert-{media_id}",
            daemon=True,
        )
        thread.start()
        return False, self.get_state(media_id)

    def _convert_worker(
        self,
        media_id: int,
        source: Path,
        probe: Optional[ProbeInfo],
    ) -> None:
        media_lock = self._media_lock(media_id)
        tmp: Optional[Path] = None
        slot_held = False
        try:
            # Global slot: only one FFmpeg at a time on this machine
            self._set_state(
                media_id,
                status="pending",
                progress=0,
                stage="Queued — waiting for another conversion to finish",
                indeterminate=True,
            )
            self._ffmpeg_slots.acquire()
            slot_held = True
            with self._lock:
                self._running.add(media_id)

            with media_lock:
                if not source.is_file():
                    self._set_state(
                        media_id,
                        status="failed",
                        progress=0,
                        stage="Conversion failed",
                        error="Source disappeared during conversion",
                        ready=False,
                    )
                    return

                # Re-probe under lock
                self._set_state(
                    media_id,
                    status="analyzing",
                    progress=2,
                    stage="Analyzing video",
                    indeterminate=True,
                )
                if probe is None:
                    probe = self.probe(source)

                if probe is not None and probe.browser_compatible():
                    self._set_state(
                        media_id,
                        status="completed",
                        progress=100,
                        stage="Ready",
                        ready=True,
                        mode="original",
                        indeterminate=False,
                    )
                    return

                remux = probe is not None and probe.can_remux_to_mp4()
                mode = "remux" if remux else "lossless"
                duration = probe.duration if probe else None

                # Temp next to source when possible (same filesystem → atomic replace)
                stem = source.stem
                parent = source.parent
                tmp = parent / f".{stem}.converting.{media_id}.mp4"
                try:
                    # Ensure we can write next to source
                    parent.mkdir(parents=True, exist_ok=True)
                    with open(tmp, "ab"):
                        pass
                    tmp.unlink(missing_ok=True)
                except OSError:
                    # Fall back to work dir
                    tmp = self.work_dir / f"{media_id}_{stem}.converting.mp4"

                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass

                self._set_state(
                    media_id,
                    status="converting",
                    progress=5,
                    stage="Converting video" if not remux else "Remuxing video",
                    mode=mode,
                    indeterminate=duration is None or duration <= 0,
                )

                ok, err = self._run_ffmpeg(
                    media_id, source, tmp, remux=remux, duration=duration, probe=probe
                )
                if not ok:
                    self._safe_unlink(tmp)
                    self._set_state(
                        media_id,
                        status="failed",
                        progress=0,
                        stage="Conversion failed",
                        error=err or "FFmpeg conversion failed",
                        ready=False,
                    )
                    log.error("Conversion failed media_id=%s: %s", media_id, err)
                    return

                # --- verify ---
                self._set_state(
                    media_id,
                    status="verifying",
                    progress=92,
                    stage="Verifying converted video",
                    indeterminate=True,
                )
                if not tmp.is_file() or tmp.stat().st_size <= 0:
                    self._safe_unlink(tmp)
                    self._set_state(
                        media_id,
                        status="failed",
                        progress=0,
                        stage="Conversion failed",
                        error="Converted file is empty or missing",
                        ready=False,
                    )
                    return

                out_probe = self.probe(tmp)
                if out_probe is None or not out_probe.video_codec:
                    self._safe_unlink(tmp)
                    self._set_state(
                        media_id,
                        status="failed",
                        progress=0,
                        stage="Conversion failed",
                        error="Converted file has no valid video stream",
                        ready=False,
                    )
                    return

                if duration and out_probe.duration:
                    # Allow small duration drift
                    if abs(out_probe.duration - duration) > max(2.0, duration * 0.15):
                        log.warning(
                            "Duration mismatch media_id=%s src=%.2f out=%.2f",
                            media_id, duration, out_probe.duration,
                        )

                # --- replace ---
                self._set_state(
                    media_id,
                    status="replacing",
                    progress=96,
                    stage="Finalizing video",
                    indeterminate=True,
                )

                final = source.with_suffix(".mp4")
                same_path = final.resolve() == source.resolve()
                soft_kept: Optional[Path] = None

                try:
                    if same_path:
                        # Source is already .mp4 (re-encode in place): soft-keep original first
                        soft = self._soft_delete_path(source, media_id)
                        try:
                            os.replace(str(source), str(soft))
                        except OSError as exc:
                            self._safe_unlink(tmp)
                            self._set_state(
                                media_id,
                                status="failed",
                                progress=0,
                                stage="Conversion failed",
                                error=f"Could not soft-keep original: {exc}",
                                ready=False,
                            )
                            return
                        os.replace(str(tmp), str(final))
                        tmp = None
                        soft_kept = soft
                        log.info(
                            "Soft-kept original media_id=%s at %s", media_id, soft
                        )
                    else:
                        os.replace(str(tmp), str(final))
                        tmp = None
                        # Soft-delete original (rename aside) — never permanent unlink
                        if source.resolve() != final.resolve() and source.is_file():
                            try:
                                soft = self._soft_delete_path(source, media_id)
                                os.replace(str(source), str(soft))
                                soft_kept = soft
                                log.info(
                                    "Soft-kept original media_id=%s at %s",
                                    media_id, soft,
                                )
                            except OSError as exc:
                                log.warning(
                                    "Could not soft-keep original %s: %s "
                                    "(MP4 is ready; original still at source if move failed)",
                                    source, exc,
                                )
                except OSError as exc:
                    self._safe_unlink(tmp)
                    self._set_state(
                        media_id,
                        status="failed",
                        progress=0,
                        stage="Conversion failed",
                        error=f"Could not finalize converted file: {exc}",
                        ready=False,
                    )
                    return

                if not final.is_file() or final.stat().st_size <= 0:
                    self._set_state(
                        media_id,
                        status="failed",
                        progress=0,
                        stage="Conversion failed",
                        error="Final MP4 missing after replace",
                        ready=False,
                    )
                    return

                st = final.stat()
                new_name = final.name
                width = out_probe.width
                height = out_probe.height
                out_dur = out_probe.duration

                if self.on_replaced:
                    try:
                        self.on_replaced(
                            media_id,
                            final,
                            new_name,
                            int(st.st_size),
                            int(st.st_mtime_ns),
                            width,
                            height,
                            out_dur,
                            soft_kept,
                        )
                    except Exception as exc:
                        log.exception("DB update after replace failed media_id=%s", media_id)
                        # File is already replaced — mark completed but note DB issue
                        self._set_state(
                            media_id,
                            status="completed",
                            progress=100,
                            stage="Ready",
                            ready=True,
                            mode=mode,
                            error=f"File replaced but database update failed: {exc}",
                            indeterminate=False,
                        )
                        return

                self._set_state(
                    media_id,
                    status="completed",
                    progress=100,
                    stage="Ready",
                    ready=True,
                    mode=mode,
                    error=None,
                    indeterminate=False,
                )
                log.info(
                    "Conversion complete media_id=%s mode=%s path=%s",
                    media_id, mode, final,
                )
        except Exception as exc:
            log.exception("Conversion worker error media_id=%s", media_id)
            self._safe_unlink(tmp)
            self._set_state(
                media_id,
                status="failed",
                progress=0,
                stage="Conversion failed",
                error=str(exc),
                ready=False,
            )
        finally:
            with self._lock:
                self._active.discard(media_id)
                self._running.discard(media_id)
            if slot_held:
                try:
                    self._ffmpeg_slots.release()
                except ValueError:
                    pass


    _vt_checked = False
    _vt_available = False

    def _pick_video_encoder(self, ffmpeg: str, probe: Optional[ProbeInfo] = None) -> list[str]:
        """Near-lossless browser H.264 — keep quality and color tone as close as possible.

        Remux (stream copy) is preferred when possible (bit-identical).
        When re-encode is required: CRF 14 / high VT quality, slow preset,
        and preserve source color primaries / transfer / matrix tags.
        Browser requires 8-bit yuv420p H.264; that is the only forced change.
        """
        if not VideoCompatService._vt_checked:
            VideoCompatService._vt_checked = True
            if sys.platform == "darwin":
                try:
                    proc = subprocess.run(
                        [ffmpeg, "-hide_banner", "-encoders"],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    out = (proc.stdout or "") + (proc.stderr or "")
                    VideoCompatService._vt_available = "h264_videotoolbox" in out
                except Exception:
                    VideoCompatService._vt_available = False
            log.info(
                "Video encoder: %s",
                "h264_videotoolbox (GPU, near-lossless)"
                if VideoCompatService._vt_available
                else "libx264 CRF 14 slow (CPU, near-lossless)",
            )

        color_flags: list[str] = []
        if probe is not None:
            for flag, val in (
                ("color_primaries", probe.color_primaries),
                ("color_trc", probe.color_transfer),
                ("colorspace", probe.color_space),
            ):
                if val and str(val).lower() not in ("unknown", "unspecified", "reserved"):
                    color_flags.extend([f"-{flag}", str(val)])
            if probe.color_range:
                cr = str(probe.color_range).lower()
                if cr in ("pc", "full"):
                    color_flags.extend(["-color_range", "pc"])
                elif cr in ("tv", "limited"):
                    color_flags.extend(["-color_range", "tv"])

        if VideoCompatService._vt_available:
            return [
                "-c:v", "h264_videotoolbox",
                "-q:v", "75",
                "-profile:v", "high",
                "-pix_fmt", "yuv420p",
                "-realtime", "false",
                "-allow_sw", "1",
                *color_flags,
            ]

        return [
            "-c:v", "libx264",
            "-crf", "14",
            "-preset", "slow",
            "-profile:v", "high",
            "-level", "5.1",
            "-pix_fmt", "yuv420p",
            "-x264-params", "aq-mode=3:ref=6:bframes=8:me=umh:subme=10:psy-rd=1.0:0.15",
            *color_flags,
        ]


    @staticmethod
    def _soft_delete_path(source: Path, media_id: int) -> Path:
        """Recoverable path for the original — never permanently deleted.

        Same folder: ``movie.wmv`` → ``movie.wmv.lfs_original``
        (scanner ignores ``*.lfs_original``).
        """
        candidate = Path(str(source) + ".lfs_original")
        if not candidate.exists():
            return candidate
        # Collision: include media id
        return Path(str(source) + f".{media_id}.lfs_original")

    @staticmethod
    def _safe_unlink(path: Optional[Path]) -> None:
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _run_ffmpeg(
        self,
        media_id: int,
        source: Path,
        dest: Path,
        *,
        remux: bool,
        duration: Optional[float],
        probe: Optional[ProbeInfo] = None,
    ) -> tuple[bool, Optional[str]]:
        ffmpeg = _ffmpeg_bin()
        if not ffmpeg:
            return False, "ffmpeg not found"

        if remux:
            # Bit-identical streams — only the container changes
            cmd = [
                ffmpeg, "-hide_banner", "-y", "-nostdin",
                "-i", str(source),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "copy",
                "-c:a", "copy",
                "-map_metadata", "0",
                "-movflags", "+faststart",
                "-f", "mp4",
                "-progress", "pipe:1",
                "-nostats",
                str(dest),
            ]
        else:
            vcodec = self._pick_video_encoder(ffmpeg, probe)
            ac = (probe.audio_codec or "").lower() if probe else ""
            if ac in ("aac", "mp3", "mp4a"):
                audio_args = ["-c:a", "copy"]
            else:
                audio_args = ["-c:a", "aac", "-b:a", "320k", "-ac", "2"]
            cmd = [
                ffmpeg, "-hide_banner", "-y", "-nostdin",
                "-i", str(source),
                "-map", "0:v:0", "-map", "0:a:0?",
                *vcodec,
                *audio_args,
                "-map_metadata", "0",
                "-movflags", "+faststart",
                "-f", "mp4",
                "-progress", "pipe:1",
                "-nostats",
                str(dest),
            ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            return False, str(exc)

        last_out_time = 0.0
        stderr_chunks: list[str] = []

        def _read_stderr():
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_chunks.append(line)
                if len(stderr_chunks) > 200:
                    stderr_chunks.pop(0)

        err_thread = threading.Thread(target=_read_stderr, daemon=True)
        err_thread.start()

        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("out_time_ms="):
                    try:
                        ms = int(line.split("=", 1)[1])
                        if ms > 0:
                            last_out_time = ms / 1_000_000.0
                    except ValueError:
                        pass
                elif line.startswith("out_time="):
                    # HH:MM:SS.microseconds
                    try:
                        t = line.split("=", 1)[1].strip()
                        parts = t.split(":")
                        if len(parts) == 3:
                            h, m, s = parts
                            last_out_time = int(h) * 3600 + int(m) * 60 + float(s)
                    except (ValueError, IndexError):
                        pass
                elif line.startswith("progress="):
                    if duration and duration > 0 and last_out_time >= 0:
                        pct = min(90.0, 5.0 + (last_out_time / duration) * 85.0)
                        self._set_state(
                            media_id,
                            status="converting",
                            progress=pct,
                            stage="Remuxing video" if remux else "Converting video",
                            indeterminate=False,
                        )
                    else:
                        self._set_state(
                            media_id,
                            status="converting",
                            stage="Remuxing video" if remux else "Converting video",
                            indeterminate=True,
                        )
                if line == "progress=end":
                    break

            proc.wait(timeout=7200)
        except subprocess.TimeoutExpired:
            proc.kill()
            return False, "ffmpeg timed out"
        except Exception as exc:
            try:
                proc.kill()
            except Exception:
                pass
            return False, str(exc)
        finally:
            try:
                err_thread.join(timeout=2)
            except Exception:
                pass

        if proc.returncode != 0:
            err = "".join(stderr_chunks).strip()
            lines = [ln for ln in err.splitlines() if ln.strip()]
            tail = "\n".join(lines[-25:]) if lines else f"exit {proc.returncode}"
            return False, tail

        return True, None


_service: Optional[VideoCompatService] = None


def init_video_compat(
    work_dir: Path,
    on_replaced: Optional[DbUpdateFn] = None,
) -> VideoCompatService:
    global _service
    _service = VideoCompatService(work_dir, on_replaced=on_replaced)
    return _service


def get_video_compat() -> VideoCompatService:
    if _service is None:
        raise RuntimeError("VideoCompatService not initialized")
    return _service
