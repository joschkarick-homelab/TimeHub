#!/usr/bin/env python3
"""Übertrage alle Projekte und Zeiterfassungen von einem Nutzer auf einen anderen.

Umzug, keine Kopie: die Datensätze wechseln den Besitzer (`user_id`). Der
Quellnutzer bleibt bestehen, hat danach aber keine Projekte/Einträge mehr.

Mitbewegt wird automatisch:
  * EntrySync-Zeilen — sie hängen an der Zeiterfassung (`entry_id`), nicht am
    Nutzer, und ziehen mit den Einträgen mit.

Sonderfall Projektcode-Kollision: Projekte sind pro Nutzer eindeutig
(`UNIQUE(user_id, code)`). Hat der Zielnutzer bereits einen Code, den ein
umziehendes Projekt trägt, bekommt das umziehende Projekt einen eindeutigen
Suffix (`CODE-2`, `CODE-3`, …) — wie beim normalen Anlegen in der App.

Sicherheit: standardmäßig DRY-RUN (es wird nichts geschrieben, die Transaktion
wird zurückgerollt). Erst mit ``--commit`` wird festgeschrieben.

Die Datenbank wird wie in der App über ``DATABASE_URL`` / ``.env`` aufgelöst;
das Script also in derselben Umgebung (gleiche Env-Vars) wie TimeHub starten.

Beispiele:
    # Vorschau (nichts wird geschrieben):
    python scripts/transfer_user_data.py --from admin@joschka.eu --to jori@tuta.com

    # Tatsächlich übertragen:
    python scripts/transfer_user_data.py --from admin@joschka.eu --to jori@tuta.com --commit
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Repo-Root importierbar machen, egal von wo das Script gestartet wird.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.models.time_entry import TimeEntry  # noqa: E402
from app.models.user import User  # noqa: E402


def find_user(db, email: str) -> User | None:
    """Nutzer per E-Mail. Exakt zuerst; sonst case-insensitiver Eindeutig-Treffer
    (E-Mails werden in TimeHub nicht normalisiert gespeichert)."""
    e = (email or "").strip()
    user = db.execute(select(User).where(User.email == e)).scalar_one_or_none()
    if user is not None:
        return user
    matches = db.execute(
        select(User).where(func.lower(User.email) == e.lower())
    ).scalars().all()
    if len(matches) > 1:
        raise SystemExit(f"Mehrdeutig: mehrere Nutzer matchen {email!r} (Groß-/Kleinschreibung).")
    return matches[0] if matches else None


def unique_code(db, user_id: int, code: str, taken: set[str]) -> str:
    """Freier Projektcode für `user_id` auf Basis von `code`. Berücksichtigt
    bereits in der DB vergebene Codes UND in dieser Transaktion neu belegte
    (`taken`)."""
    base = (code or "PROJEKT").strip() or "PROJEKT"
    candidate, n = base, 2
    while True:
        in_db = db.execute(
            select(Project.id).where(Project.user_id == user_id, Project.code == candidate)
        ).first()
        if in_db is None and candidate not in taken:
            return candidate
        candidate = f"{base}-{n}"
        n += 1


def transfer(db, src: User, dst: User):
    """Setzt den Besitzer aller Projekte und Zeiterfassungen von src auf dst.
    Gibt (projekte, eintraege, umbenennungen) zurück; nicht committet."""
    projects = db.execute(
        select(Project).where(Project.user_id == src.id).order_by(Project.id)
    ).scalars().all()
    entries = db.execute(
        select(TimeEntry).where(TimeEntry.user_id == src.id)
    ).scalars().all()

    taken: set[str] = set(db.execute(
        select(Project.code).where(Project.user_id == dst.id)
    ).scalars().all())

    renames: list[tuple[int, str, str]] = []
    for p in projects:
        new_code = unique_code(db, dst.id, p.code, taken)
        if new_code != p.code:
            renames.append((p.id, p.code, new_code))
            p.code = new_code
        taken.add(new_code)
        p.user_id = dst.id

    for e in entries:
        e.user_id = dst.id

    return projects, entries, renames


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--from", dest="src_email", required=True, help="Quell-E-Mail")
    ap.add_argument("--to", dest="dst_email", required=True, help="Ziel-E-Mail")
    ap.add_argument(
        "--commit", action="store_true",
        help="Änderungen festschreiben (ohne dieses Flag: Dry-Run, kein Schreiben)",
    )
    args = ap.parse_args()

    db = SessionLocal()
    try:
        src = find_user(db, args.src_email)
        dst = find_user(db, args.dst_email)
        if src is None:
            raise SystemExit(f"Quell-Nutzer nicht gefunden: {args.src_email}")
        if dst is None:
            raise SystemExit(f"Ziel-Nutzer nicht gefunden: {args.dst_email}")
        if src.id == dst.id:
            raise SystemExit("Quelle und Ziel sind derselbe Nutzer — nichts zu tun.")

        projects, entries, renames = transfer(db, src, dst)

        print(f"Quelle: {src.email} (id={src.id})")
        print(f"Ziel:   {dst.email} (id={dst.id})")
        print(f"Projekte zu übertragen:        {len(projects)}")
        print(f"Zeiterfassungen zu übertragen: {len(entries)}")
        if renames:
            print(f"Code-Kollisionen umbenannt:    {len(renames)}")
            for pid, old, new in renames:
                print(f"   Projekt id={pid}: '{old}' -> '{new}'")

        if args.commit:
            db.commit()
            print("\n✅ Übertragung festgeschrieben.")
        else:
            db.rollback()
            print("\nℹ️  DRY-RUN — nichts geschrieben. Mit --commit übernehmen.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
