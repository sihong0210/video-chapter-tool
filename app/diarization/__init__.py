"""Optional local speaker diarization and transcript alignment."""

from app.diarization.alignment import (
    label_transcript_segments,
    speaker_for_interval,
    validate_speaker_turns,
)
from app.diarization.models import (
    DiarizationResult,
    LabeledTranscriptSegment,
    SpeakerTurn,
)
from app.diarization.audio import DiarizationAudioError, assemble_session_wav
from app.diarization.pipeline import (
    SessionDiarizationResult,
    analyze_session_speakers,
    cleanup_retained_chunks,
)
from app.diarization.provider import DiarizationError, SpeakerDiarizer
from app.diarization.pyannote_provider import (
    DEFAULT_PYANNOTE_MODEL,
    PyannoteSpeakerDiarizer,
)
from app.diarization.runtime import collect_diarization_runtime

__all__ = [
    "DEFAULT_PYANNOTE_MODEL",
    "DiarizationError",
    "DiarizationAudioError",
    "DiarizationResult",
    "LabeledTranscriptSegment",
    "PyannoteSpeakerDiarizer",
    "SpeakerDiarizer",
    "SpeakerTurn",
    "SessionDiarizationResult",
    "analyze_session_speakers",
    "assemble_session_wav",
    "cleanup_retained_chunks",
    "collect_diarization_runtime",
    "label_transcript_segments",
    "speaker_for_interval",
    "validate_speaker_turns",
]
