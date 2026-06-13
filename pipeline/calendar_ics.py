"""Deadline calendar: parse venue deadlines and emit an .ics the user can import.

Stdlib only. Best-effort date parsing across the messy formats CFP pages use
(ISO, 'May 8, 2026', '8 May 2026', trailing 'AoE', etc.). Unparseable deadlines
are kept in the event description rather than guessed.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}
_ABBR = {m[:3].lower(): i for m, i in _MONTHS.items()}


def parse_deadline(s: str) -> date | None:
    if not s:
        return None
    t = s.strip()
    if t.lower() in ("unknown", "tbd", "n/a", "none", ""):
        return None
    # strip timezone-ish trailers
    t = re.sub(r"\b(AoE|UTC|GMT|PST|PDT|EST|EDT|ET|PT|anywhere on earth)\b.*$", "", t,
               flags=re.IGNORECASE).strip().strip(",")
    # ISO 2026-05-08
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    # slashes 05/08/2026 (assume m/d/y) or 2026/05/08
    m = re.search(r"\b(\d{1,4})/(\d{1,2})/(\d{1,4})\b", t)
    if m:
        a, b, c = (int(x) for x in m.groups())
        try:
            if a > 31:
                return date(a, b, c)
            return date(c, a, b)
        except ValueError:
            pass
    # 'May 8, 2026' / 'May 8 2026'
    m = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", t)
    if m:
        mon = _MONTHS.get(m.group(1).lower()) or _ABBR.get(m.group(1)[:3].lower())
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(2)))
            except ValueError:
                pass
    # '8 May 2026'
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\.?\s+(\d{4})", t)
    if m:
        mon = _MONTHS.get(m.group(2).lower()) or _ABBR.get(m.group(2)[:3].lower())
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(1)))
            except ValueError:
                pass
    return None


def _esc(s: str) -> str:
    return (str(s or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _fold(line: str) -> str:
    # RFC5545 75-octet line folding (simple).
    out = []
    while len(line) > 73:
        out.append(line[:73])
        line = " " + line[73:]
    out.append(line)
    return "\r\n".join(out)


def build_ics(events: list[dict], cal_name: str = "Autoscience paper deadlines",
              stamp: str | None = None) -> str:
    """events: list of {uid, summary, date(date), description, url, reminder_days}."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//autoscience//mission-control//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_esc(cal_name)}"]
    dtstamp = (stamp or "20260101T000000Z")
    for ev in events:
        d: date = ev["date"]
        nxt = d + timedelta(days=1)
        lines += ["BEGIN:VEVENT",
                  f"UID:{ev.get('uid', 'evt')}@autoscience",
                  f"DTSTAMP:{dtstamp}",
                  f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
                  f"DTEND;VALUE=DATE:{nxt.strftime('%Y%m%d')}",
                  _fold(f"SUMMARY:{_esc(ev.get('summary', 'Deadline'))}"),
                  _fold(f"DESCRIPTION:{_esc(ev.get('description', ''))}")]
        if ev.get("url"):
            lines.append(_fold(f"URL:{_esc(ev['url'])}"))
        days = int(ev.get("reminder_days", 7))
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY",
                  _fold(f"DESCRIPTION:{_esc(ev.get('summary', 'Deadline'))} in {days} days"),
                  f"TRIGGER:-P{days}D", "END:VALARM", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def venue_event(paper_title: str, venue: dict, uid: str) -> dict | None:
    d = parse_deadline(venue.get("deadline", ""))
    if d is None:
        return None
    arch = venue.get("archival", "unknown")
    tier = venue.get("tier", "?")
    odds = venue.get("accept_odds")
    fit = venue.get("fit_score")
    name = venue.get("name", "venue")
    summary = f"[CFP] {name} — {paper_title[:48]}"
    desc_parts = [
        f"Paper: {paper_title}",
        f"Venue: {name} ({venue.get('kind','?')}, tier {tier}, {arch})",
        f"Host: {venue.get('host','')}",
        f"Fit: {fit}/100 — {venue.get('fit_reason','')}",
        f"Odds: {odds}/100 — {venue.get('odds_reason','')}",
        f"Deadline (verified={venue.get('deadline_verified')}): {venue.get('deadline')}",
        f"CFP: {venue.get('cfp_url') or venue.get('url','')}",
    ]
    fixes = venue.get("fixes") or []
    if fixes:
        desc_parts.append("Fixes to improve odds: " + "; ".join(fixes))
    if not venue.get("deadline_verified"):
        desc_parts.append("⚠ deadline UNVERIFIED — confirm on the CFP page.")
    return {
        "uid": uid,
        "summary": summary,
        "date": d,
        "description": "\n".join(desc_parts),
        "url": venue.get("cfp_url") or venue.get("url", ""),
        "reminder_days": 7,
    }
