"""Local vision-model image classifier (LM Studio, OpenAI-compatible).

Sends each candidate image to a locally-hosted vision model and asks it to bin
the image into one of four categories. Images are downscaled with the macOS
built-in ``sips`` before sending — vision encoders tile by pixel dimensions, so
a smaller image is markedly faster and cheaper with no quality loss for this
coarse task. If ``sips`` is unavailable the raw bytes are sent (files are ≤1MB
by the time they reach here, so that is fine).
"""

from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests

from .errors import ClassificationError, ClassifierUnavailableError
from .logutil import get_logger

logger = get_logger(__name__)

CATEGORIES = ("screenshot", "meme", "photo", "other")

# Structured-output schema the model must fill. Verified to work against a live
# LM Studio: strict json_schema is honoured and returns valid matching JSON.
_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "image_classification",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": list(CATEGORIES)},
                "confidence": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["category", "confidence", "reason"],
            "additionalProperties": False,
        },
    },
}

# Photo-biased on purpose: a flagged image is a deletion candidate, so when the
# model is unsure we want it to fall back to "photo" (keep) rather than delete.
_PROMPT = (
    "Classify this image into exactly one category:\n"
    '- "screenshot": a capture of a device screen — status bars, app UI, chat '
    "conversations, web pages, settings, error dialogs.\n"
    '- "meme": an image made to be shared as a joke — image macros, captioned '
    "humor, reaction images, forwarded jokes.\n"
    '- "photo": a real photograph taken with a camera — people, pets, places, '
    "objects, food, scenery, even if low quality, blurry, or dark.\n"
    '- "other": none of the above — logos, saved graphics, posters, diagrams, '
    "documents, blank or corrupt images.\n"
    'If you are unsure between "photo" and any other category, answer "photo".\n'
    "Respond with JSON: category, confidence (0.0-1.0), and reason (one short "
    "sentence)."
)


@dataclass(frozen=True)
class Classification:
    category: str
    confidence: float
    reason: str


def prepare_image(path: Path, max_dim: int, work_dir: Path) -> tuple[bytes, str]:
    """Return ``(bytes, mime)`` for ``path``, downscaled to ``max_dim`` px.

    Uses ``sips`` (ships with macOS) to produce a JPEG no larger than
    ``max_dim`` on its longest side. On any failure — ``sips`` missing, nonzero
    exit, empty output — falls back to the raw file bytes and its real mime.
    """
    out = work_dir / f"{path.stem}-{abs(hash(str(path))) & 0xFFFFFF:x}.jpg"
    try:
        proc = subprocess.run(
            ["sips", "-Z", str(max_dim), "-s", "format", "jpeg",
             str(path), "--out", str(out)],
            capture_output=True,
            timeout=60,
        )
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 0:
            data = out.read_bytes()
            out.unlink(missing_ok=True)
            return data, "image/jpeg"
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("sips downscale failed for %s (%s); sending raw", path, exc)

    data = path.read_bytes()
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return data, mime


class LMStudioClassifier:
    """Classifies images via an OpenAI-compatible local vision endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: tuple[float, float],
        max_dim: int,
        work_dir: Path,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_dim = max_dim
        self.work_dir = work_dir

    def check_available(self) -> None:
        """Raise :class:`ClassifierUnavailableError` if the endpoint is down."""
        url = f"{self.base_url}/v1/models"
        try:
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ClassifierUnavailableError(
                f"Local vision model is not reachable at {self.base_url}. "
                "Start LM Studio, load a vision model, and enable the local "
                "server — or pass --lm-url to point elsewhere.\n"
                f"({exc})"
            ) from exc

    def classify(self, path: Path) -> Classification:
        data, mime = prepare_image(path, self.max_dim, self.work_dir)
        b64 = base64.b64encode(data).decode("ascii")
        body = {
            "model": self.model,
            # Mandatory: this is a thinking model, and without "none" it spends
            # the entire token budget on reasoning and returns empty content.
            "reasoning_effort": "none",
            "temperature": 0,
            "max_tokens": 300,
            "response_format": _RESPONSE_FORMAT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                    ],
                }
            ],
        }
        url = f"{self.base_url}/v1/chat/completions"
        try:
            resp = requests.post(url, json=body, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ClassificationError(f"{path.name}: request failed ({exc})") from exc

        try:
            content = resp.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            category = parsed["category"]
            confidence = float(parsed.get("confidence", 0.0))
            reason = str(parsed.get("reason", ""))
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise ClassificationError(
                f"{path.name}: could not parse model response ({exc})"
            ) from exc

        if category not in CATEGORIES:
            raise ClassificationError(
                f"{path.name}: model returned unknown category {category!r}"
            )
        confidence = max(0.0, min(1.0, confidence))
        return Classification(category=category, confidence=confidence, reason=reason)
