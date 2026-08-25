from __future__ import annotations

import ctypes
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from app.transcription.streaming import StreamProgress, StreamingTranscriptResult


MIB = 1024 * 1024
MEMORY_GATE_MINIMUM_SECONDS = 30 * 60
MAX_MEMORY_GROWTH_MIB_PER_HOUR = 128.0


@dataclass(frozen=True, slots=True)
class MemorySample:
    elapsed_seconds: float
    rss_bytes: int

    def to_dict(self) -> dict[str, float]:
        return {
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "rss_mib": round(self.rss_bytes / MIB, 3),
        }


@dataclass(frozen=True, slots=True)
class ProgressSample:
    elapsed_seconds: float
    chunks_produced: int
    chunks_processed: int
    queued_chunks: int
    estimated_backlog_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _ProcessMemoryCountersEx(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def _read_process_rss_bytes() -> int | None:
    if os.name != "nt":
        return None
    counters = _ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ProcessMemoryCountersEx),
            ctypes.c_ulong,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        succeeded = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        )
    except (AttributeError, OSError):
        return None
    return int(counters.WorkingSetSize) if succeeded else None


class StabilityObserver:
    """Collect bounded, transcript-free memory and queue samples."""

    def __init__(
        self,
        *,
        sample_interval_seconds: float = 5.0,
        memory_reader: Callable[[], int | None] = _read_process_rss_bytes,
    ) -> None:
        if sample_interval_seconds <= 0:
            raise ValueError("記憶體取樣間隔必須大於 0。")
        self.sample_interval_seconds = sample_interval_seconds
        self._memory_reader = memory_reader
        self._started = 0.0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._memory_samples: list[MemorySample] = []
        self._progress_samples: list[ProgressSample] = []

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("穩定性監控已啟動。")
        self._started = time.monotonic()
        self._sample_memory()
        self._thread = threading.Thread(
            target=self._sample_loop,
            name="stability-memory-sampler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=max(2.0, self.sample_interval_seconds + 1.0))
        self._sample_memory()
        self._thread = None

    def record_progress(self, progress: StreamProgress) -> None:
        elapsed = max(0.0, time.monotonic() - self._started)
        sample = ProgressSample(
            elapsed_seconds=round(elapsed, 3),
            chunks_produced=progress.chunks_produced,
            chunks_processed=progress.chunks_processed,
            queued_chunks=progress.queued_chunks,
            estimated_backlog_seconds=progress.estimated_backlog_seconds,
        )
        with self._lock:
            self._progress_samples.append(sample)

    @property
    def memory_samples(self) -> tuple[MemorySample, ...]:
        with self._lock:
            return tuple(self._memory_samples)

    @property
    def progress_samples(self) -> tuple[ProgressSample, ...]:
        with self._lock:
            return tuple(self._progress_samples)

    def _sample_loop(self) -> None:
        while not self._stop_event.wait(self.sample_interval_seconds):
            self._sample_memory()

    def _sample_memory(self) -> None:
        rss_bytes = self._memory_reader()
        if rss_bytes is None or rss_bytes < 0:
            return
        elapsed = max(0.0, time.monotonic() - self._started)
        sample = MemorySample(round(elapsed, 3), int(rss_bytes))
        with self._lock:
            self._memory_samples.append(sample)


def _linear_growth_mib_per_hour(samples: Iterable[MemorySample]) -> float | None:
    values = tuple(samples)
    if len(values) < 3:
        return None
    xs = [sample.elapsed_seconds for sample in values]
    ys = [sample.rss_bytes / MIB for sample in values]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denominator = sum((value - mean_x) ** 2 for value in xs)
    if denominator <= 0:
        return None
    slope_per_second = sum(
        (x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)
    ) / denominator
    return round(slope_per_second * 3600, 3)


def _memory_summary(
    samples: tuple[MemorySample, ...],
    captured_duration_seconds: float,
) -> tuple[dict[str, Any], dict[str, str]]:
    if not samples:
        return (
            {"samples": [], "sample_count": 0},
            {
                "status": "inconclusive",
                "detail": "目前平台無法取得程序 RAM。",
            },
        )
    warmup_seconds = min(300.0, captured_duration_seconds * 0.1)
    post_warmup = tuple(
        sample for sample in samples if sample.elapsed_seconds >= warmup_seconds
    )
    slope = _linear_growth_mib_per_hour(post_warmup)
    summary = {
        "sample_interval_seconds": (
            round(samples[1].elapsed_seconds - samples[0].elapsed_seconds, 3)
            if len(samples) > 1
            else None
        ),
        "sample_count": len(samples),
        "start_rss_mib": round(samples[0].rss_bytes / MIB, 3),
        "end_rss_mib": round(samples[-1].rss_bytes / MIB, 3),
        "peak_rss_mib": round(max(sample.rss_bytes for sample in samples) / MIB, 3),
        "warmup_seconds": round(warmup_seconds, 3),
        "post_warmup_growth_mib_per_hour": slope,
        "samples": [sample.to_dict() for sample in samples],
    }
    if captured_duration_seconds < MEMORY_GATE_MINIMUM_SECONDS or slope is None:
        check = {
            "status": "inconclusive",
            "detail": "需至少 30 分鐘樣本才能判定 RAM 是否持續成長。",
        }
    elif slope <= MAX_MEMORY_GROWTH_MIB_PER_HOUR:
        check = {
            "status": "pass",
            "detail": f"暖機後趨勢 {slope:.3f} MiB/小時。",
        }
    else:
        check = {
            "status": "fail",
            "detail": (
                f"暖機後趨勢 {slope:.3f} MiB/小時，超過 "
                f"{MAX_MEMORY_GROWTH_MIB_PER_HOUR:.0f} MiB/小時。"
            ),
        }
    return summary, check


def _timestamp_counts(transcript: StreamingTranscriptResult) -> dict[str, int]:
    invalid_ranges = 0
    nonmonotonic_starts = 0
    adjacent_overlaps = 0
    outside_capture = 0
    previous_start = -1.0
    previous_end = -1.0
    for segment in transcript.segments:
        if segment.start < 0 or segment.end < segment.start:
            invalid_ranges += 1
        if segment.start < previous_start:
            nonmonotonic_starts += 1
        if segment.start < previous_end:
            adjacent_overlaps += 1
        if segment.end > transcript.capture.captured_duration_seconds + 0.05:
            outside_capture += 1
        previous_start = segment.start
        previous_end = max(previous_end, segment.end)
    return {
        "invalid_ranges": invalid_ranges,
        "nonmonotonic_starts": nonmonotonic_starts,
        "adjacent_overlaps": adjacent_overlaps,
        "outside_capture": outside_capture,
    }


def build_stability_report(
    *,
    session_id: int,
    transcript: StreamingTranscriptResult,
    observer: StabilityObserver,
    pending_audio_files: int,
    cleanup_expected: bool = True,
) -> dict[str, Any]:
    capture = transcript.capture
    source_coverage = (
        capture.source_frames_received / capture.frames_captured
        if capture.frames_captured > 0
        else 0.0
    )
    timestamps = _timestamp_counts(transcript)
    memory, memory_check = _memory_summary(
        observer.memory_samples,
        capture.captured_duration_seconds,
    )
    progress = observer.progress_samples
    final_queue = progress[-1].queued_chunks if progress else 0
    stopped_early = capture.cancelled_by_user or capture.stopped_by_request
    if capture.captured_duration_seconds < 60:
        coverage_check = {
            "status": "inconclusive",
            "detail": (
                f"來源 Frame 覆蓋率 {source_coverage * 100:.4f}%；"
                "短於 60 秒，可能受 WASAPI 啟停緩衝影響。"
            ),
        }
    else:
        coverage_check = {
            "status": "pass" if source_coverage >= 0.99 else "fail",
            "detail": f"來源 Frame 覆蓋率 {source_coverage * 100:.4f}%。",
        }
    checks: dict[str, dict[str, str]] = {
        "capture_duration": {
            "status": (
                "inconclusive"
                if stopped_early
                else (
                    "pass"
                    if abs(
                        capture.captured_duration_seconds
                        - capture.requested_duration_seconds
                    )
                    <= max(0.25, capture.requested_duration_seconds * 0.001)
                    else "fail"
                )
            ),
            "detail": (
                f"要求 {capture.requested_duration_seconds:.3f} 秒，"
                f"取得 {capture.captured_duration_seconds:.3f} 秒。"
                + ("使用者提前停止。" if stopped_early else "")
            ),
        },
        "source_audio_coverage": coverage_check,
        "stream_status_events": {
            "status": "pass" if capture.stream_status_event_count == 0 else "fail",
            "detail": f"音訊狀態事件 {capture.stream_status_event_count} 次。",
        },
        "queue_drained": {
            "status": (
                "pass"
                if transcript.chunks_processed == capture.chunks_produced
                and final_queue == 0
                else "fail"
            ),
            "detail": (
                f"產生 {capture.chunks_produced}、完成 "
                f"{transcript.chunks_processed}、結束待處理 {final_queue}。"
            ),
        },
        "timestamps": {
            "status": "pass" if not any(timestamps.values()) else "fail",
            "detail": str(timestamps),
        },
        "pending_audio_cleanup": {
            "status": (
                "pass"
                if pending_audio_files == 0
                else ("fail" if cleanup_expected else "inconclusive")
            ),
            "detail": (
                f"尚存 WAV {pending_audio_files} 個。"
                + ("" if cleanup_expected else "此流程允許保留 Chunk。")
            ),
        },
        "memory_growth": memory_check,
    }
    failed = any(check["status"] == "fail" for check in checks.values())
    inconclusive = any(
        check["status"] == "inconclusive" for check in checks.values()
    )
    if failed:
        outcome = "fail"
    elif inconclusive:
        outcome = "provisional_pass"
    else:
        outcome = "pass"
    gate_duration = (
        capture.captured_duration_seconds
        if stopped_early
        else capture.requested_duration_seconds
    )
    if gate_duration >= 10_800:
        gate_level = "release_3_hour_or_longer"
    elif gate_duration >= 3_600:
        gate_level = "candidate_1_hour"
    elif gate_duration >= 1_800:
        gate_level = "daily_30_minute"
    else:
        gate_level = "short_baseline"
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "outcome": outcome,
        "gate_level": gate_level,
        "privacy": {"contains_transcript": False, "contains_audio": False},
        "runtime": transcript.runtime,
        "metrics": {
            "requested_duration_seconds": capture.requested_duration_seconds,
            "captured_duration_seconds": capture.captured_duration_seconds,
            "session_wall_time_seconds": transcript.session_wall_time_seconds,
            "source_frames_received": capture.source_frames_received,
            "silence_frames_padded": capture.silence_frames_padded,
            "source_coverage_ratio": round(source_coverage, 8),
            "chunks_produced": capture.chunks_produced,
            "chunks_processed": transcript.chunks_processed,
            "segments": len(transcript.segments),
            "transcription_seconds": transcript.transcription_seconds,
            "peak_queued_chunks": transcript.peak_queued_chunks,
            "peak_estimated_backlog_seconds": (
                transcript.peak_estimated_backlog_seconds
            ),
            "pending_audio_files": pending_audio_files,
            "cancelled_by_user": capture.cancelled_by_user,
            "stopped_by_request": capture.stopped_by_request,
            "timestamps": timestamps,
        },
        "memory": memory,
        "progress_samples": [sample.to_dict() for sample in progress],
        "checks": checks,
    }


def build_failed_report(
    *,
    session_id: int,
    observer: StabilityObserver,
    error: BaseException,
) -> dict[str, Any]:
    samples = observer.memory_samples
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "outcome": "error",
        "privacy": {"contains_transcript": False, "contains_audio": False},
        "error": {
            "type": type(error).__name__,
            "message": " ".join(str(error).split())[:500],
        },
        "memory": {
            "sample_count": len(samples),
            "samples": [sample.to_dict() for sample in samples],
        },
        "progress_samples": [
            sample.to_dict() for sample in observer.progress_samples
        ],
    }
