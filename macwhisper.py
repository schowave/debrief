"""Read-only access to MacWhisper's local database (live transcripts of recorded meetings).

MacWhisper stores every recording as a `session`; meeting recordings link to `recordedmeeting`. The lines
of a transcript are in `transcriptline` and are still rewritten shortly after they appear (`isConfirmed`,
`dateUpdated`). Times in the database are UTC; a line's wall-clock time is the session start plus `start` ms.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

DB = Path.home() / "Library/Application Support/MacWhisper/Database/main.sqlite"
FMT = "%Y-%m-%d %H:%M:%S.%f"
# A session whose transcription never finished (crash) would count as running forever
MAX_RUNNING_IDLE_S = 15 * 60

MEETING_SQL = """
select hex(s.id) sid, s.dateCreated, s.playbackDuration, s.transcriptionDidSucceed,
       coalesce(s.userChosenTitle, m.matchedCalendarTitle, m.title, s.aiTitle) title, m.appName,
       (select count(*) from transcriptline t where t.sessionId = s.id) lines,
       (select max(t.dateUpdated) from transcriptline t where t.sessionId = s.id) last_update
from session s join recordedmeeting m on m.id = s.recordedMeetingID
where s.dateDeleted is null
"""


def connect(db: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db or DB}?mode=ro", uri=True, timeout=5, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def wal_stamp(db: Path | None = None) -> tuple:
    """Cheap change indicator: MacWhisper writes into the WAL file first."""
    db = db or DB
    out = []
    for p in (db, db.with_name(db.name + "-wal")):
        try:
            st = p.stat()
            out.append((st.st_mtime_ns, st.st_size))
        except FileNotFoundError:
            out.append(None)
    return tuple(out)


def to_local(utc: str) -> dt.datetime:
    return dt.datetime.strptime(utc[:23].ljust(23, "0"), FMT).replace(tzinfo=dt.timezone.utc).astimezone()


def _meeting(r: sqlite3.Row, now: dt.datetime) -> dict:
    start = to_local(r["dateCreated"])
    last = to_local(r["last_update"]) if r["last_update"] else start
    running = r["transcriptionDidSucceed"] is None and (now - last).total_seconds() < MAX_RUNNING_IDLE_S
    return {"id": r["sid"], "start": start.isoformat(timespec="seconds"), "titel": r["title"] or "(ohne Titel)",
            "app": r["appName"], "laeuft": running, "zeilen": r["lines"],
            "dauer_s": round(r["playbackDuration"] or 0)}


def meetings(con: sqlite3.Connection, limit: int = 15, now: dt.datetime | None = None) -> list[dict]:
    now = now or dt.datetime.now().astimezone()
    rows = con.execute(MEETING_SQL + " order by s.dateCreated desc limit ?", (limit,))
    return [_meeting(r, now) for r in rows]


def meeting(con: sqlite3.Connection, sid: str, now: dt.datetime | None = None) -> dict | None:
    r = con.execute(MEETING_SQL + " and hex(s.id) = upper(?)", (sid,)).fetchone()
    return _meeting(r, now or dt.datetime.now().astimezone()) if r else None


def running(con: sqlite3.Connection, now: dt.datetime | None = None) -> dict | None:
    """The meeting that is being recorded right now (newest first)."""
    return next((m for m in meetings(con, 3, now) if m["laeuft"]), None)


def speakers(con: sqlite3.Connection, sid: str) -> dict[str, str]:
    """Speaker id (hex, "" without speaker) -> label: the name from MacWhisper or "Speaker N" by first appearance."""
    labels = {"": "?"}
    rows = con.execute(
        """select hex(t.speakerID) spk, sp.name from transcriptline t left join speaker sp on sp.id = t.speakerID
           where t.sessionId = unhex(?) and t.speakerID is not null group by t.speakerID order by min(t.start)""",
        (sid,),
    )
    for n, r in enumerate(rows, 1):
        labels[r["spk"]] = r["name"] or f"Speaker {n}"
    return labels


def line_ids(con: sqlite3.Connection, sid: str) -> set[str]:
    """Ids of the session's current lines. While recording, MacWhisper deletes unconfirmed lines and writes
    confirmed ones under new ids; an incremental read by `dateUpdated` does not see the deletions."""
    return {r[0] for r in con.execute("select hex(id) from transcriptline where sessionId = unhex(?)", (sid,))}


def lines(con: sqlite3.Connection, sid: str, since: str | None = None) -> list[dict]:
    """Lines of a session; with `since` (a `dateUpdated` value) only those changed afterwards. `stand` is the
    value to pass as `since` next time. `nr` is MacWhisper's orderIndex, which stays empty while recording."""
    rows = con.execute(
        """select hex(id) lid, orderIndex, start, end, text, hex(speakerID) spk, isConfirmed, dateUpdated
           from transcriptline where sessionId = unhex(?) and (? is null or dateUpdated > ?)
           order by orderIndex, start""",
        (sid, since, since),
    )
    return [{"id": r["lid"], "nr": r["orderIndex"], "start_ms": r["start"] or 0, "ende_ms": r["end"] or 0,
             "text": (r["text"] or "").strip(), "sprecher": r["spk"] or "",
             "bestaetigt": bool(r["isConfirmed"]) and not (r["text"] or "").rstrip().endswith("…"),
             "stand": r["dateUpdated"]} for r in rows]
