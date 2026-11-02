"""Existing transcripts as recordings for the live assistant. `mise run test`."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import live  # noqa: E402
import transcripts  # noqa: E402

MACWHISPER = """---
Title: Planung Q4
Recorded At: 2026-10-20T09:00:00
Speakers:
- Anna Beispiel
- Ben Muster
---

Anna Beispiel
Wir starten mit dem Antrag. Der Kunde reicht ihn per Portal ein.

Ben Muster
Dann prüfen wir die Unterlagen und melden uns zurück.
"""

VTT = """WEBVTT

1
00:00:05.000 --> 00:00:08.000
<v Anna Beispiel>Guten Morgen zusammen.</v>

2
00:00:10.500 --> 00:00:14.000
<v Ben Muster>Morgen, wir können anfangen.</v>
"""

CHAT = """Anna Beispiel  09:15
Hast du den Antrag schon gesehen?

Ben Muster  09:16
Ja, ich schaue ihn mir gleich an.
"""


def test_macwhisper_export_with_frontmatter():
    t = transcripts.parse(MACWHISPER, "Planung.md")
    assert t["titel"] == "Planung Q4" and t["start"].startswith("2026-10-20T09:00:00")
    assert [(t["sprecher"][z["sprecher"]], z["text"]) for z in t["zeilen"]] == [
        ("Anna Beispiel", "Wir starten mit dem Antrag. Der Kunde reicht ihn per Portal ein."),
        ("Ben Muster", "Dann prüfen wir die Unterlagen und melden uns zurück.")]
    assert t["zeilen"][1]["start_ms"] > t["zeilen"][0]["ende_ms"]  # estimated times run forward


def test_webvtt_keeps_its_times_and_speakers():
    t = transcripts.parse(VTT, "teams.vtt")
    assert [(z["start_ms"], t["sprecher"][z["sprecher"]]) for z in t["zeilen"]] == [(5000, "Anna Beispiel"), (10500, "Ben Muster")]


def test_pasted_chat_and_plain_text():
    chat = transcripts.parse(CHAT)
    assert [t for t in chat["sprecher"].values() if t != "?"] == ["Anna Beispiel", "Ben Muster"]
    plain = transcripts.parse("Erster Absatz mit einigen Worten.\n\nZweiter Absatz folgt hier.")
    assert [z["text"] for z in plain["zeilen"]] == ["Erster Absatz mit einigen Worten.", "Zweiter Absatz folgt hier."]
    named = transcripts.parse("[00:01:00] Anna: Es geht los.\n[00:01:30] Ben: Ja, genau.")
    assert [(z["start_ms"], named["sprecher"][z["sprecher"]]) for z in named["zeilen"]] == [(60_000, "Anna"), (90_000, "Ben")]
    with pytest.raises(ValueError):
        transcripts.parse("  \n ")


def test_imported_transcript_becomes_a_session_and_can_be_attached(tmp_path, monkeypatch):
    from livedata import make_db
    db = make_db(tmp_path / "main.sqlite")
    lm = live.LiveManager({"kategorien": {}, "kategorien_datei": tmp_path / "c.yaml", "ich": "Ich"}, db=db, directory=tmp_path / "live")
    monkeypatch.setattr(lm, "_calendar", lambda sid: None)
    s = lm.new_session_from_transcript(MACWHISPER, "Planung.md")
    assert s["titel"] == "Planung Q4" and not s["laeuft"]
    zeilen, labels = lm.visible_lines(s)
    assert [z["nr"] for z in zeilen] == [0, 1] and "Anna Beispiel" in labels.values()
    assert any(m["id"] == s["id"] and m["app"] == "Datei" for m in lm.meetings())
    assert lm.new_session_from_transcript(MACWHISPER, "Planung.md")["id"] == s["id"]  # same text, same session
    # a second transcript follows the first one, even though it is older
    s = lm.attach_transcript(s["id"], VTT, "teams.vtt")
    zeilen, _ = lm.visible_lines(s)
    assert [z["nr"] for z in zeilen] == [0, 1, 2, 3]
    assert zeilen[2]["start_ms"] >= zeilen[1]["ende_ms"] + live.GAP_AFTER_LAST_MS + 5000
    assert not lm.is_running(s)
