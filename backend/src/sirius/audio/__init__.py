"""Full-duplex hybrid voice & audio subsystem for Sirius."""

from sirius.audio.aec import AcousticEchoCanceller
from sirius.audio.gemini_live import GeminiLiveClient
from sirius.audio.io_stream import AudioIOStream
from sirius.audio.local_voice import LocalVoiceEngine
from sirius.audio.pipeline import VoicePipeline
from sirius.audio.vad import SileroVAD
from sirius.audio.wake_kill import LocalWakeAndKillDetector

__all__ = [
    "AcousticEchoCanceller",
    "AudioIOStream",
    "GeminiLiveClient",
    "LocalVoiceEngine",
    "LocalWakeAndKillDetector",
    "SileroVAD",
    "VoicePipeline",
]
