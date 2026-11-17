"""Idempotenz-Schlüssel: Eine Aufgabe (z. B. „Stream 42 planen“) wird genau einmal ausgeführt."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ProcessingKey


def claim(db: Session, key: str, job_type: str) -> bool:
    """True, wenn der Schlüssel neu beansprucht wurde; False, wenn er bereits existiert."""
    if db.scalar(select(ProcessingKey.key).where(ProcessingKey.key == key)) is not None:
        return False
    try:
        with db.begin_nested():
            db.add(ProcessingKey(key=key[:255], job_type=job_type[:64]))
            db.flush()
    except IntegrityError:
        return False
    return True


def release(db: Session, key: str) -> None:
    row = db.get(ProcessingKey, key)
    if row is not None:
        db.delete(row)
        db.flush()
