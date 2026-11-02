"""A MacWhisper database in miniature for the tests and for `tests/smoke.py`.

What a *running* recording looks like differs from a finished one, and the first real one broke the live page:
- `orderIndex` stays NULL until the recording is finished (order by start time instead),
- text that is not final yet ends with "…" and `isConfirmed` is 0,
- when a line is confirmed, MacWhisper deletes the unconfirmed row and writes a new row under a new id,
- `transcriptionDidSucceed` stays NULL, and a recording that stops getting lines counts as stale after a while.
`running_recording()` builds that shape; `add_line(..., order=None)` is its building block.
"""
import sqlite3
import uuid
from pathlib import Path

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


def add_line(db: Path, nr: int, text: str, start_ms: int, updated: str, confirmed: int = 1, spk=SPK,
             order: int | None | str = "nr") -> None:
    con = sqlite3.connect(db)
    con.execute("insert or replace into transcriptline values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uuid.UUID(int=100 + nr).bytes, updated, updated, text, start_ms, start_ms + 1000, SID, spk,
                 nr if order == "nr" else order, confirmed))
    con.commit()
    con.close()


def running_recording(db: Path, texts: list[str], start_ms: int = 0, step_ms: int = 4000, confirmed: bool = True,
                      updated: str = "2026-10-30 12:00:{s:02d}.000") -> None:
    """Lines as MacWhisper writes them while recording: no orderIndex, the last one still unconfirmed."""
    for i, text in enumerate(texts):
        last = i == len(texts) - 1
        pending = last and not confirmed
        add_line(db, i, text + ("…" if pending else ""), start_ms + i * step_ms, updated.format(s=min(i + 1, 59)),
                 confirmed=0 if pending else 1, order=None)

