"""Tests for the live assistant: MacWhisper reader, merge rules, lifecycle, process library. `mise run test`."""
import datetime as dt
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import live  # noqa: E402
import macwhisper as mw  # noqa: E402

SID = uuid.UUID(int=1).bytes
SPK = uuid.UUID(int=2).bytes


def make_db(path: Path, running: bool = True, start: str = "2026-10-30 12:00:00.000") -> Path:
    """Tiny database in MacWhisper's schema (only the columns we read)."""
    con = sqlite3.connect(path)
    con.executescript("""
        create table recordedmeeting (id blob primary key, title text, appName text, matchedCalendarTitle text);
        create table session (id blob primary key, dateCreated text, playbackDuration real, transcriptionDidSucceed int,
                              recordedMeetingID blob, userChosenTitle text, aiTitle text, dateDeleted text);
        create table speaker (id blob primary key, name text);
        create table transcriptline (id blob primary key, dateCreated text, dateUpdated text, text text, start int,
                                     end int, sessionId blob, speakerID blob, orderIndex int, isConfirmed int);
    """)
    con.execute("insert into recordedmeeting values (?, 'Teams Meeting', 'Teams', 'Prozess-Workshop')", (b"m",))
    con.execute("insert into session values (?, ?, 0, ?, ?, null, null, null)", (SID, start, None if running else 1, b"m"))
    con.execute("insert into speaker values (?, null)", (SPK,))
    con.commit()
    con.close()
    return path


def add_line(db: Path, nr: int, text: str, start_ms: int, updated: str, confirmed: int = 1, spk=SPK) -> None:
    con = sqlite3.connect(db)
    con.execute("insert or replace into transcriptline values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uuid.UUID(int=100 + nr).bytes, updated, updated, text, start_ms, start_ms + 1000, SID, spk, nr, confirmed))
    con.commit()
    con.close()


@pytest.fixture
def db(tmp_path):
    return make_db(tmp_path / "main.sqlite")


def test_reader_lines_speakers_and_incremental(db):
    add_line(db, 0, "Hallo zusammen", 0, "2026-10-30 12:00:01.000")
    add_line(db, 1, "Der Antrag geht an…", 2000, "2026-10-30 12:00:03.000", confirmed=0)
    con = mw.connect(db)
    now = dt.datetime(2026, 10, 30, 12, 1, tzinfo=dt.timezone.utc)
    m = mw.running(con, now)
    assert m["titel"] == "Prozess-Workshop" and m["laeuft"] and m["zeilen"] == 2
    assert mw.speakers(con, m["id"]) == {"": "?", SPK.hex().upper(): "Speaker 1"}
    zeilen = mw.lines(con, m["id"])
    assert [z["bestaetigt"] for z in zeilen] == [True, False]
    add_line(db, 1, "Der Antrag geht an die Fachabteilung.", 2000, "2026-10-30 12:00:05.000")
    changed = mw.lines(con, m["id"], zeilen[-1]["stand"])
    assert [(z["nr"], z["bestaetigt"]) for z in changed] == [(1, True)]


def test_stale_unfinished_session_is_not_running(db):
    add_line(db, 0, "Hallo", 0, "2026-10-30 12:00:01.000")
    con = mw.connect(db)
    later = dt.datetime(2026, 10, 30, 13, 0, tzinfo=dt.timezone.utc)
    assert mw.running(con, later) is None


def session_with_process():
    s = live.new_session({"id": "A" * 32, "titel": "T", "start": "2026-10-30T12:00:00+00:00"},
                         {"weg": "direkt", "analyse": "sonnet", "recherche": "sonnet"})
    live.apply_analysis(s, {"aenderungen": [
        {"op": "prozess_neu", "ref": "p1", "text": "Antrag"},
        {"op": "neu", "prozess": "p1", "ref": "n1", "typ": "command", "text": "Antrag stellen", "lane": "Kunde"},
        {"op": "neu", "prozess": "p1", "ref": "n2", "typ": "event", "text": "Antrag gestellt", "nach": "n1"},
        {"op": "verbinden", "von": "n1", "nach": "n2"},
    ]}, 1)
    return s


def test_analysis_builds_process_with_refs():
    s = session_with_process()
    p = s["prozesse"][0]
    assert p["name"] == "Antrag"
    assert [(e["typ"], e["text"], e["gesperrt"]) for e in p["elemente"]] == [
        ("command", "Antrag stellen", False), ("event", "Antrag gestellt", False)]
    assert p["verbindungen"] == [{"von": p["elemente"][0]["id"], "nach": p["elemente"][1]["id"]}]


def test_locked_elements_only_get_proposals_and_rejections_stick():
    s = session_with_process()
    cmd = s["prozesse"][0]["elemente"][0]
    live.edit_process(s, {"op": "element_aendern", "id": cmd["id"], "text": "Antrag einreichen"})
    assert cmd["gesperrt"] and cmd["herkunft"] == "ich"
    change = {"aenderungen": [{"op": "aendern", "element": cmd["id"], "text": "Antrag abschicken", "begruendung": "x"}]}
    live.apply_analysis(s, change, 2)
    assert cmd["text"] == "Antrag einreichen"
    assert [v["felder"] for v in s["vorschlaege"]] == [{"text": "Antrag abschicken"}]
    live.decide_proposal(s, s["vorschlaege"][0]["id"], annehmen=False)
    live.apply_analysis(s, change, 3)
    assert s["vorschlaege"] == []


def test_accepted_proposal_changes_element():
    s = session_with_process()
    ev = s["prozesse"][0]["elemente"][1]
    live.edit_process(s, {"op": "bestaetigen", "id": ev["id"]})
    live.apply_analysis(s, {"aenderungen": [{"op": "loeschen", "element": ev["id"]}]}, 2)
    assert len(s["prozesse"][0]["elemente"]) == 2
    live.decide_proposal(s, s["vorschlaege"][0]["id"], annehmen=True)
    assert [e["text"] for e in s["prozesse"][0]["elemente"]] == ["Antrag stellen"]


def test_deleted_element_is_not_added_again():
    s = session_with_process()
    ev = s["prozesse"][0]["elemente"][1]
    live.edit_process(s, {"op": "element_loeschen", "id": ev["id"]})
    live.apply_analysis(s, {"aenderungen": [{"op": "neu", "typ": "event", "text": "Antrag  gestellt!"}]}, 2)
    assert [e["text"] for e in s["prozesse"][0]["elemente"]] == ["Antrag stellen"]
    assert s["prozesse"][0]["verbindungen"] == []


def test_entries_deduplicated_and_dropped_ones_stay_dropped():
    s = session_with_process()
    neu = {"typ": "frage", "text": "Wer prüft den Antrag?", "begruendung": "", "prio": 1, "zeilen": [3], "elemente": ["x"]}
    live.apply_analysis(s, {"eintraege": [neu]}, 1)
    live.set_entry_status(s, s["eintraege"][0]["id"], "verworfen")
    live.apply_analysis(s, {"eintraege": [{**neu, "text": "wer prüft den Antrag"}]}, 2)
    assert len(s["eintraege"]) == 1


def test_assumption_ratings_become_facts_and_questions():
    s = session_with_process()
    ev = s["prozesse"][0]["elemente"][1]
    live.apply_analysis(s, {"eintraege": [
        {"typ": "annahme", "text": "Anträge kommen per E-Mail", "begruendung": "", "prio": 2, "zeilen": [], "elemente": [ev["id"]]},
        {"typ": "annahme", "text": "Es gibt eine Frist", "begruendung": "", "prio": 2, "zeilen": [], "elemente": []},
        {"typ": "annahme", "text": "Prüfung dauert 2 Tage", "begruendung": "", "prio": 2, "zeilen": [], "elemente": []},
    ]}, 1)
    a, b, c = s["eintraege"]
    live.set_entry_status(s, a["id"], "stimmt")
    assert ev["gesperrt"] and ev["herkunft"] == "bestaetigt"
    live.set_entry_status(s, b["id"], "stimmt_nicht", "Frist ist 14 Tage")
    assert [f["text"] for f in s["fakten"]] == ["Anträge kommen per E-Mail",
                                                "Falsch: Es gibt eine Frist. Richtig ist: Frist ist 14 Tage"]
    live.set_entry_status(s, c["id"], "unklar")
    assert c["typ"] == "frage" and c["status"] == "neu"
    live.set_entry_status(s, b["id"], "neu")  # undo removes the fact again
    assert len(s["fakten"]) == 1


def test_answered_questions_and_instruction_classification():
    s = session_with_process()
    live.add_entry(s, "frage", "Was passiert bei Ablehnung?")
    q = s["eintraege"][0]
    a = live.add_instruction(s, "Der Prozess startet bei der Freigabe")
    once = live.add_instruction(s, "Fokus auf Fehlerfälle", einmalig=True)
    keep = live.add_instruction(s, "Kurze Fragen bitte")
    live.apply_analysis(s, {"erledigt": [{"id": q["id"], "notiz": "Geht zurück an den Kunden"}],
                            "anweisungen": [{"id": a["id"], "art": "fakt", "fakt": "Prozess startet bei Freigabe"}]}, 2)
    assert q["status"] == "beantwortet" and q["notiz"] == "Geht zurück an den Kunden"
    assert [f["text"] for f in s["fakten"]] == ["Prozess startet bei Freigabe"]
    assert [x["id"] for x in s["anweisungen"]] == [keep["id"]] and once["id"] not in [x["id"] for x in s["anweisungen"]]
    assert s["anweisungen"][0]["neu"] is False


def test_window_summarises_old_part():
    s = session_with_process()
    zeilen = [{"nr": i, "start_ms": i * 60_000, "bestaetigt": True, "text": "x", "sprecher": ""} for i in range(30)]
    summarise, verbatim = live.window(s, zeilen, 15)
    assert [z["nr"] for z in summarise] == list(range(14)) and verbatim[0]["nr"] == 14
    s["zusammenfassung"]["bis_ms"] = 14 * 60_000
    summarise, verbatim = live.window(s, zeilen, 15)
    assert summarise == [] and verbatim[0]["nr"] == 14


def test_replay_shows_lines_up_to_virtual_time():
    s = session_with_process()
    s["replay"] = {"start": 1000.0, "tempo": 10}
    zeilen = [{"nr": i, "start_ms": i * 10_000} for i in range(5)]
    assert [z["nr"] for z in live.visible(s, zeilen, now=1002.0)] == [0, 1, 2]


def test_context_suggestion_from_calendar():
    cfg = {"kategorien": {"acme": {"ordner": "Kunden/Acme", "projekte": {
        "plattform": {"hinweise": "Migration", "personen": ["Erika Muster"], "repos": ["/x"]},
        "andere": {"hinweise": "", "personen": []}}}}}
    termin = {"titel": "Weekly", "teilnehmer": [{"name": "Erika Muster"}]}
    assert live.suggest_context(cfg, termin) == {"kategorie": "acme", "projekt": "plattform"}
    assert live.project_repos(cfg, "acme", "plattform") == ["/x"]


def test_library_roundtrip_and_history(tmp_path):
    cfg = {"vault": tmp_path, "kategorien": {"acme": {"ordner": "Kunden/Acme", "projekte": {"p": {}}}}}
    s = session_with_process()
    s["kontext"].update(kategorie="acme", projekt="p")
    rel = live.save_library_process(cfg, s, s["prozesse"][0]["id"])
    assert rel == "Kunden/Acme/p/Prozesse/Antrag.md"
    text = (tmp_path / rel).read_text()
    assert "```mermaid" in text and "angelegt mit 2 Elementen" in text
    assert [x["name"] for x in live.list_library(cfg, "acme", "p")] == ["Antrag"]

    s2 = session_with_process()
    s2["prozesse"] = []
    s2["kontext"].update(kategorie="acme", projekt="p")
    p = live.load_library_process(cfg, s2, rel)
    assert all(e["gesperrt"] and e["herkunft"] == "bibliothek" for e in p["elemente"])
    assert len(p["verbindungen"]) == 1
    live.edit_process(s2, {"op": "element_neu", "prozess": p["id"], "typ": "policy", "text": "Bei Eingang prüfen"})
    live.save_library_process(cfg, s2, p["id"])
    text = (tmp_path / rel).read_text()
    assert "angelegt mit 2 Elementen" in text and "+1 / −0 / ~0 Elemente" in text
    assert len(list((tmp_path / "Kunden/Acme/p/Prozesse").glob("*.md"))) == 1


def test_note_section_lists_questions_and_process():
    s = session_with_process()
    live.add_entry(s, "frage", "Wer genehmigt?")
    section = live.note_section(s)
    assert section.startswith("## Live-Assistent")
    assert "- [ ] Wer genehmigt?" in section and "### Prozess: Antrag" in section


def test_manager_session_for_recording(tmp_path, db):
    lm = live.LiveManager({"kategorien": {}}, db=db, directory=tmp_path / "live")
    s = live.new_session({"id": "B" * 32, "titel": "T", "start": "2026-10-30T13:00:00+01:00"}, lm.settings())
    lm.put(s)
    assert lm.session_for_recording("2026-10-30T13:01:30+01:00")["id"] == "B" * 32
    assert lm.session_for_recording(dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=10)) is None


def test_manager_analysis_merges_result(tmp_path, db, monkeypatch):
    add_line(db, 0, "Der Kunde stellt einen Antrag.", 0, "2026-10-30 12:00:01.000")
    lm = live.LiveManager({"kategorien": {}, "kategorien_datei": tmp_path / "c.yaml", "ich": "Ich"}, db=db, directory=tmp_path / "live")
    sid = SID.hex().upper()
    monkeypatch.setattr(lm, "_calendar", lambda sid: None)
    s = lm.open_session(sid)
    assert s["titel"] == "Prozess-Workshop"
    calls = []

    def fake_claude(cfg, system, prompt, schema, label, model=None, dirs=None):
        calls.append(prompt)
        return {"eintraege": [{"typ": "frage", "text": "Wie kommt der Antrag rein?", "begruendung": "", "prio": 1,
                               "zeilen": [0], "elemente": []}], "erledigt": [], "aenderungen": [], "recherche": [],
                "anweisungen": []}, {"kosten_usd": 0.01}
    monkeypatch.setattr(live, "run_claude", fake_claude)
    lm.analyse(sid)
    s = lm.get(sid)
    assert "#0 [" in calls[0] and "Der Kunde stellt einen Antrag." in calls[0]
    assert [e["text"] for e in s["eintraege"]] == ["Wie kommt der Antrag rein?"]
    assert s["analysiert_bis"] == 0 and s["laeufe"][-1]["art"] == "analyse"
