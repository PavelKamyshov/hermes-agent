"""Unit tests for the fluent-degeneration guard: word salad, not verbatim repetition.

The shape under test is the 2026-10-02 incident - a completed answer (finish_reason="stop") that
repeated nothing, parsed as prose and said nothing. Its measured shape was ttr 0.964, mean word
length 8.13, function-word ratio 0.011, 0.22 newlines per 1k chars, 0.05% structural chars; every
real stored answer at 6000+ chars scored ttr <= 0.60, mean word <= 5.94, function words >= 0.114,
and at least 6.25 newlines per 1k. The tests below rebuild both sides of that separation from
synthetic text - the incident's own bytes never enter the repo.
"""

from __future__ import annotations

import random

from agent.repetition_guard import DEGENERATE_MIN_CHARS, is_incoherent_degeneration

_SYLLABLES = (
    "ка", "ло", "ми", "ту", "ре", "со", "ва", "ні", "пе", "чу", "дра", "зір", "гой", "фля",
    "бер", "тан", "мур", "щад", "кле", "пру", "сто", "дзе", "рив", "нем", "жал", "хви",
)


def _salad(chars: int, seed: int = 7) -> str:
    """Associative word salad: novel long pseudo-words, no line breaks, no function words."""
    rng = random.Random(seed)
    words = []
    total = 0
    while total < chars:
        word = "".join(rng.choice(_SYLLABLES) for _ in range(rng.randint(3, 4)))
        words.append(word)
        total += len(word) + 1
    return " ".join(words) + "."


def _report(paragraphs: int = 40) -> str:
    """An ordinary long Ukrainian answer: short words, function words, real line structure."""
    body = (
        "Ось що я знайшов по цьому питанню і що з ним можна зробити далі. "
        "Я перевірив три джерела і в усіх трьох дані збігаються між собою. "
        "Якщо хочете, я можу підготувати короткий підсумок для клініки окремо."
    )
    return "\n\n".join(f"## Розділ {i + 1}\n{body}" for i in range(paragraphs))


class TestDegenerateOutputGuard:
    def test_incident_shape_is_flagged(self):
        salad = _salad(DEGENERATE_MIN_CHARS * 2)
        assert len(salad) >= DEGENERATE_MIN_CHARS
        assert is_incoherent_degeneration(salad) is True

    def test_short_salad_is_not_judged(self):
        # Below the length floor: too little signal to accuse a response of degenerating.
        assert is_incoherent_degeneration(_salad(DEGENERATE_MIN_CHARS // 3)) is False

    def test_long_ukrainian_report_is_not_flagged(self):
        report = _report()
        assert len(report) >= DEGENERATE_MIN_CHARS
        assert is_incoherent_degeneration(report) is False

    def test_code_block_is_not_flagged(self):
        code = "\n".join(
            f"def handler_{i}(payload, context):\n    return payload.get('key_{i}')"
            for i in range(200)
        )
        assert len(code) >= DEGENERATE_MIN_CHARS
        assert is_incoherent_degeneration(code) is False

    def test_single_line_json_dump_is_not_flagged(self):
        # A wall of data with no newlines still carries structural characters: judging it on word
        # statistics alone would discard a legitimate answer.
        dump = "{" + ", ".join(f'"key_{i}": "value_{i} payload"' for i in range(400)) + "}"
        assert len(dump) >= DEGENERATE_MIN_CHARS
        assert is_incoherent_degeneration(dump) is False

    def test_english_prose_is_not_flagged(self):
        prose = "\n\n".join(
            "I checked the logs and the timeline matches the incident report. "
            "The guard fires on the shape of the answer, not on its language."
            for _ in range(60)
        )
        assert len(prose) >= DEGENERATE_MIN_CHARS
        assert is_incoherent_degeneration(prose) is False

    def test_empty_and_non_string_input_are_safe(self):
        assert is_incoherent_degeneration("") is False
        assert is_incoherent_degeneration(None) is False
