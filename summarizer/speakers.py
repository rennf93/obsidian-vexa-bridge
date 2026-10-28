"""Name generic diarization labels ("Speaker 1", "Speaker 2", ...) from the conversation itself.

Vexa 0.12 records no participant roster, so the only source of real names is what people say
(direct address, self-introduction) plus the recording owner (OWNER_NAME). One LLM call, through
the same LiteLLM path as llm.summarize, maps each generic label to a first name. Any failure
(LLM error, bad JSON, invalid or ambiguous mapping) means no renaming: naming is best-effort and
must never fail a meeting.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from summarizer.llm import _extract_text, _format_transcript

if TYPE_CHECKING:
    from summarizer.config import Config
    from summarizer.types import Utterance

log = logging.getLogger("vexa-summarizer")

MAX_NAME_CHARS = 40
_GENERIC_LABEL = re.compile(r"^Speaker \d+$")
# Names become part of the note filename: path separators and ':' would break or escape it.
_FORBIDDEN_NAME_CHARS = re.compile(r"[\\/:\r\n]")

SYSTEM_PROMPT = """\
You identify who the speakers of a diarized meeting transcript are. The speakers are labeled \
generically (for example `Speaker 1`). Map each generic label to that person's first name, but \
only when the conversation supports it: someone is addressed by name, introduces themselves, or \
the recording owner hint plus context makes it clear. Do not invent names and do not guess: if \
the conversation does not support a name for a label, leave that label out of the mapping.

Output ONLY a strict JSON object, no prose and no code fences, whose keys are the generic labels \
exactly as written in the transcript and whose values are first names. Example: \
{"Speaker 1": "Maria", "Speaker 2": "Tom"}. If no label can be named, output {}.
"""


def has_generic_speakers(transcript: list[Utterance]) -> bool:
    labels = {u.speaker for u in transcript}
    # "Unknown" means the lines were never split by speaker: naming it would hand every line to one person.
    return bool(labels) and all(_GENERIC_LABEL.match(s) for s in labels)


def build_naming_prompt(transcript: list[Utterance], cfg: Config) -> list[dict[str, str]]:
    owner = f"The recording owner is {cfg.owner_name}, who is always one of the speakers.\n\n" if cfg.owner_name else ""
    labels = ", ".join(sorted({u.speaker for u in transcript}))
    user = (
        f"{owner}Speaker labels: {labels}\n\n"
        f"Transcript:\n\n{_format_transcript(transcript)}\n\n"
        f"Return the JSON mapping now."
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def parse_speaker_names(raw: str, labels: set[str]) -> dict[str, str]:
    """Validate the model's reply; raises ValueError on anything unusable (caller keeps generic labels)."""
    text = raw.strip()
    if text.startswith("```"):  # tolerate a fenced reply even though the prompt forbids it
        text = text.strip("`").removeprefix("json").strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    mapping: dict[str, str] = {}
    for label, name in data.items():
        if label not in labels:
            continue  # unknown keys ignored
        if not isinstance(name, str):
            raise ValueError(f"name for {label!r} is not a string")
        name = name.strip()
        if not name or len(name) > MAX_NAME_CHARS or _FORBIDDEN_NAME_CHARS.search(name):
            raise ValueError(f"invalid name for {label!r}")
        mapping[label] = name
    if len({n.casefold() for n in mapping.values()}) != len(mapping):
        raise ValueError("two labels mapped to the same name")
    return mapping


async def name_speakers(
    transcript: list[Utterance],
    cfg: Config,
    call_api: Callable[..., Any] | None = None,
) -> list[Utterance]:
    """Return the transcript with generic labels replaced by names, or unchanged on any failure.

    No LLM call when SPEAKER_NAMING_ENABLED is off or the transcript already carries real names.
    """
    if not cfg.speaker_naming_enabled or not has_generic_speakers(transcript):
        return transcript
    try:
        if call_api is None:
            import litellm  # lazy, same as llm.summarize

            call_api = litellm.completion
        resp = call_api(
            model=cfg.ai_model,
            messages=build_naming_prompt(transcript, cfg),
            api_base=cfg.ai_base_url,
            api_key=cfg.ai_api_key,
        )
        mapping = parse_speaker_names(_extract_text(resp), {u.speaker for u in transcript})
    except Exception as exc:
        log.warning("speaker naming failed, keeping generic labels: %s", exc)
        return transcript
    if not mapping:
        log.info("speaker naming: conversation supports no names, keeping generic labels")
        return transcript
    log.info("speaker naming: %s", mapping)
    return [replace(u, speaker=mapping.get(u.speaker, u.speaker)) for u in transcript]
