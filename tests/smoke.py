"""Browser smoke test of the live page: `mise run smoke` (needs Google Chrome; not part of `mise run check`).

Starts a scratch server in this process - a fixture MacWhisper database with a *running* recording, a temporary
session folder and a stub instead of the model - and clicks through the main flows in a headless Chrome. It never
touches `live/`, the vault, `learned.json` or the gateway, so the same flows can be tried without risking real
sessions. Checks read the page's state with `evaluate`; screenshots are not needed.
"""
import datetime as dt
import re
import socket
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import debrief  # noqa: E402
import live  # noqa: E402
import server  # noqa: E402
from livedata import SID, make_db, running_recording  # noqa: E402

TEXTS = ["Der Kunde stellt einen Antrag im Portal.", "Die Sachbearbeitung prüft ihn und genehmigt ihn.",
         "Danach geht eine Mail an den Kunden.", "Was bei einer Ablehnung passiert, ist noch offen."]


def model_stub(cfg, system, prompt, schema, label, model=None, dirs=None):
    """Canned answers in the shape of the real ones, chosen by the label of the run."""
    ids = re.findall(r"^\s{2}(\w+) \[(\w+)\] (.+)$", prompt, re.M)  # elements as listed in the prompt
    if label == "Live analysis":
        els = [("actor", "Kunde", "Kunde"), ("command", "Kunde", "Antrag stellen"), ("event", "Kunde", "Antrag gestellt"),
               ("command", "Sachbearbeitung", "Antrag prüfen")]
        return {"eintraege": [
            {"typ": "frage", "text": "Was passiert bei einer Ablehnung?", "begruendung": "Pfad fehlt", "prio": 1, "zeilen": [3], "elemente": ["e3"]},
            {"typ": "frage", "text": "Wie lange darf die Prüfung dauern?", "begruendung": "Frist", "prio": 2, "zeilen": [1], "elemente": []}],
            "erledigt": [], "recherche": [], "anweisungen": [], "aenderungen": [
            {"op": "prozess_neu", "ref": "p", "text": "Antrag"},
            *[{"op": "neu", "prozess": "p", "ref": f"e{i}", "typ": t, "text": x, "lane": lane} for i, (t, lane, x) in enumerate(els)],
            {"op": "verbinden", "von": "e1", "nach": "e2"}]}, {"kosten_usd": 0}
    if label == "Live review":
        a, b = ids[1][0], ids[2][0]
        return {"zusammenfassung": "Der Ablauf hat eine Lücke.", "befunde": [
            {"kategorie": "sonderfaelle", "schwere": "hoch", "elemente": [b], "beobachtung": "Keine Ablehnung modelliert.",
             "empfehlung": "Ablehnungspfad ergänzen.", "frage": "Was passiert bei Ablehnung des Antrags?",
             "aenderung": [{"op": "element_neu", "typ": "event", "text": "Antrag abgelehnt", "lane": "Sachbearbeitung", "nach": b}]},
            {"kategorie": "sprache", "schwere": "niedrig", "elemente": [a], "beobachtung": "Akteur doppelt.", "empfehlung": "Zusammenführen."}]}, {"kosten_usd": 0}
    if label == "Live process edit":
        return {"antwort": "Policy eingefügt.", "operationen": [
            {"op": "element_neu", "typ": "policy", "text": "Bei fehlenden Daten nachfordern", "lane": "Sachbearbeitung", "nach": ids[1][0]}]}, {"kosten_usd": 0}
    if label == "Live answers":
        return {"beantwortet": []}, {"kosten_usd": 0}
    if label == "Live summary":
        return {"zusammenfassung": "Zusammenfassung."}, {"kosten_usd": 0}
    return {"eintraege": [], "erledigt": [], "recherche": [], "anweisungen": [], "aenderungen": []}, {"kosten_usd": 0}


def start_server(tmp: Path) -> tuple[str, ThreadingHTTPServer, live.LiveManager]:
    (tmp / "inbox").mkdir()
    (tmp / "c.yaml").write_text("{}")
    server.PENDING, server.TRASH, server.DUPLIKATE = tmp / "pending", tmp / "trash", tmp / "duplicates.json"
    debrief.FINGERABDRUECKE, debrief.GELERNT = tmp / "fingerprints.json", tmp / "learned.json"
    live.EINSTELLUNGEN = tmp / "settings.json"
    live.run_claude = model_stub
    now = dt.datetime.now(dt.timezone.utc)
    stamp = lambda ago: (now - dt.timedelta(seconds=ago)).strftime("%Y-%m-%d %H:%M:%S.000")  # noqa: E731
    db = make_db(tmp / "main.sqlite", running=True, start=stamp(60))
    running_recording(db, TEXTS, updated=stamp(5))  # no orderIndex, as while recording
    cfg = {"vault": tmp, "inbox": "inbox", "min_woerter": 30, "kategorien": {}, "kategorien_datei": tmp / "c.yaml",
           "ich": "Ich", "review": {"port": 0}, "model": "stub"}
    store = server.Store(cfg)
    lm = live.LiveManager(cfg, db=db, directory=tmp / "live")
    lm._calendar = lambda sid: None
    store.live = lm
    lm.start_threads()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = ThreadingHTTPServer(("127.0.0.1", port), server.make_handler(store, lm))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", srv, lm


def main() -> int:
    from playwright.sync_api import sync_playwright
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="debrief-smoke-"))
    url, srv, lm = start_server(tmp)
    sid = SID.hex().upper()

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("dialog", lambda d: d.accept())
        js = page.evaluate

        def step(name, fn):
            try:
                fn()
                print(f"ok    {name}")
            except Exception as e:  # noqa: BLE001 - report every failing step, not only the first
                failures.append(name)
                shot = tmp / f"fail-{len(failures)}.png"
                page.screenshot(path=shot)
                print(f"FAIL  {name}: {str(e).splitlines()[0][:200]}\n      screenshot: {shot}")

        def wait(expr: str, timeout: int = 20000):
            page.wait_for_function(expr, timeout=timeout)

        def transcript():
            page.goto(f"{url}/live#{sid}")
            wait("typeof S !== 'undefined' && S && document.querySelectorAll('.ln').length === 4")
            assert js("document.querySelectorAll('.ln.unconf').length") == 0, "all four lines are confirmed"
        step("running recording without orderIndex shows its lines", transcript)

        def analysis():
            page.click("[data-start]")
            wait("S.laeufe.length >= 1 && !S.arbeit")
            wait("document.querySelectorAll('.sticky').length === 4")
            assert js("document.querySelectorAll('#assist article.entry').length") >= 2
            assert js("document.querySelectorAll('#assist .imp').length") >= 2, "questions show their importance"
        step("analysis fills questions and board", analysis)

        def skeletons():
            n = js("(S.arbeit = 'analyse', renderAll(), document.querySelectorAll('.sk-card').length)")
            board = js("(S.arbeit = 'neu_erzeugen', renderAll(), !document.querySelector('#workbar').hidden)")
            js("(S.arbeit = null, renderAll())")
            assert n >= 1 and board, "skeleton cards and the work bar show while the assistant works"
        step("skeletons show while the assistant works", skeletons)

        def review():
            els = js("S.prozesse[0].elemente.length")
            page.click("#b-review")
            page.click("#rv-go")
            wait("document.querySelectorAll('#review-body .finding').length === 2")
            page.click("#review-body .finding [data-act=anwenden]")  # the first finding carries a fix
            wait(f"S.prozesse[0].elemente.length === {els + 1}")
            before = js("S.eintraege.length")
            page.click("#review-body .finding:not(.done) [data-act=frage]")
            wait("document.querySelectorAll('#review-body .finding.done').length === 2")
            assert js("S.eintraege.length") == before + 1, "one question was added"
            page.click("#review-body .finding.done:nth-of-type(2) [data-act=zuruecksetzen]")
            wait("document.querySelectorAll('#review-body .finding.done').length === 1")
            page.click("#review-body .finding:not(.done) [data-act=frage]")  # the same question once more
            wait("document.querySelectorAll('#review-body .finding.done').length === 2")
            assert js("S.eintraege.length") == before + 1, "the same question is not added twice"
            page.keyboard.press("Escape")
        step("review: apply a fix, take a question over once even when clicked twice", review)

        def instruction():
            els = js("S.prozesse[0].elemente.length")
            page.fill("#pcmd-text", "Füge eine Policy für fehlende Daten ein")
            page.press("#pcmd-text", "Enter")
            wait(f"S.prozesse[0].elemente.length === {els + 1}")
            wait("!document.querySelector('#pcmd-undo').hidden")
            page.click("#pcmd-undo")
            wait(f"S.prozesse[0].elemente.length === {els}")
        step("instruction edits the process and can be undone", instruction)

        def paragraph():
            page.click("#transcript .ex")
            wait("document.querySelectorAll('#transcript .ln.out').length > 0")
            page.click("#transcript .ex")
            wait("document.querySelectorAll('#transcript .ln.out').length === 0")
        step("a paragraph can be left out of the analysis and back in", paragraph)

        def listening():
            page.click("#b-listen")
            wait("isMuted() && !document.querySelector('#t-mute').hidden")
            page.click("#b-listen")
            wait("!isMuted()")
        step("not listening can be switched on and off", listening)

        def own_window():
            pop = browser.new_page(viewport={"width": 1300, "height": 800})
            pop.goto(f"{url}/live?nur=prozess#{sid}")
            pop.wait_for_function("typeof S !== 'undefined' && S && document.querySelectorAll('.sticky').length > 0")
            assert pop.evaluate("document.body.classList.contains('solo')")
            assert not pop.evaluate("document.querySelector('#c-trans').offsetParent"), "only the board is shown"
            right = pop.evaluate("document.querySelector('#pcmd-go').getBoundingClientRect().right")
            assert right <= 1300, f"the instruction field fits the window ({right:.0f}px)"
            pop.close()
        step("own window shows only the process and fits", own_window)

        def split():
            old = js("S.id")
            page.click("#b-split")
            wait(f"S && S.id !== '{old}' && S.ab_ms > 0")
            wait("document.querySelectorAll('.ln').length === 0")
            assert js("S.eintraege.length") == 0 and "ab" in js("S.titel")
        step("new session from now starts empty in the same recording", split)

        def no_errors():
            assert not errors, errors[0][:200]
        step("no errors in the page", no_errors)
        browser.close()

    srv.shutdown()
    print("\nscratch data:", tmp, "(temporary, nothing else was touched)")
    print(f"{len(failures)} failed" if failures else "all steps passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
