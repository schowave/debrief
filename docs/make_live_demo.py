"""Records docs/live-demo.gif: `mise run demo` (needs Google Chrome and ffmpeg).

Uses the scratch server of `tests/smoke.py` (fixture MacWhisper database of a running recording, model stub), so the
animation shows invented data only and never touches `live/`, the vault or a model. Lines are added to the recording
while the page is open, the stub answers with questions, assumptions and a small process, then an instruction and a
review finding change the board.
"""
import datetime as dt
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import live  # noqa: E402
import smoke  # noqa: E402
from livedata import SID, SPK, add_line  # noqa: E402

OUT = ROOT / "docs" / "live-demo.gif"
SPK2 = uuid.UUID(int=3).bytes
LINES = [  # (speaker, text)
    (SPK, "Let's walk through how a customer applies for a new contract."),
    (SPK2, "The customer fills in the application in the portal and submits it."),
    (SPK2, "Then the back office checks the documents and approves the application."),
    (SPK, "And after the approval?"),
    (SPK2, "The customer gets a confirmation email and the contract is created in SAP."),
    (SPK2, "What happens on a rejection, we haven't really agreed on yet."),
]


def stub(cfg, system, prompt, schema, label, model=None, dirs=None):
    ids = re.findall(r"^\s{2}(\w+) \[(\w+)\] (.+)$", prompt, re.M)

    def ref(text: str) -> str:  # id of the element as listed in the prompt
        return next(i for i, _, t in ids if text in t)
    time.sleep(1.2)  # long enough for the skeleton cards to be seen
    if label == "Live analysis":
        els = [("actor", "Customer", "Customer"), ("command", "Customer", "Submit application"),
               ("event", "Customer", "Application submitted"), ("command", "Back office", "Check documents"),
               ("event", "Back office", "Application approved"), ("policy", "Back office", "Whenever approved: create contract"),
               ("system", "Back office", "SAP")]
        return {"eintraege": [
            {"typ": "frage", "text": "What happens when the application is rejected?", "begruendung": "The rejection path is still open",
             "prio": 1, "zeilen": [5], "elemente": ["e4"]},
            {"typ": "frage", "text": "How long may the document check take?", "begruendung": "No deadline mentioned",
             "prio": 2, "zeilen": [2], "elemente": ["e3"]},
            {"typ": "frage", "text": "Who is notified if SAP is not reachable?", "begruendung": "External system without error path",
             "prio": 3, "zeilen": [4], "elemente": ["e6"]},
            {"typ": "annahme", "text": "The contract is only created after the approval", "begruendung": "Order in line 5",
             "zeilen": [4], "elemente": ["e5"]},
            {"typ": "annahme", "text": "Customers can upload missing documents later", "begruendung": "Not said, but usual",
             "zeilen": [2], "elemente": []},
            {"typ": "offen", "text": "Rejection path", "begruendung": "Not agreed yet", "zeilen": [5], "elemente": []}],
            "erledigt": [], "recherche": [], "anweisungen": [], "aenderungen": [
            {"op": "prozess_neu", "ref": "p", "text": "Contract application"},
            *[{"op": "neu", "prozess": "p", "ref": f"e{i}", "typ": t, "text": x, "lane": lane} for i, (t, lane, x) in enumerate(els)],
            *[{"op": "verbinden", "von": f"e{i}", "nach": f"e{i + 1}"} for i in range(1, 6)]]}, {"kosten_usd": 0}
    if label == "Live process edit":
        return {"antwort": "Policy inserted.", "operationen": [
            {"op": "element_neu", "typ": "policy", "text": "Whenever documents are missing: request them", "lane": "Back office",
             "nach": ref("Check documents")}]}, {"kosten_usd": 0}
    if label == "Live review":
        return {"zusammenfassung": "The happy path is complete; the rejection is missing.", "befunde": [
            {"kategorie": "sonderfaelle", "schwere": "hoch", "elemente": [ref("Application approved")],
             "beobachtung": "No rejection is modelled.",
             "empfehlung": "Add the rejection path.", "frage": "What happens when the application is rejected?",
             "aenderung": [{"op": "element_neu", "typ": "event", "text": "Application rejected", "lane": "Back office",
                            "nach": ref("Check documents")}]},
            {"kategorie": "daten_systeme", "schwere": "mittel", "elemente": [ref("SAP")], "beobachtung": "Which data goes to SAP is unclear.",
             "empfehlung": "Add a read model for the contract data."}]}, {"kosten_usd": 0}
    return smoke.model_stub(cfg, system, prompt, schema, label, model, dirs)


def main() -> int:
    from playwright.sync_api import sync_playwright
    tmp = Path(tempfile.mkdtemp(prefix="debrief-demo-"))
    smoke.TEXTS = []  # the recording starts empty, lines arrive while the page is open
    url, srv, lm = smoke.start_server(tmp)
    live.run_claude = stub
    lm.cfg["model"] = "sonnet"  # shown in the header; the stub answers anyway
    db = tmp / "main.sqlite"
    con = sqlite3.connect(db)
    con.execute("update recordedmeeting set matchedCalendarTitle = 'Contract process workshop'")
    con.execute("update speaker set name = 'Erika Mustermann' where id = ?", (SPK,))
    con.execute("insert into speaker values (?, 'Max Muster')", (SPK2,))
    con.commit()
    con.close()
    now = dt.datetime.now(dt.timezone.utc)
    frames = tmp / "frames"
    frames.mkdir()
    n = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 880}, device_scale_factor=1, locale="en-GB")
        page.on("dialog", lambda d: d.accept())
        page.add_init_script("addEventListener('DOMContentLoaded', () => document.head.insertAdjacentHTML('beforeend',"
                             " '<style>#board { zoom: .72 }</style>'))")  # the whole process fits the board column

        def shot(hold: float = 1.0):
            nonlocal n
            for _ in range(max(1, round(hold * 2))):  # 2 frames per second
                page.screenshot(path=frames / f"{n:04d}.png")
                n += 1

        def say(i: int):
            spk, text = LINES[i]
            stamp = (now + dt.timedelta(seconds=i + 1)).strftime("%Y-%m-%d %H:%M:%S.000")
            add_line(db, i, text, i * 5000, stamp, spk=spk, order=None)
            page.wait_for_function(f"document.querySelectorAll('.ln').length >= {i + 1}", timeout=20000)

        page.goto(f"{url}/live#{SID.hex().upper()}")
        page.wait_for_function("typeof S !== 'undefined' && S", timeout=20000)
        for i in range(len(LINES)):
            say(i)
            shot(0.6)
        shot(1)
        page.click("[data-start]")
        page.wait_for_function("S.arbeit", timeout=20000)
        shot(1)
        page.wait_for_function("S.laeufe.length >= 1 && !S.arbeit && document.querySelectorAll('.sticky').length >= 7", timeout=20000)
        shot(4)
        page.fill("#pcmd-text", "Insert a policy for missing documents after »Check documents«")
        shot(1.5)
        page.press("#pcmd-text", "Enter")
        page.wait_for_function("document.querySelectorAll('.sticky').length >= 8", timeout=20000)
        shot(2.5)
        page.click("#b-review")
        page.evaluate("document.querySelectorAll('#review-body details').forEach(d => d.open = false)")  # long German prompt
        shot(1)
        page.click("#rv-go")
        page.wait_for_function("document.querySelectorAll('#review-body .finding').length === 2", timeout=20000)
        shot(3)
        page.click("#review-body .finding [data-act=anwenden]")
        page.wait_for_timeout(600)
        shot(1.5)
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        shot(2)
        page.goto(f"{url}/live?nur=prozess#{SID.hex().upper()}")  # "Own window": the whole board
        page.wait_for_function("typeof S !== 'undefined' && S && document.querySelectorAll('.sticky').length >= 9", timeout=20000)
        page.wait_for_timeout(400)
        shot(4)
        browser.close()
    srv.shutdown()

    palette = tmp / "palette.png"
    scale = "fps=2,scale=1280:-1:flags=lanczos"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", "2", "-i", frames / "%04d.png",
                    "-vf", f"{scale},palettegen=stats_mode=diff", palette], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", "2", "-i", frames / "%04d.png", "-i", palette,
                    "-lavfi", f"{scale}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle", OUT],
                   check=True)
    shutil.rmtree(frames, ignore_errors=True)  # the rest stays: the live threads still read the database
    print(f"{OUT.relative_to(ROOT)}: {n} frames, {OUT.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
