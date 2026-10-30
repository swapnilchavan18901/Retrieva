"""Deterministic paragraph/heading chunks; no embedding calls or cross-block overlap."""

import re
import unicodedata

CHUNK_SIZE = 1000
CHUNKER_VERSION = "structure-v1"


def _heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 100:
        return False
    return bool(
        re.match(r"^#{1,6}\s+\S", line)
        or re.match(r"^\d+(?:\.\d+)*[.)]?\s+[A-Z]", line) and len(line.split()) <= 12
        or line.endswith(":") and len(line.split()) <= 12
        or line.isupper() and len(line.split()) <= 12
    )


def _bounded(text: str, maximum: int) -> list[str]:
    """Prefer complete sentences; fall back to words for very long sentences."""
    chunks, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        units = [sentence] if len(sentence) <= maximum else sentence.split()
        for unit in units:
            while len(unit) > maximum:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(unit[:maximum])
                unit = unit[maximum:]
            if not unit:
                continue
            candidate = f"{current} {unit}".strip()
            if len(candidate) > maximum:
                chunks.append(current)
                current = unit
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def structure_chunks(text: str, maximum: int = CHUNK_SIZE) -> list[dict]:
    if maximum < 1:
        raise ValueError("Chunk size must be positive")
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    chunks, paragraph = [], []
    section = None

    def flush():
        if paragraph:
            value = " ".join(" ".join(line.split()) for line in paragraph).strip()
            chunks.extend({"text": part, "section": section} for part in _bounded(value, maximum))
            paragraph.clear()

    for line in text.split("\n"):
        if not line.strip():
            flush()
        elif _heading(line):
            flush()
            section = " ".join(line.split())
            # Keep headings independently, so editing a heading doesn't invalidate its entire section.
            chunks.extend({"text": part, "section": section} for part in _bounded(section, maximum))
        else:
            paragraph.append(line)
    flush()
    return chunks
