// Debrief Kalender: liefert Termine eines Zeitraums als JSON (für den Debrief).
//   kalender --auth                     Zugriff anfragen (einmalig, zeigt den macOS-Dialog)
//   kalender --von <ISO> --bis <ISO>    Termine, die den Zeitraum überschneiden (Ortszeit, yyyy-MM-ddTHH:mm:ss)
import EventKit
import Foundation

let args = CommandLine.arguments
func arg(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}
func emit(_ obj: Any, code: Int32 = 0) -> Never {
    let data = (try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])) ?? Data("{}".utf8)
    FileHandle.standardOutput.write(data + Data("\n".utf8))
    exit(code)
}

let store = EKEventStore()
let done = DispatchSemaphore(value: 0)
var granted = false
store.requestFullAccessToEvents { ok, _ in granted = ok; done.signal() }
done.wait()
if !granted {
    emit(["fehler": "Kein Zugriff auf den Kalender – Systemeinstellungen → Datenschutz & Sicherheit → Kalender → „Debrief Kalender“ erlauben"], code: 2)
}
if args.contains("--auth") { emit(["ok": true]) }

let fmt = DateFormatter()
fmt.locale = Locale(identifier: "en_US_POSIX")
fmt.timeZone = .current
fmt.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
guard let von = arg("--von").flatMap(fmt.date(from:)), let bis = arg("--bis").flatMap(fmt.date(from:)) else {
    emit(["fehler": "Aufruf: --von yyyy-MM-ddTHH:mm:ss --bis yyyy-MM-ddTHH:mm:ss"], code: 1)
}

func person(_ p: EKParticipant) -> [String: Any] {
    let mail = p.url.absoluteString.replacingOccurrences(of: "mailto:", with: "", options: .caseInsensitive)
    return ["name": p.name ?? "", "email": mail.removingPercentEncoding ?? mail, "ich": p.isCurrentUser]
}
let events = store.events(matching: store.predicateForEvents(withStart: von, end: bis, calendars: nil))
    .filter { !$0.isAllDay }
emit(events.map { e -> [String: Any] in [
    "titel": e.title ?? "",
    "start": fmt.string(from: e.startDate),
    "ende": fmt.string(from: e.endDate),
    "ort": e.location ?? "",
    "kalender": e.calendar.title,
    "organisator": e.organizer.map(person) ?? NSNull(),
    "teilnehmer": (e.attendees ?? []).map(person),
] })
