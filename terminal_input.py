"""Bounded durable at-most-once PTY input receipts; never store input text.

A reservation is committed before writing any bytes. An uncertain reservation
is never replayed. Receipts are pruned only for permanently closed terminals.
"""
import hashlib
import os
from pathlib import Path
import re
import sqlite3
from contextlib import contextmanager

import settings

ID = re.compile(r'[a-f0-9]{32}\Z')
REQUEST = re.compile(r'[A-Za-z0-9_-]{8,100}\Z')
SHA = re.compile(r'[a-f0-9]{64}\Z')
MAX_RECORDS = 250000
MAX_DATABASE_BYTES = 64 * 1024 * 1024
UNCERTAIN = '입력이 일부 또는 전부 반영되었을 수 있어 같은 요청을 다시 보내지 않습니다. 터미널 내용을 확인해 주세요.'


class InputUncertain(RuntimeError):
    pass


class TerminalInputs:
    def __init__(self, state_dir, max_records=MAX_RECORDS):
        if type(max_records) is not int or not 1 <= max_records <= MAX_RECORDS:
            raise ValueError('터미널 입력 기록 한도가 올바르지 않습니다.')
        self.max_records = max_records
        state = settings.safe_path(state_dir, directory=True, private=True, allow_missing=True)
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder = settings.safe_path(state / 'terminal-inputs', directory=True, private=True, allow_missing=True)
        folder.mkdir(mode=0o700, exist_ok=True)
        self.path = folder / 'receipts.sqlite3'
        settings.safe_path(self.path, private=True, allow_missing=True)
        if not self.path.exists():
            try:
                fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                settings.safe_path(self.path, private=True)
            else:
                os.close(fd)
        with self._connection() as connection:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError('지원하지 않는 터미널 입력 기록 버전입니다.')
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('CREATE TABLE IF NOT EXISTS receipts ('
                'request_hash TEXT PRIMARY KEY, terminal_hash TEXT NOT NULL, digest TEXT NOT NULL, '
                "status TEXT NOT NULL CHECK(status IN ('unknown', 'accepted'))) WITHOUT ROWID")
            connection.execute('CREATE INDEX IF NOT EXISTS receipts_terminal ON receipts(terminal_hash)')
            connection.execute('CREATE TABLE IF NOT EXISTS totals (id INTEGER PRIMARY KEY CHECK(id=1), count INTEGER NOT NULL)')
            connection.execute('INSERT OR IGNORE INTO totals SELECT 1, COUNT(*) FROM receipts')
            connection.execute('CREATE TRIGGER IF NOT EXISTS receipts_insert AFTER INSERT ON receipts '
                'BEGIN UPDATE totals SET count=count+1 WHERE id=1; END')
            connection.execute('CREATE TRIGGER IF NOT EXISTS receipts_delete AFTER DELETE ON receipts '
                'BEGIN UPDATE totals SET count=count-1 WHERE id=1; END')
            connection.execute('PRAGMA user_version=1')
            connection.execute('COMMIT')

    @contextmanager
    def _connection(self):
        connection = None
        try:
            settings.safe_path(self.path, private=True)
            settings.safe_path(self.path.with_name(self.path.name + '-journal'), private=True, allow_missing=True)
            if self.path.stat().st_size > MAX_DATABASE_BYTES:
                raise RuntimeError('터미널 입력 기록 파일이 허용 크기를 넘습니다.')
            connection = sqlite3.connect(str(self.path), timeout=2, isolation_level=None)
            connection.execute('PRAGMA journal_mode=DELETE')
            connection.execute('PRAGMA synchronous=FULL')
            page_size = connection.execute('PRAGMA page_size').fetchone()[0]
            connection.execute('PRAGMA max_page_count=' + str(MAX_DATABASE_BYTES // page_size))
            yield connection
        except sqlite3.Error:
            raise RuntimeError('터미널 입력 기록을 안전하게 저장하지 못했습니다. 새 입력을 보내지 않습니다.') from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _terminal(terminal_id):
        if not isinstance(terminal_id, str) or not ID.fullmatch(terminal_id):
            raise ValueError('터미널 ID가 올바르지 않습니다.')
        return hashlib.sha256(terminal_id.encode()).hexdigest()

    def submit(self, terminal_id, request_id, text, writer):
        terminal_hash = self._terminal(terminal_id)
        if not isinstance(request_id, str) or not REQUEST.fullmatch(request_id):
            raise ValueError('입력 요청 식별자가 필요합니다.')
        if not isinstance(text, str) or len(text.encode()) > 32768:
            raise ValueError('입력은 32KiB 이하여야 합니다.')
        digest = hashlib.sha256(text.encode()).hexdigest()
        request_hash = hashlib.sha256((terminal_id + ':' + request_id).encode()).hexdigest()
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            prior = connection.execute('SELECT digest, status FROM receipts WHERE request_hash=?', (request_hash,)).fetchone()
            if prior:
                if not isinstance(prior[0], str) or not SHA.fullmatch(prior[0]) or prior[1] not in ('unknown', 'accepted'):
                    raise RuntimeError('터미널 입력 기록 형식이 올바르지 않습니다.')
                if prior[0] != digest:
                    raise ValueError('같은 요청으로 다른 입력을 보낼 수 없습니다.')
                connection.execute('COMMIT')
                if prior[1] == 'unknown':
                    raise InputUncertain(UNCERTAIN)
                return {'ok': True, 'delivery': 'accepted', 'deduplicated': True}
            count = connection.execute('SELECT count FROM totals WHERE id=1').fetchone()
            if count is None or type(count[0]) is not int or count[0] < 0:
                raise RuntimeError('터미널 입력 기록 형식이 올바르지 않습니다.')
            if count[0] >= self.max_records:
                raise RuntimeError('터미널 입력 기록 한도에 도달했습니다. 종료된 터미널을 닫아 기록을 정리한 뒤 다시 연결해 주세요.')
            connection.execute('INSERT INTO receipts VALUES (?, ?, ?, ?)', (request_hash, terminal_hash, digest, 'unknown'))
            connection.execute('COMMIT')
        try:
            writer(terminal_id, text)
            with self._connection() as connection:
                connection.execute('BEGIN IMMEDIATE')
                result = connection.execute("UPDATE receipts SET status='accepted' WHERE request_hash=? AND digest=? AND status='unknown'", (request_hash, digest))
                if result.rowcount != 1:
                    raise InputUncertain(UNCERTAIN)
                connection.execute('COMMIT')
        except (OSError, RuntimeError, ValueError, KeyError):
            raise InputUncertain(UNCERTAIN) from None
        return {'ok': True, 'delivery': 'accepted', 'deduplicated': False}

    def prune_closed(self, terminal_id, terminal_records):
        self._terminal(terminal_id)
        if not isinstance(terminal_records, list) or not any(isinstance(row, dict) and
                row.get('id') == terminal_id and row.get('closed') is True for row in terminal_records):
            raise ValueError('영구 종료를 확인한 터미널의 입력 기록만 정리할 수 있습니다.')
        return self.prune_all_closed([{'id': terminal_id, 'closed': True}])

    def prune_all_closed(self, terminal_records):
        if not isinstance(terminal_records, list):
            raise ValueError('터미널 종료 기록이 올바르지 않습니다.')
        hashes = sorted({self._terminal(row.get('id')) for row in terminal_records
                         if isinstance(row, dict) and row.get('closed') is True})
        if not hashes:
            return {'pruned': 0}
        pruned = 0
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            for begin in range(0, len(hashes), 200):
                batch = hashes[begin:begin + 200]
                result = connection.execute('DELETE FROM receipts WHERE terminal_hash IN (' +
                    ','.join('?' for unused in batch) + ')', batch)
                pruned += result.rowcount
            connection.execute('COMMIT')
        return {'pruned': pruned}
