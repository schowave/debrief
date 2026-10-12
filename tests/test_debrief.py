"""Tests for the todo and chat logic: `mise run test`."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import debrief as d  # noqa: E402
import importer as i  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "inbox").mkdir()
    return {"ich": "David", "vault": tmp_path, "inbox": "inbox", "unklar_ordner": "Unklar", "links": True,
            "min_konfidenz": 0.6,
            "kategorien": {"kunde": {"ordner": "Kunden/Kunde", "projekte": {
                "Alpha": {"hinweise": "", "personen": ["Petra Muster"]},
                "Beta": {"hinweise": "", "personen": []}}}}}


def todo(aufgabe, **kw):
    return {"aufgabe": aufgabe, "verantwortlich": None, "faellig": None, "ist_meins": True, **kw}


def result(todos, **kw):
    return {"titel": "Gespräch", "kategorie": "kunde", "projekt": "Alpha", "person": None, "konfidenz": 0.9,
            "begruendung": "b", "zusammenfassung": "z", "entscheidungen": [], "tags": [], "todos": todos, **kw}


# --- is_me / format_todo -------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("David", True), ("david schowalter", True), ("David Schowalter", True),
    ("Davidson", False), ("Anna", False), ("", False), (None, False)])
def test_is_me(cfg, name, expected):
    assert d.is_me(cfg, name) is expected


def test_format_todo_mine_with_due_date_and_urgent():
    assert d.format_todo(todo("Mail schreiben", faellig="2026-10-20", dringend=True), "David") \
        == "- [ ] Mail schreiben 📅 2026-10-20 ⏫"


def test_format_todo_other_person_is_no_task():
    assert d.format_todo(todo("Angebot prüfen", ist_meins=False, verantwortlich="Anna"), "David") \
        == "- Angebot prüfen (@Anna)"


def test_format_todo_done():
    line = d.format_todo(todo("Erledigt", erledigt=True), "David")
    assert line.startswith("- [x] Erledigt ✅ ")


# --- rename_in_result ----------------------------------------------------

def test_rename_does_not_expand_full_name_again():
    out = d.rename_in_result(result([todo("Petr fragen", verantwortlich="Petr")],
                                    zusammenfassung="Petr Novikov und Petr"), {"Petr": "Petr Novikov"})
    assert out["zusammenfassung"] == "Petr Novikov und Petr Novikov"
    assert out["todos"][0]["verantwortlich"] == "Petr Novikov"


def test_rename_speaker_placeholder_not_inside_longer_number():
    out = d.rename_in_result(result([], zusammenfassung="Speaker 1 und Speaker 10"), {"Speaker 1": "Anna"})
    assert out["zusammenfassung"] == "Anna und Speaker 10"


# --- route_todos / write_note --------------------------------------------

def test_route_todos_by_conversation_and_todo_project(cfg):
    res, routed = d.route_todos(cfg, result([
        todo("A"), todo("B", projekt="Beta"), todo("C", ist_meins=False, verantwortlich="Anna")]), reviewed=True)
    assert [t["aufgabe"] for t in res["todos"]] == ["C"]
    assert {p: [t["aufgabe"] for t in ts] for p, ts in routed.items()} == {"Alpha": ["A"], "Beta": ["B"]}
    assert all("projekt" not in t for ts in routed.values() for t in ts)


def test_route_todos_keeps_todos_of_unclear_notes(cfg):
    res, routed = d.route_todos(cfg, result([todo("A")], konfidenz=0.2), reviewed=False)
    assert routed == {} and len(res["todos"]) == 1


def test_route_todos_without_project_keeps_todos(cfg):
    res, routed = d.route_todos(cfg, result([todo("A")], projekt=None), reviewed=True)
    assert routed == {} and len(res["todos"]) == 1


def test_write_note_files_my_todos_into_project_todo_notes(cfg):
    src = cfg["vault"] / "inbox" / "x.md"
    src.write_text("---\nTitle: x\nRecorded At: 2026-10-12 10:00:00\n---\n\nHallo\n")
    dest = d.write_note(src, cfg, result([todo("A", dringend=True), todo("B", projekt="Beta"),
                                          todo("C", ist_meins=False, verantwortlich="Anna")]), reviewed=True)
    note = dest.read_text()
    assert note.count("## Meine Todos") == 1
    assert "[[Kunden/Kunde/Alpha/Todos|Todos Alpha]]" in note and "[[Kunden/Kunde/Beta/Todos|Todos Beta]]" in note
    assert "- C (@Anna)" in note
    alpha = (cfg["vault"] / "Kunden/Kunde/Alpha/Todos.md").read_text()
    assert f"- [ ] A ⏫ [[{dest.stem}]]" in alpha
    assert f"- [ ] B [[{dest.stem}]]" in (cfg["vault"] / "Kunden/Kunde/Beta/Todos.md").read_text()
    assert not src.exists()


def test_collect_project_todos_from_note_moved_by_hand(cfg):
    folder = cfg["vault"] / "Kunden/Kunde/Alpha/Petra Muster"
    folder.mkdir(parents=True)
    note = folder / "2026-10-01 Alt.md"
    note.write_text("## Meine Todos\n\n- [ ] Offen\n- [x] Fertig ✅ 2026-10-02\n\n## Todos anderer\n\n- X (@Anna)\n")
    assert d.collect_project_todos(cfg) == 2
    text = note.read_text()
    assert "- [ ]" not in text and "- [x]" not in text
    assert "## Meine Todos\n\n- 2 in [[Kunden/Kunde/Alpha/Todos|Todos Alpha]]\n\n## Todos anderer" in text
    todos = (cfg["vault"] / "Kunden/Kunde/Alpha/Todos.md").read_text()
    assert "- [ ] Offen [[2026-10-01 Alt]]" in todos
    assert d.collect_project_todos(cfg) == 0  # nothing left to move


# --- Chat import ---------------------------------------------------------

@pytest.mark.parametrize("line, name, stamp", [
    ("Max Mustermann   12:34", "Max Mustermann", dt.time(12, 34)),
    ("Anna Beispiel 12.10.2026 12:36", "Anna Beispiel", dt.datetime(2026, 10, 12, 12, 36)),
    ("[12:40] Max Mustermann", "Max Mustermann", dt.time(12, 40)),
    ("Max 10/9/26 3:05 PM", "Max", dt.datetime(2026, 10, 9, 15, 5)),
    ("Ludwig van Beethoven 09:00", "Ludwig van Beethoven", dt.time(9, 0)),
])
def test_chat_header(line, name, stamp):
    assert i.chat_header(line) == (name, stamp)


@pytest.mark.parametrize("line", ["Termin um 12:30", "Wir treffen uns 12.10.2026 12:30 im Büro", "Hallo"])
def test_chat_header_ignores_normal_text(line):
    assert i.chat_header(line) is None


def test_normalize_chat_detects_speakers():
    body = i.normalize_chat("Max Mustermann  12:34\nHallo zusammen, kurze Frage.\n\n\n\n"
                            "Anna Beispiel 12:36\nDas passt so für mich.")
    assert body == "Max Mustermann\nHallo zusammen, kurze Frage.\n\nAnna Beispiel\nDas passt so für mich."
    assert i.detect_speakers(body) == ["Max Mustermann", "Anna Beispiel"]


def test_chat_start():
    now = dt.datetime(2026, 10, 12, 14, 0)
    assert i.chat_start("Max  12.10.2026 09:15\nHallo", now) == dt.datetime(2026, 10, 12, 9, 15)
    assert i.chat_start("Max  09:15\nHallo", now) == dt.datetime(2026, 10, 12, 9, 15)
    assert i.chat_start("Max  15:10\nHallo", now) == dt.datetime(2026, 10, 11, 15, 10)  # later than now: yesterday
    assert i.chat_start("nur Text", now) is None
