"""Tests for summarizer.speakers: generic-label detection, reply validation, the naming seam."""

import json
import logging

import pytest

from summarizer import config, speakers
from summarizer.types import Utterance


def _cfg(enabled=True, owner="Renzo"):
    return config.Config(ai_model="openai/x", ai_api_key="k", speaker_naming_enabled=enabled, owner_name=owner)


def _generic():
    return [
        Utterance("Speaker 1", 0.0, 5.0, "Hi Maria, thanks for joining."),
        Utterance("Speaker 2", 5.0, 10.0, "Thanks Renzo."),
    ]


def _reply(payload):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    calls = []

    def call_api(**kwargs):
        calls.append(kwargs)
        return raw

    return call_api, calls


@pytest.mark.parametrize(
    "labels,expected",
    [
        (["Speaker 1", "Speaker 12"], True),
        (["Speaker 1"], False),
        (["Speaker 1", "Unknown"], False),
        (["Unknown"], False),
        (["David", "Renzo"], False),
        (["Speaker 1", "David"], False),
        (["Speaker one"], False),
        ([], False),
    ],
)
def test_has_generic_speakers(labels, expected):
    assert speakers.has_generic_speakers([Utterance(s, 0, 1, "x") for s in labels]) is expected


async def test_mapping_is_applied_and_unmapped_label_kept():
    call_api, _ = _reply({"Speaker 1": "Renzo"})
    out = await speakers.name_speakers(_generic(), _cfg(), call_api)
    assert [u.speaker for u in out] == ["Renzo", "Speaker 2"]
    assert out[0].text == "Hi Maria, thanks for joining."


async def test_prompt_has_owner_hint_labels_and_transcript():
    call_api, calls = _reply({})
    await speakers.name_speakers(_generic(), _cfg(owner="Renzo"), call_api)
    msgs = calls[0]["messages"]
    assert calls[0]["model"] == "openai/x" and calls[0]["api_key"] == "k"
    assert "recording owner is Renzo" in msgs[1]["content"]
    assert "[00:00:00] Speaker 1: Hi Maria" in msgs[1]["content"]
    assert "strict JSON" in msgs[0]["content"] and "Do not invent names" in msgs[0]["content"]


async def test_prompt_without_owner_has_no_owner_line():
    call_api, calls = _reply({})
    await speakers.name_speakers(_generic(), _cfg(owner=None), call_api)
    assert "recording owner" not in calls[0]["messages"][1]["content"]


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        '["Renzo"]',
        {"Speaker 1": "Ana", "Speaker 2": "ana"},  # duplicate names (case-insensitive)
        {"Speaker 1": "A" * 41},
        {"Speaker 1": ""},
        {"Speaker 1": "Ana\nBob"},
        {"Speaker 1": "a/b"},
        {"Speaker 1": "*Bob"},
        {"Speaker 1": "Bob #1"},
        {"Speaker 1": "[[X]]"},
        {"Speaker 1": "Bob|Al"},
        {"Speaker 1": "Bob^"},
        {"Speaker 1": "-Bob"},
        {"Speaker 1": "Bob2"},
        {"Speaker 1": 5},
    ],
)
async def test_bad_reply_means_no_renaming(reply, caplog):
    call_api, _ = _reply(reply)
    with caplog.at_level(logging.WARNING, logger="vexa-summarizer"):
        out = await speakers.name_speakers(_generic(), _cfg(), call_api)
    assert out == _generic()
    assert "speaker naming failed" in caplog.text


async def test_llm_exception_means_no_renaming(caplog):
    def boom(**_):
        raise TimeoutError("slow")

    with caplog.at_level(logging.WARNING, logger="vexa-summarizer"):
        out = await speakers.name_speakers(_generic(), _cfg(), boom)
    assert out == _generic()
    assert "slow" in caplog.text


async def test_unknown_keys_ignored():
    call_api, _ = _reply({"Speaker 1": "Renzo", "Speaker 9": "Ghost"})
    out = await speakers.name_speakers(_generic(), _cfg(), call_api)
    assert [u.speaker for u in out] == ["Renzo", "Speaker 2"]


async def test_fenced_reply_is_tolerated():
    call_api, _ = _reply('```json\n{"Speaker 2": "Maria"}\n```')
    out = await speakers.name_speakers(_generic(), _cfg(), call_api)
    assert [u.speaker for u in out] == ["Speaker 1", "Maria"]


async def test_disabled_flag_makes_no_call():
    call_api, calls = _reply({"Speaker 1": "Renzo"})
    out = await speakers.name_speakers(_generic(), _cfg(enabled=False), call_api)
    assert out == _generic() and calls == []


async def test_already_named_transcript_makes_no_call():
    named = [Utterance("David", 0, 1, "hi"), Utterance("Renzo", 1, 2, "hello")]
    call_api, calls = _reply({"David": "X"})
    assert await speakers.name_speakers(named, _cfg(), call_api) == named
    assert calls == []


@pytest.mark.parametrize("name", ["José", "Zoë", "Dmitry", "Анна", "Mary-Jane", "O'Brien", "J. R. Smith"])
def test_safe_names_pass(name):
    assert speakers.parse_speaker_names(json.dumps({"Speaker 1": name}), {"Speaker 1"}) == {"Speaker 1": name}
