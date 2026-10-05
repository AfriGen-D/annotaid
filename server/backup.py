"""Nightly database backup, run by a thread inside the server (M23).

Uses SQLite's online backup API, which produces a consistent copy while the app
keeps running — copying the .db file directly could catch it mid-write (and
miss whatever is still in the -wal file).

PDFs are NOT copied here: they are content-addressed and never change once
written, so the sysadmin's ordinary backup of the data directory covers them.
Backups land in <data_dir>/backups, newest KEEP kept.
"""
from __future__ import annotations

import glob
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

KEEP = 14
HOUR_UTC = 2  # nightly, at 02:00 UTC


class Backups:
    def __init__(self, db_path: str, backup_dir: str, keep: int = KEEP, errors=None):
        self.db_path = db_path
        self.dir = backup_dir
        self.keep = keep
        self.errors = errors
        self._lock = threading.Lock()
        self._stop = threading.Event()
        os.makedirs(self.dir, exist_ok=True)

    def run_now(self) -> dict:
        """Write one backup now. -> {"file", "size", "at"}."""
        with self._lock:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            target = os.path.join(self.dir, f"annotaid-{stamp}.db")
            tmp = target + ".partial"
            src = sqlite3.connect(self.db_path, timeout=30)
            try:
                dst = sqlite3.connect(tmp)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            os.replace(tmp, target)
            self._prune()
            return self._info(target)

    def _prune(self) -> None:
        files = sorted(glob.glob(os.path.join(self.dir, "annotaid-*.db")))
        for old in files[:-self.keep] if self.keep else []:
            try:
                os.remove(old)
            except OSError:
                pass

    @staticmethod
    def _info(path: str) -> dict:
        st = os.stat(path)
        return {
            "file": os.path.basename(path),
            "size": st.st_size,
            "at": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def latest_path(self):
        files = sorted(glob.glob(os.path.join(self.dir, "annotaid-*.db")))
        return files[-1] if files else None

    def list(self) -> list:
        return [self._info(p) for p in sorted(glob.glob(os.path.join(self.dir, "annotaid-*.db")),
                                              reverse=True)]

    # ---- nightly thread ------------------------------------------------- #
    def start(self) -> None:
        threading.Thread(target=self._loop, name="annotaid-backup", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            nxt = now.replace(hour=HOUR_UTC, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            if self._stop.wait((nxt - now).total_seconds()):
                return
            try:
                self.run_now()
            except Exception as exc:  # noqa: BLE001
                if self.errors:
                    self.errors.add("BACKUP", "nightly", exc)
                time.sleep(60)
