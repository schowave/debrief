"""Tests for the inbox scan in the review server: `mise run test`."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import debrief as d  # noqa: E402
import server as s  # noqa: E402

TEXT = " ".join(f"wort{n}" for n in range(200))


@pytest.fixture
def store(tmp_path, monkeypatch):
    (tmp_path / "inbox").mkdir()
    (tmp_path / "trash").mkdir()
    monkeypatch.setattr(s, "PENDING", tmp_path / "pending")
    monkeypatch.setattr(s, "TRASH", tmp_path / "trash")
    monkeypatch.setattr(s, "DUPLIKATE", tmp_path / "duplicates.json")
    monkeypatch.setattr(d, "FINGERABDRUECKE", tmp_path / "fingerprints.json")
    monkeypatch.setattr(s, "MIN_FILE_AGE_S", 0)
    monkeypatch.setattr(s, "RENAME_AFTER_S", float("inf"))
    monkeypatch.setattr(s, "calendar_candidates", lambda cfg, meta: [])
    st = s.Store({"vault": tmp_path, "inbox": "inbox", "min_woerter": 30})
    monkeypatch.setattr(st, "notify_text", lambda *a, **kw: None)
    return st


def transcript(store, name, mtime):
    path = store.inbox / name
    path.write_text(f"Sprecher 1\n{TEXT}\n")
    os.utime(path, (mtime, mtime))
    return path


@pytest.mark.parametrize("names", [("a.md", "b.md"), ("b.md", "a.md")])
def test_newest_duplicate_in_inbox_wins(store, names):
    old, new = names
    transcript(store, old, 1_000_000)
    store.scan()
    transcript(store, new, 2_000_000)
    store.scan()
    assert [p.name for p in store.inbox.glob("*.md")] == [new]
    assert [i["datei"] for i in store.all()] == [new]
    assert [p.name for p in s.TRASH.iterdir()] == [old]


def test_both_arrive_in_one_pass_newest_wins(store):
    transcript(store, "a.md", 2_000_000)
    transcript(store, "b.md", 1_000_000)
    store.scan()
    assert [i["datei"] for i in store.all()] == ["a.md"]
    assert [p.name for p in s.TRASH.iterdir()] == ["b.md"]


def test_filed_note_stays_and_new_copy_is_discarded(store):
    meta = {}
    d.add_fingerprint(d.fingerprint(f"Sprecher 1\n{TEXT}\n", ["Sprecher 1"], meta), "Notiz", "Kunden/Notiz.md")
    transcript(store, "a.md", 2_000_000)
    store.scan()
    assert store.all() == []
    assert [p.name for p in s.TRASH.iterdir()] == ["a.md"]


def test_sorted_by_recording_date_newest_first(store):
    for item_id, recorded, created in [("a" * 12, "2026-10-01 09:00:00", "2026-10-23T10:00:00"),
                                       ("b" * 12, "2026-10-22 17:18:27", "2026-10-19T10:00:00"),
                                       ("c" * 12, "", "2026-10-05T08:00:00")]:
        store.put({"id": item_id, "datei": f"{item_id}.md", "aufgenommen": recorded, "erstellt": created})
    assert [i["id"][0] for i in store.all()] == ["b", "c", "a"]


def test_live_session_hands_over_speaker_names(store, tmp_path):
    import live
    store.live = live.LiveManager({"kategorien": {}}, db=tmp_path / "none.sqlite", directory=tmp_path / "live")
    sitzung = live.new_session({"id": "C" * 32, "titel": "T", "start": "2026-10-30T13:00:00+01:00"}, {})
    sitzung["sprecher_namen"] = {"Speaker 2": "Erika Muster", "Speaker 9": "Nicht dabei"}
    store.live.put(sitzung)
    path = store.inbox / "Meeting.md"
    path.write_text(f"---\nRecorded At: 2026-10-30T13:00:40+01:00\nSpeakers:\n- Speaker 2\n---\nSpeaker 2\n{TEXT}\n")
    os.utime(path, (1_000_000, 1_000_000))
    store.scan()
    item = store.all()[0]
    assert item["live"] == "C" * 32
    assert item["sprecher_namen"] == {"Speaker 2": "Erika Muster"}
