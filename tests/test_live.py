"""Tests for the live assistant: MacWhisper reader, merge rules, lifecycle, process library. `mise run test`."""
import datetime as dt
import time
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import live  # noqa: E402
import macwhisper as mw  # noqa: E402
from livedata import SID, SPK, add_line, make_db, running_recording  # noqa: E402


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


def test_running_recording_without_order_index_gets_numbered_by_time(tmp_path, db):
    """MacWhisper leaves orderIndex empty while recording; lines are numbered by start time instead."""
    add_line(db, 1, "Zweiter Satz", 3000, "2026-10-30 12:00:04.000", order=None)
    add_line(db, 0, "Erster Satz", 1000, "2026-10-30 12:00:02.000", order=None)
    lm = live.LiveManager({"kategorien": {}}, db=db, directory=tmp_path / "live")
    zeilen, _ = lm.lines(SID.hex().upper())
    assert [(z["nr"], z["text"]) for z in zeilen] == [(0, "Erster Satz"), (1, "Zweiter Satz")]
    # MacWhisper replaces an unconfirmed line by a confirmed one under a new id
    add_line(db, 2, "Dritter …", 5000, "2026-10-30 12:00:06.000", confirmed=0, order=None)
    assert [z["nr"] for z in lm.lines(SID.hex().upper())[0]] == [0, 1, 2]
    con = sqlite3.connect(db)
    con.execute("delete from transcriptline where id = ?", (uuid.UUID(int=102).bytes,))
    con.commit()
    con.close()
    add_line(db, 3, "Dritter Satz.", 5000, "2026-10-30 12:00:08.000", order=None)
    zeilen, _ = lm.lines(SID.hex().upper())
    assert [(z["nr"], z["text"]) for z in zeilen] == [(0, "Erster Satz"), (1, "Zweiter Satz"), (2, "Dritter Satz.")]


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


def test_merged_processes_share_lanes_and_are_not_re_created():
    s = live.new_session({"id": "C" * 32, "titel": "T", "start": "2026-10-30T13:00:00+01:00"}, {})
    live.apply_analysis(s, {"aenderungen": [
        {"op": "prozess_neu", "ref": "a", "text": "R&D-Manager legt Projekt an"},
        {"op": "neu", "prozess": "a", "typ": "command", "text": "Projekt anlegen", "lane": "R&D Manager"},
        {"op": "prozess_neu", "ref": "b", "text": "Agile-Manager pflegt Kapazitäten"},
        {"op": "neu", "prozess": "b", "typ": "command", "text": "Kapazität pflegen", "lane": "Agile Manager"},
    ]}, 1)
    a, b = s["prozesse"]
    live.edit_process(s, {"op": "prozesse_zusammenfuehren", "ids": [a["id"], b["id"]], "name": "Projekt bis Kapazität"})
    assert [p["name"] for p in s["prozesse"]] == ["Projekt bis Kapazität"]
    assert [e["text"] for e in s["prozesse"][0]["elemente"]] == ["Projekt anlegen", "Kapazität pflegen"]
    live.apply_analysis(s, {"aenderungen": [
        {"op": "prozess_neu", "ref": "x", "text": "Agile-Manager pflegt Kapazitäten"},
        {"op": "neu", "prozess": "x", "typ": "event", "text": "Kapazität gepflegt", "lane": "Agile Manager"},
    ]}, 2)
    assert len(s["prozesse"]) == 1 and s["prozesse"][0]["elemente"][-1]["text"] == "Kapazität gepflegt"
    assert "Agile-Manager pflegt Kapazitäten" in live.process_prompt(s)


def test_failed_summary_does_not_stop_the_analysis(tmp_path, db, monkeypatch):
    for i in range(3):
        add_line(db, i, f"Satz {i}", i * 20 * 60_000, f"2026-10-30 12:{i:02d}:01.000")  # 0, 20 and 40 minutes in
    lm = live.LiveManager({"kategorien": {}, "kategorien_datei": tmp_path / "c.yaml", "ich": "Ich"}, db=db, directory=tmp_path / "live")
    sid = SID.hex().upper()
    monkeypatch.setattr(lm, "_calendar", lambda sid: None)
    lm.open_session(sid)

    def fake_claude(cfg, system, prompt, schema, label, model=None, dirs=None):
        if label == "Live summary":
            raise RuntimeError("claude failed (1): gateway timeout")
        return {"eintraege": [], "erledigt": [], "aenderungen": [], "recherche": [], "anweisungen": []}, {"kosten_usd": 0}
    monkeypatch.setattr(live, "run_claude", fake_claude)
    lm.analyse(sid)
    s = lm.get(sid)
    assert [(r["art"], bool(r["fehler"])) for r in s["laeufe"]] == [("zusammenfassung", True), ("analyse", False)]
    assert s["fehler"] is None and s["analysiert_bis"] == 2


def test_muted_ranges_and_speakers_never_reach_the_analysis():
    s = live.new_session({"id": "E" * 32, "titel": "T", "start": "2026-10-30T13:00:00+01:00"}, {})
    labels = {"a": "Speaker 1", "b": "Speaker 2"}
    zeilen = [{"id": str(i), "nr": i, "start_ms": i * 10_000, "sprecher": "a" if i % 2 else "b", "text": "x"} for i in range(6)]
    live.exclude_range(s, 10_000, 30_000)                       # paragraph switched off
    assert [z["nr"] for z in live.listened(s, zeilen, labels)] == [0, 3, 4, 5]
    live.include_range(s, 10_000, 20_000)                       # one line back in
    assert s["ausgenommen"] == [{"von_ms": 20_000, "bis_ms": 30_000}]
    live.set_speaker_muted(s, "Speaker 1", True)
    assert [z["nr"] for z in live.listened(s, zeilen, labels)] == [0, 4]
    live.set_speaker_muted(s, "Speaker 1", False)
    live.set_listening(s, False, 45_000)                        # "do not listen" from now on, 3 s early
    assert live.is_muted(s) and [z["nr"] for z in live.listened(s, zeilen, labels)] == [0, 1, 3, 4]
    live.set_listening(s, True, 60_000)
    assert not live.is_muted(s) and s["ausgenommen"][-1] == {"von_ms": 42_000, "bis_ms": 57_000}
    live.set_listening(s, True, 61_000)                         # already listening: nothing changes
    assert not live.is_muted(s)


def test_muted_lines_are_not_sent_to_the_model(tmp_path, db, monkeypatch):
    add_line(db, 0, "Das ist wichtig.", 0, "2026-10-30 12:00:01.000")
    add_line(db, 1, "Das ist privat.", 5000, "2026-10-30 12:00:07.000")
    lm = live.LiveManager({"kategorien": {}, "kategorien_datei": tmp_path / "c.yaml", "ich": "Ich"}, db=db, directory=tmp_path / "live")
    sid = SID.hex().upper()
    monkeypatch.setattr(lm, "_calendar", lambda sid: None)
    lm.open_session(sid)
    lm.update(sid, lambda s: live.exclude_range(s, 4000, 6000))
    prompts = []

    def fake_claude(cfg, system, prompt, schema, label, model=None, dirs=None):
        prompts.append(prompt)
        return {"eintraege": [], "erledigt": [], "aenderungen": [], "recherche": [], "anweisungen": []}, {"kosten_usd": 0}
    monkeypatch.setattr(live, "run_claude", fake_claude)
    lm.analyse(sid)
    assert "Das ist wichtig." in prompts[0] and "Das ist privat." not in prompts[0]


def test_ended_session_can_be_resumed_and_stays_open(tmp_path, db, monkeypatch):
    add_line(db, 0, "Hallo.", 0, "2026-10-30 12:00:01.000")
    lm = live.LiveManager({"kategorien": {}, "kategorien_datei": tmp_path / "c.yaml", "ich": "Ich"}, db=db, directory=tmp_path / "live")
    sid = SID.hex().upper()
    monkeypatch.setattr(lm, "_calendar", lambda sid: None)
    monkeypatch.setattr(lm, "trigger", lambda sid: None)
    lm.open_session(sid)
    lm.update(sid, lambda s: s.update(beendet=True, analysiert_bis=0, laeuft=True))
    lm.is_running = lambda s: False                        # the recording is over (or paused for a long time)
    lm._schedule()
    assert lm.get(sid)["laeuft"] is False                  # an ended session notices it too
    lm.start(sid, "fortsetzen")
    lm._schedule()
    s = lm.get(sid)
    assert s["aktiv"] and s["offen"] and not s["beendet"]  # not ended again at once
    lm.update(sid, lambda s2: s2.update(aktiv=False, offen=False))
    lm.start(sid, "live")
    lm._schedule()
    assert lm.get(sid)["beendet"]                          # a normal start still ends with the recording


SID2 = uuid.UUID(int=3).bytes
SPK2 = uuid.UUID(int=4).bytes


def add_second_recording(db: Path, start: str = "2026-10-30 12:30:00.000", running: bool = True) -> None:
    con = sqlite3.connect(db)
    con.execute("insert into session values (?, ?, 0, ?, ?, null, null, null)", (SID2, start, None if running else 1, b"m"))
    con.execute("insert into speaker values (?, null)", (SPK2,))
    for i, text in enumerate(["Weiter geht es.", "Genau, nach der Pause."]):
        con.execute("insert into transcriptline values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (uuid.UUID(int=900 + i).bytes, start, "2026-10-30 12:30:05.000", text, i * 3000, i * 3000 + 2000, SID2, SPK2, None, 1))
    con.commit()
    con.close()


def attach_setup(tmp_path, db, monkeypatch):
    add_line(db, 0, "Erste Aufnahme.", 0, "2026-10-30 12:00:01.000")
    add_second_recording(db)
    lm = live.LiveManager({"kategorien": {}, "kategorien_datei": tmp_path / "c.yaml", "ich": "Ich"}, db=db, directory=tmp_path / "live")
    monkeypatch.setattr(lm, "_calendar", lambda sid: None)
    lm.open_session(SID.hex().upper())
    return lm


def test_attached_recording_continues_the_time_line(tmp_path, db, monkeypatch):
    lm = attach_setup(tmp_path, db, monkeypatch)
    sid, sid2 = SID.hex().upper(), SID2.hex().upper()
    s = lm.attach(sid, sid2)
    assert s["aufnahmen"][0]["offset_ms"] == 30 * 60_000 and s["aktiv"]
    zeilen, labels = lm.visible_lines(s)
    assert [(z["nr"], z["start_ms"], z["text"]) for z in zeilen] == [
        (0, 0, "Erste Aufnahme."), (1, 1_800_000, "Weiter geht es."), (2, 1_803_000, "Genau, nach der Pause.")]
    assert labels[SPK.hex().upper()] == "Speaker 1" and labels[SPK2.hex().upper() + "@2"] == "Speaker 1 (2)"
    assert zeilen[1]["sprecher"] != zeilen[0]["sprecher"]  # same "Speaker 1" in both recordings, different people
    with pytest.raises(ValueError):
        lm.attach(sid, sid2)  # only once
    # the transcript that MacWhisper exports for either recording finds the session; names follow the recording
    assert lm.session_for_recording("2026-10-30T12:30:30+00:00")["id"] == sid
    lm.update(sid, lambda s2: s2["sprecher_namen"].update({"Speaker 1": "Erika", "Speaker 1 (2)": "Max"}))
    assert live.names_for_recording(lm.get(sid), dt.datetime(2026, 10, 30, 12, 30, 10, tzinfo=dt.timezone.utc)) == {"Speaker 1": "Max"}
    assert live.names_for_recording(lm.get(sid), dt.datetime(2026, 10, 30, 12, 0, 5, tzinfo=dt.timezone.utc)) == {"Speaker 1": "Erika"}


def test_attaching_ends_the_extra_session_and_a_finished_recording_keeps_it_open(tmp_path, db, monkeypatch):
    lm = attach_setup(tmp_path, db, monkeypatch)
    sid, sid2 = SID.hex().upper(), SID2.hex().upper()
    lm.open_session(sid2)  # the new recording got a session of its own when it was opened
    lm.update(sid2, lambda s: s.update(aktiv=True))
    con = sqlite3.connect(db)
    con.execute("update session set transcriptionDidSucceed = 1 where id = ?", (SID2,))
    con.commit()
    con.close()
    s = lm.attach(sid, sid2)
    assert s["offen"]  # nothing is being recorded any more: stays open for questions until stopped
    assert lm.get(sid2)["aufgegangen_in"] == sid and not lm.get(sid2)["aktiv"]
    assert not lm.is_running(s)


def test_new_recording_is_offered_as_continuation(tmp_path, db, monkeypatch):
    lm = attach_setup(tmp_path, db, monkeypatch)
    lm.update(SID.hex().upper(), lambda s: s.update(letzter_lauf=dt.datetime(2026, 10, 30, 12, 20, tzinfo=dt.timezone.utc).timestamp()))
    new = next(m for m in lm.meetings() if m["id"] == SID2.hex().upper())
    assert new["fortsetzung_von"] == {"id": SID.hex().upper(), "titel": "Prozess-Workshop"}
    lm.attach(SID.hex().upper(), SID2.hex().upper())
    assert not any(m.get("fortsetzung_von") for m in lm.meetings())  # attached: no further offer


def regen_setup(tmp_path, db, monkeypatch):
    add_line(db, 0, "Der Kunde stellt einen Antrag.", 0, "2026-10-30 12:00:01.000")
    lm = live.LiveManager({"kategorien": {}, "kategorien_datei": tmp_path / "c.yaml", "ich": "Ich"}, db=db, directory=tmp_path / "live")
    sid = SID.hex().upper()
    monkeypatch.setattr(lm, "_calendar", lambda sid: None)
    lm.open_session(sid)

    def build(s):
        live.apply_analysis(s, {"aenderungen": [
            {"op": "prozess_neu", "ref": "p", "text": "Antrag"},
            {"op": "neu", "prozess": "p", "ref": "a", "typ": "command", "text": "Antrag stellen", "lane": "Kunde"},
            {"op": "neu", "prozess": "p", "ref": "b", "typ": "event", "text": "Antrag gestellt", "lane": "Kunde"},
            {"op": "neu", "prozess": "p", "ref": "c", "typ": "command", "text": "Alte Prüfung", "lane": "Fachabteilung"},
        ], "eintraege": [{"typ": "frage", "text": "Wer prüft?", "elemente": ["c"]}]}, 1)
        p = s["prozesse"][0]
        live.edit_process(s, {"op": "sperren", "id": p["elemente"][0]["id"]})  # "Antrag stellen" is yours
    lm.update(sid, build)
    return lm, sid


def test_rebuild_keeps_locked_elements_and_relinks_questions(tmp_path, db, monkeypatch):
    lm, sid = regen_setup(tmp_path, db, monkeypatch)
    prompts, working = [], []
    p = lm.get(sid)["prozesse"][0]
    locked_id = p["elemente"][0]["id"]

    def fake_claude(cfg, system, prompt, schema, label, model=None, dirs=None):
        prompts.append(prompt)
        working.append(lm.get(sid)["arbeit"])
        return {"eintraege": [], "erledigt": [], "recherche": [], "anweisungen": [], "aenderungen": [
            {"op": "neu", "prozess": p["id"], "ref": "n", "typ": "command", "text": "Alte Prüfung", "lane": "Sachbearbeiter", "nach": locked_id},
            {"op": "neu", "prozess": p["id"], "ref": "m", "typ": "event", "text": "Antrag geprüft", "lane": "Sachbearbeiter", "nach": "n"},
            {"op": "verbinden", "von": locked_id, "nach": "n"}]}, {"kosten_usd": 0}
    monkeypatch.setattr(live, "run_claude", fake_claude)
    lm.regenerate(sid, None, neu_aufbauen=True, hinweis="Die Prüfung macht der Sachbearbeiter", blocking=True)
    s = lm.get(sid)
    texts = [e["text"] for e in s["prozesse"][0]["elemente"]]
    assert texts == ["Antrag stellen", "Alte Prüfung", "Antrag geprüft"]
    assert "Antrag gestellt" not in prompts[0] and "Antrag stellen" in prompts[0] and "Die Prüfung macht der Sachbearbeiter" in prompts[0]
    frage = s["eintraege"][0]
    assert frage["elemente"] == [s["prozesse"][0]["elemente"][1]["id"]]  # the question points at the rebuilt element
    assert s["laeufe"][-1]["art"] == "prozess" and not s["analyse_laeuft"] and sid not in lm._busy
    assert working == ["neu_erzeugen"] and s["arbeit"] is None  # skeletons while it runs, gone afterwards


def test_revise_changes_in_place_and_a_failure_changes_nothing(tmp_path, db, monkeypatch):
    lm, sid = regen_setup(tmp_path, db, monkeypatch)
    p = lm.get(sid)["prozesse"][0]
    victim = p["elemente"][2]["id"]
    monkeypatch.setattr(live, "run_claude", lambda *a, **k: ({"eintraege": [], "erledigt": [], "recherche": [], "anweisungen": [],
                        "aenderungen": [{"op": "loeschen", "element": victim, "begruendung": "gilt nicht mehr"}]}, {"kosten_usd": 0}))
    lm.regenerate(sid, p["id"], blocking=True)
    assert [e["text"] for e in lm.get(sid)["prozesse"][0]["elemente"]] == ["Antrag stellen", "Antrag gestellt"]

    def boom(*a, **k):
        raise RuntimeError("gateway timeout")
    monkeypatch.setattr(live, "run_claude", boom)
    before = lm.get(sid)["prozesse"]
    lm.regenerate(sid, None, neu_aufbauen=True, blocking=True)
    s = lm.get(sid)
    assert s["prozesse"] == before and "gateway timeout" in s["fehler"] and not s["analyse_laeuft"]
    lm._busy.add(sid)
    with pytest.raises(ValueError):
        lm.regenerate(sid)  # an analysis is already running


def test_instruction_edits_process_as_own_locked_edits_and_can_be_undone(tmp_path, db, monkeypatch):
    lm, sid = regen_setup(tmp_path, db, monkeypatch)
    p = lm.get(sid)["prozesse"][0]
    last = p["elemente"][2]["id"]
    seen = []

    def fake_claude(cfg, system, prompt, schema, label, model=None, dirs=None):
        seen.append(prompt)
        return {"antwort": "Policy eingefügt, Lane umbenannt.", "operationen": [
            {"op": "element_neu", "ref": "n", "prozess": p["id"], "typ": "policy", "text": "Wenn Unterlagen fehlen, nachfordern",
             "lane": "Fachabteilung", "nach": p["elemente"][1]["id"]},
            {"op": "verbinden", "von": "n", "nach": last},
            {"op": "element_aendern", "id": last, "lane": "Prüfstelle"},
            {"op": "element_loeschen", "id": "gibt-es-nicht"}]}, {"kosten_usd": 0}
    monkeypatch.setattr(live, "run_claude", fake_claude)
    r = lm.process_command(sid, "Füge nach „Antrag gestellt“ eine Policy ein und benenne die Lane um", p["id"])
    assert r["angewendet"] == 3 and len(r["fehler"]) == 1 and "Policy" in r["antwort"]
    s = lm.get(sid)
    els = {e["text"]: e for e in s["prozesse"][0]["elemente"]}
    assert list(els) == ["Antrag stellen", "Antrag gestellt", "Wenn Unterlagen fehlen, nachfordern", "Alte Prüfung"]
    assert els["Wenn Unterlagen fehlen, nachfordern"]["gesperrt"] and els["Alte Prüfung"]["lane"] == "Prüfstelle"
    assert {"von": els["Wenn Unterlagen fehlen, nachfordern"]["id"], "nach": last} in s["prozesse"][0]["verbindungen"]
    assert "Antrag gestellt" in seen[0] and s["rueckgaengig"]
    lm.undo_command(sid)
    s = lm.get(sid)
    assert [e["text"] for e in s["prozesse"][0]["elemente"]] == ["Antrag stellen", "Antrag gestellt", "Alte Prüfung"]
    assert s["prozesse"][0]["elemente"][2]["lane"] == "Fachabteilung" and s["rueckgaengig"] is None
    with pytest.raises(ValueError):
        lm.undo_command(sid)  # one step only
    monkeypatch.setattr(live, "run_claude", lambda *a, **k: ({"antwort": "Unklar.", "operationen": []}, {"kosten_usd": 0}))
    r = lm.process_command(sid, "Mach es besser")
    assert r["angewendet"] == 0 and lm.get(sid)["rueckgaengig"] is None  # nothing changed: nothing to undo


def test_review_reports_findings_and_you_decide_on_each(tmp_path, db, monkeypatch):
    lm, sid = regen_setup(tmp_path, db, monkeypatch)
    monkeypatch.setattr(live, "EINSTELLUNGEN", tmp_path / "settings.json")
    p = lm.get(sid)["prozesse"][0]
    ev, cmd = p["elemente"][1]["id"], p["elemente"][2]["id"]
    prompts = []

    def fake_claude(cfg, system, prompt, schema, label, model=None, dirs=None):
        prompts.append((system, prompt))
        return {"zusammenfassung": "Der Ablauf hat Lücken.", "befunde": [
            {"kategorie": "sonderfaelle", "schwere": "mittel", "elemente": [ev, "fremd"], "beobachtung": "Keine Ablehnung modelliert.",
             "empfehlung": "Ablehnungspfad ergänzen.", "frage": "Was passiert bei Ablehnung?",
             "aenderung": [{"op": "element_neu", "prozess": p["id"], "typ": "event", "text": "Antrag abgelehnt", "lane": "Fachabteilung", "nach": cmd}]},
            {"kategorie": "sprache", "schwere": "hoch", "elemente": [cmd], "beobachtung": "„Alte Prüfung“ ist kein Command.",
             "empfehlung": "Imperativ verwenden."}]}, {"kosten_usd": 0}
    monkeypatch.setattr(live, "run_claude", fake_claude)
    lm.review(sid, None, "Achte nur auf Sonderfälle.", als_standard=True, blocking=True)
    assert "Achte nur auf Sonderfälle." in prompts[0][0] and lm.review_default() == "Achte nur auf Sonderfälle."
    s = lm.get(sid)
    assert [b["schwere"] for b in s["review"]["befunde"]] == ["hoch", "mittel"]  # most severe first
    hoch, mittel = s["review"]["befunde"]
    assert mittel["elemente"] == [ev]  # unknown ids are dropped
    with pytest.raises(ValueError):
        lm.review_action(sid, hoch["id"], "anwenden")  # no change attached
    assert lm.review_action(sid, mittel["id"], "anwenden")["angewendet"] == 1
    lm.review_action(sid, mittel["id"], "frage")
    lm.review_action(sid, hoch["id"], "verwerfen")
    s = lm.get(sid)
    assert "Antrag abgelehnt" in [e["text"] for e in s["prozesse"][0]["elemente"]]
    assert s["eintraege"][-1]["text"] == "Was passiert bei Ablehnung?" and s["eintraege"][-1]["elemente"] == [ev]
    lm.review_action(sid, mittel["id"], "zuruecksetzen")
    lm.review_action(sid, mittel["id"], "frage")  # a second click does not add the question again
    assert [e["text"] for e in lm.get(sid)["eintraege"]].count("Was passiert bei Ablehnung?") == 1
    assert [b["status"] for b in s["review"]["befunde"]] == ["verworfen", "uebernommen"]
    lm.review(sid, None, blocking=True)
    assert "kein command" in prompts[1][1].lower()  # the dismissed finding is passed on, not reported again
    lm.undo_command(sid)  # the applied finding can be taken back
    assert "Antrag abgelehnt" not in [e["text"] for e in lm.get(sid)["prozesse"][0]["elemente"]]
    lm.save_settings({"weg": "direkt"})
    assert lm.review_default() == "Achte nur auf Sonderfälle."  # saving the model keeps the criteria


def test_answers_are_noticed_between_the_analyses(tmp_path, db, monkeypatch):
    add_line(db, 0, "Wird erst bezahlt oder erst bestellt?", 0, "2026-10-30 12:00:01.000")
    lm = live.LiveManager({"kategorien": {}, "kategorien_datei": tmp_path / "c.yaml", "ich": "Ich"}, db=db, directory=tmp_path / "live")
    sid = SID.hex().upper()
    monkeypatch.setattr(lm, "_calendar", lambda sid: None)
    lm.open_session(sid)

    def setup(s):
        live.apply_analysis(s, {"eintraege": [{"typ": "frage", "text": "Wird erst bezahlt oder erst bestellt?", "prio": 1}]}, 1)
        s.update(aktiv=True, analysiert_bis=0, letzter_lauf=time.time())  # the last full analysis was just now
    lm.update(sid, setup)
    frage = lm.get(sid)["eintraege"][0]["id"]
    calls = []

    def fake_claude(cfg, system, prompt, schema, label, model=None, dirs=None):
        calls.append(prompt)
        return {"beantwortet": [{"id": frage, "notiz": "Erst bestellen, danach bezahlen."}]}, {"kosten_usd": 0}
    monkeypatch.setattr(live, "run_claude", fake_claude)
    lm.check_answers(sid, blocking=True)
    assert not calls  # nothing new yet: no model call
    add_line(db, 1, "Die Bestellung wird aufgegeben und danach bezahlt.", 5000, "2026-10-30 12:00:09.000")
    lm.is_running = lambda s: True
    started = []
    monkeypatch.setattr(lm, "check_answers", lambda sid, blocking=False: started.append(sid) or True)
    lm.update(sid, lambda s: s.update(antwortpruefung=0.0))
    lm._schedule()  # the analysis is not due (it just ran), but a question is open and a new line is there
    assert started == [sid]
    assert live.LiveManager.check_answers(lm, sid, blocking=True)
    s = lm.get(sid)
    e = s["eintraege"][0]
    assert e["status"] == "beantwortet" and e["automatisch"] and e["notiz"] == "Erst bestellen, danach bezahlen."
    assert "Die Bestellung wird aufgegeben" in calls[0] and s["geprueft_bis"] == 1 and sid not in lm._busy
    assert not live.LiveManager.check_answers(lm, sid, blocking=True) and len(calls) == 1  # nothing open: no call


def test_new_session_in_the_same_recording_starts_where_it_is_now(tmp_path, db, monkeypatch):
    for nr, (text, ms) in enumerate([("Alt eins.", 0), ("Alt zwei.", 5000), ("Neu eins.", 12000), ("Neu zwei.", 20000)]):
        add_line(db, nr, text, ms, f"2026-10-30 12:00:{nr + 1:02d}.000")
    lm = live.LiveManager({"kategorien": {}, "kategorien_datei": tmp_path / "c.yaml", "ich": "Ich"}, db=db, directory=tmp_path / "live")
    sid = SID.hex().upper()
    monkeypatch.setattr(lm, "_calendar", lambda sid: None)
    monkeypatch.setattr(lm, "trigger", lambda sid: None)
    lm.open_session(sid)
    lm.update(sid, lambda s: (live.apply_analysis(s, {"eintraege": [{"typ": "frage", "text": "Alte Frage?", "prio": 1}]}, 1),
                              s["sprecher_namen"].update({"Speaker 1": "Erika"}), s.update(aktiv=True, analysiert_bis=1)))
    lm.is_running = lambda s: True
    lm.recording_ms = lambda s: 10_000  # the recording is 10 s in
    lm.update(sid, lambda s: s.update(erstellt="2026-10-30T12:00:00+00:00"))  # created earlier: `erstellt` only counts seconds
    n = lm.split(sid)
    old = lm.get(sid)
    assert old["beendet"] and not old["aktiv"] and old["bis_ms"] == 10_000
    assert n["aktiv"] and n["id"] != sid and n["ab_ms"] == 10_000 and n["eintraege"] == [] and n["sprecher_namen"] == {"Speaker 1": "Erika"}
    assert n["titel"] == f"Prozess-Workshop · ab {dt.datetime.fromisoformat(n['start']) + dt.timedelta(seconds=10):%H:%M}"
    new_lines, _ = lm.visible_lines(n)
    assert [(z["nr"], z["text"]) for z in new_lines] == [(0, "Neu eins."), (1, "Neu zwei.")]  # numbered from 0 again
    old_lines, _ = lm.visible_lines(old)
    assert [z["text"] for z in old_lines] == ["Alt eins.", "Alt zwei."]  # resuming the old one does not swallow the new part
    assert {m["id"]: m["titel"] for m in lm.meetings()}[n["id"]] == n["titel"]
    # the transcript of the whole recording is linked to the session you worked in last
    assert lm.session_for_recording("2026-10-30T12:00:30+00:00")["id"] == n["id"]
    with pytest.raises(ValueError):
        lm.is_running = lambda s: False
        lm.split(n["id"])  # the recording is not running


def test_running_recording_shape_goes_through_the_pipeline(tmp_path, db, monkeypatch):
    """No orderIndex, a trailing unconfirmed line that MacWhisper later replaces under a new id."""
    running_recording(db, ["Wir starten.", "Der Kunde stellt einen Antrag.", "Dann prüfen wir"], confirmed=False)
    lm = live.LiveManager({"kategorien": {}, "kategorien_datei": tmp_path / "c.yaml", "ich": "Ich"}, db=db, directory=tmp_path / "live")
    sid = SID.hex().upper()
    monkeypatch.setattr(lm, "_calendar", lambda sid: None)
    s = lm.open_session(sid)
    zeilen, _ = lm.visible_lines(s)
    assert [(z["nr"], z["bestaetigt"]) for z in zeilen] == [(0, True), (1, True), (2, False)]
    prompts = []

    def fake_claude(cfg, system, prompt, schema, label, model=None, dirs=None):
        prompts.append(prompt)
        return {"eintraege": [], "erledigt": [], "aenderungen": [], "recherche": [], "anweisungen": []}, {"kosten_usd": 0}
    monkeypatch.setattr(live, "run_claude", fake_claude)
    lm.analyse(sid)
    assert "Der Kunde stellt einen Antrag." in prompts[0] and "Dann prüfen wir" not in prompts[0]
    assert lm.get(sid)["analysiert_bis"] == 1  # the unconfirmed line is not counted as analysed
    con = sqlite3.connect(db)  # MacWhisper confirms: the unconfirmed row goes, a new one with the final text arrives
    con.execute("delete from transcriptline where text like 'Dann prüfen wir…'")
    con.commit()
    con.close()
    add_line(db, 7, "Dann prüfen wir die Unterlagen.", 8000, "2026-10-30 12:00:30.000", order=None)
    zeilen, _ = lm.visible_lines(lm.get(sid))
    assert [(z["nr"], z["text"], z["bestaetigt"]) for z in zeilen][2:] == [(2, "Dann prüfen wir die Unterlagen.", True)]
    lm.analyse(sid)
    assert "Dann prüfen wir die Unterlagen." in prompts[1] and lm.get(sid)["analysiert_bis"] == 2
