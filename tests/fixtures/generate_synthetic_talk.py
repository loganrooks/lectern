from __future__ import annotations

import math
import wave
from array import array
from pathlib import Path

SAMPLE_RATE = 16_000
AMPLITUDE = 9_000
SCRIPT = (
    "Lectern turns recorded talks into local inspectable knowledge bundles. "
    "This synthetic fixture contains no private or copyrighted media."
)


def generate(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    audio_path = root / "synthetic_talk.wav"
    transcript_path = root / "synthetic_talk.transcript.txt"

    samples: array[int] = array("h")
    for word in SCRIPT.split():
        frequency = 220 + (sum(ord(char) for char in word) % 32) * 12
        duration = min(0.24, max(0.10, len(word) * 0.018))
        _append_tone(samples, frequency, duration)
        _append_silence(samples, 0.035)

    with wave.open(str(audio_path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(samples.tobytes())

    transcript_path.write_text(SCRIPT + "\n", encoding="utf-8")
    return audio_path, transcript_path


def _append_tone(samples: array[int], frequency: int, duration_s: float) -> None:
    total = int(SAMPLE_RATE * duration_s)
    fade = max(1, int(SAMPLE_RATE * 0.01))
    for index in range(total):
        envelope = min(1.0, index / fade, (total - index) / fade)
        value = int(AMPLITUDE * envelope * math.sin(2 * math.pi * frequency * index / SAMPLE_RATE))
        samples.append(value)


def _append_silence(samples: array[int], duration_s: float) -> None:
    samples.extend([0] * int(SAMPLE_RATE * duration_s))


if __name__ == "__main__":
    generate(Path(__file__).resolve().parent)
