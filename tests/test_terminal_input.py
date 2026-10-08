import concurrent.futures
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from terminal_input import InputUncertain, TerminalInputs

TID = 'a' * 32
OTHER = 'b' * 32
RID = 'fixture-request-001'


class TerminalInputTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = TerminalInputs(self.root)
        self.writes = []

    def write(self, tid, text):
        self.writes.append((tid, text))

    def count(self):
        with sqlite3.connect(str(self.ledger.path)) as db:
            return db.execute('SELECT count FROM totals').fetchone()[0]

    def test_success_receipt_survives_restart_and_replay_never_writes(self):
        first = self.ledger.submit(TID, RID, 'fixture-input', self.write)
        restarted = TerminalInputs(self.root)
        second = restarted.submit(TID, RID, 'fixture-input', self.write)
        self.assertTrue(first['ok']); self.assertTrue(second['deduplicated'])
        self.assertEqual(self.writes, [(TID, 'fixture-input')])
        self.assertEqual(self.count(), 1)
        with self.assertRaises(ValueError):
            restarted.submit(TID, RID, 'different-payload', self.write)

    def test_partial_write_failure_is_durable_unknown_and_never_replayed(self):
        accepted = bytearray()
        text = 'fixture-' + 'x' * 2040
        def partial(tid, value):
            accepted.extend(value.encode()[:1024])
            raise RuntimeError('fixture write timeout')
        with self.assertRaises(InputUncertain): self.ledger.submit(TID, RID, text, partial)
        with self.assertRaises(InputUncertain): TerminalInputs(self.root).submit(TID, RID, text, self.write)
        with self.assertRaises(ValueError): self.ledger.submit(TID, RID, 'other', self.write)
        self.assertEqual(len(accepted), 1024)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.count(), 1)

    def test_committed_unknown_reservation_exists_before_writer_is_called(self):
        def inspect(tid, value):
            with sqlite3.connect(str(self.ledger.path)) as db:
                self.assertEqual(db.execute('SELECT status FROM receipts').fetchone()[0], 'unknown')
            self.write(tid, value)
        self.ledger.submit(TID, RID, 'fixture', inspect)

    def test_failed_final_receipt_save_does_not_replay_accepted_bytes(self):
        def writer(tid, text):
            self.write(tid, text)
            self.ledger.path.chmod(0o644)
        with self.assertRaises(InputUncertain): self.ledger.submit(TID, RID, 'fixture', writer)
        self.ledger.path.chmod(0o600)
        with self.assertRaises(InputUncertain): TerminalInputs(self.root).submit(TID, RID, 'fixture', self.write)
        self.assertEqual(len(self.writes), 1)

    def test_two_instances_with_overlapping_same_request_never_duplicate_input(self):
        another = TerminalInputs(self.root)
        entered, release = threading.Event(), threading.Event()
        def writer(tid, text):
            self.write(tid, text); entered.set()
            if not release.wait(3): raise RuntimeError('fixture wait timeout')
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.ledger.submit, TID, RID, 'fixture', writer)
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaises(InputUncertain): another.submit(TID, RID, 'fixture', self.write)
            finally:
                release.set()
            self.assertTrue(first.result(3)['ok'])
        self.assertTrue(another.submit(TID, RID, 'fixture', self.write)['deduplicated'])
        self.assertEqual(len(self.writes), 1)

    def test_capacity_fails_closed_and_only_closed_tombstone_prune_frees_records(self):
        limited = TerminalInputs(self.root, max_records=1)
        limited.submit(TID, RID, 'fixture', self.write)
        with self.assertRaisesRegex(RuntimeError, '한도'): limited.submit(OTHER, RID, 'fixture', self.write)
        self.assertTrue(limited.submit(TID, RID, 'fixture', self.write)['deduplicated'])
        with self.assertRaises(ValueError): limited.prune_closed(TID, [{'id': TID, 'alive': False}])
        self.assertEqual(limited.prune_all_closed([{'id': TID, 'alive': False}]), {'pruned': 0})
        self.assertEqual(limited.prune_closed(TID, [{'id': TID, 'closed': True}]), {'pruned': 1})
        self.assertTrue(limited.submit(OTHER, RID, 'fixture', self.write)['ok'])
        self.assertEqual(self.count(), 1)

    def test_bulk_prune_preserves_live_and_unknown_records(self):
        self.ledger.submit(TID, RID, 'fixture', self.write)
        with self.assertRaises(InputUncertain): self.ledger.submit(OTHER, RID, 'fixture',
            lambda *args: (_ for _ in ()).throw(RuntimeError('fixture timeout')))
        self.assertEqual(self.ledger.prune_all_closed([
            {'id': TID, 'closed': True}, {'id': OTHER, 'alive': False, 'closed': False}]), {'pruned': 1})
        with self.assertRaises(InputUncertain): self.ledger.submit(OTHER, RID, 'fixture', self.write)
        self.assertEqual(self.count(), 1)

    def test_replay_after_closed_prune_cannot_reopen_permanently_closed_terminal(self):
        self.ledger.submit(TID, RID, 'fixture', self.write)
        self.ledger.prune_closed(TID, [{'id': TID, 'closed': True}])
        def closed(*args): raise RuntimeError('fixture permanent tombstone')
        with self.assertRaises(InputUncertain): self.ledger.submit(TID, RID, 'fixture', closed)
        with self.assertRaises(InputUncertain): self.ledger.submit(TID, RID, 'fixture', self.write)
        self.assertEqual(len(self.writes), 1)

    def test_regular_private_database_and_payload_limits_without_raw_input_storage(self):
        self.ledger.submit(TID, RID, 'PRIVATE-FIXTURE-SENTINEL', self.write)
        self.assertEqual(self.ledger.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.ledger.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(b'PRIVATE-FIXTURE-SENTINEL', self.ledger.path.read_bytes())
        self.assertEqual(len(list(self.ledger.path.parent.iterdir())), 1)
        for tid, rid, text in [('invalid', RID, 'x'), (TID, 'short', 'x'), (TID, RID + 'large', '가' * 11000)]:
            with self.subTest(tid=tid, rid=rid), self.assertRaises(ValueError):
                self.ledger.submit(tid, rid, text, self.write)
        self.assertEqual(len(self.writes), 1)

    def test_unsafe_database_and_journal_paths_fail_before_any_write(self):
        self.ledger.path.chmod(0o644)
        with self.assertRaises(ValueError): self.ledger.submit(TID, RID, 'fixture', self.write)
        self.ledger.path.chmod(0o600)
        target = self.root / 'outside'; target.write_text('untouched'); target.chmod(0o600)
        journal = self.ledger.path.with_name(self.ledger.path.name + '-journal')
        journal.symlink_to(target)
        with self.assertRaises(ValueError): self.ledger.submit(TID, RID, 'fixture', self.write)
        self.assertEqual(target.read_text(), 'untouched')
        self.assertEqual(self.writes, [])
        journal.unlink()
        self.ledger.path.unlink(); os.mkfifo(str(self.ledger.path), 0o600)
        with self.assertRaises(ValueError): self.ledger.submit(TID, RID, 'fixture', self.write)

    def test_corrupt_or_oversized_database_never_delivers_new_input(self):
        self.ledger.path.write_bytes(b'invalid database')
        with self.assertRaises(RuntimeError): self.ledger.submit(TID, RID, 'fixture', self.write)
        with patch('terminal_input.MAX_DATABASE_BYTES', 1):
            with self.assertRaises(RuntimeError): self.ledger.submit(TID, RID, 'fixture', self.write)
        self.assertEqual(self.writes, [])


if __name__ == '__main__': unittest.main()
