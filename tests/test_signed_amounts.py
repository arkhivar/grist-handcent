"""Synthetic, offline regression tests for signed amounts and Grist writes.

Run with: python -m unittest discover -s tests -v
All external I/O and credential resolution fail closed. Grist responses and SMS
messages below exist only in memory; no live synchronization is performed.
"""

import copy
import json
import socket
import unittest
import urllib.parse
import urllib.request
from contextlib import ExitStack
from unittest.mock import patch

import sms_grist_sync as sync


TABLE = "SyntheticTransactions"
CONFIG = {"headers": {"offline": "synthetic"}, "grist_key": "offline", "f900_unsent": []}
TIMESTAMP = 1_791_936_900_000
USER_FIELDS = {"category", "performance", "student", "sprint_week", "card", "op_type"}


def message(text, mid=1001, cid=276):
    return {"mid": mid, "ts": TIMESTAMP, "type": 1, "data": text, "cid": cid}


def expense_text(amount="1 234,56"):
    return (
        f"СберБизнес. Списание {amount}р с р/c*0001 на р/с*0002"
        " по документу №TEST-1 от 14.10.2026 в пользу Тестовая организация."
        " Баланс -9 876,54р 14.10.2026 10:15:00"
    )


def income_text(amount="1 234,56"):
    return (
        f"СберБизнес. Поступление {amount}р на р/c*0001 с р/с*0002"
        " по документу №TEST-2 от 14.10.2026 от Тестовая организация."
        " Баланс 9 876,54р 14.10.2026 10:15:00"
    )


def card_text(operation):
    return f"Счёт карты VISA0001 10:15 {operation} Баланс: -9 876,54р"


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        blocked = (
            "load_config", "_resolve", "http_json", "handcent_get", "grist_call",
            "open_pipe", "delete_burst", "paginate_conversation",
            "rescan_conversation", "delete_with_verification",
        )
        for name in blocked:
            self.stack.enter_context(patch.object(
                sync, name, side_effect=AssertionError(f"{name} is disabled in offline tests")
            ))
        self.stack.enter_context(patch.object(
            urllib.request, "urlopen", side_effect=AssertionError("HTTP is disabled")
        ))
        self.stack.enter_context(patch.object(
            socket, "create_connection", side_effect=AssertionError("Sockets are disabled")
        ))
        self.stack.enter_context(patch.object(sync.time, "sleep", return_value=None))
        self.stack.enter_context(patch.object(sync, "TABLE", TABLE))
        self.stack.enter_context(patch.object(sync, "_LOG", []))


class ParsingTests(OfflineTestCase):
    def test_0321_expense_is_negative_regardless_of_text_sign(self):
        for amount in ("1 234,56", "-1 234,56"):
            with self.subTest(amount=amount):
                row, error = sync.parse_expense(message(expense_text(amount)))
                self.assertIsNone(error)
                self.assertEqual(row["amount"], -1234.56)
                self.assertEqual(row["balance_after"], -9876.54)
                self.assertEqual(row["account_from"], "0001")
                self.assertEqual(row["account_to"], "0002")

    def test_0321_income_is_positive_regardless_of_text_sign(self):
        for amount in ("1 234,56", "-1 234,56"):
            with self.subTest(amount=amount):
                row, error = sync.parse_income(message(income_text(amount)))
                self.assertIsNone(error)
                self.assertEqual(row["amount"], 1234.56)
                self.assertEqual(row["balance_after"], 9876.54)
                self.assertEqual(row["account_to"], "0001")

    def test_900_completed_operations_follow_direction(self):
        operations = (
            ("Перевод 1 234,56р от Тестовый отправитель", "transfer_in", 1234.56),
            ("Перевод -1 234,56р от Тестовый отправитель", "transfer_in", 1234.56),
            ("перевод 1 234,56р Тестовый получатель", "transfer_out", -1234.56),
            ("перевод -1 234,56р Тестовый получатель", "transfer_out", -1234.56),
            ("Покупка по СБП 1 234,56р SYNTHETIC SHOP", "purchase_sbp", -1234.56),
            ("Отмена покупки по СБП 1 234,56р SYNTHETIC SHOP", "refund", 1234.56),
            ("Отмена покупки по СБП -1 234,56р SYNTHETIC SHOP", "refund", 1234.56),
            ("Покупка 1 234,56р SYNTHETIC SHOP", "purchase", -1234.56),
            ("Оплата уведомлений 1 234,56р до 14.11.2026", "fee", -1234.56),
            ("Оплата 1 234,56р SYNTHETIC SERVICE", "payment", -1234.56),
        )
        for operation, kind, expected in operations:
            with self.subTest(operation=operation):
                row, error = sync.parse_900(message(card_text(operation), cid=277))
                self.assertIsNone(error)
                self.assertEqual(row["op_type"], kind)
                self.assertEqual(row["amount"], expected)
                self.assertEqual(row["balance_after"], -9876.54)

    def test_900_sbp_in_capture_indices_and_counterparty(self):
        text = card_text("Перевод по СБП из TESTBANK +1 234,56р от Тестовый отправитель")
        row, error = sync.parse_900(message(text, cid=277))
        self.assertIsNone(error)
        self.assertEqual(row["op_type"], "sbp_in")
        self.assertEqual(row["amount"], 1234.56)
        self.assertEqual(row["counterparty"], "Тестовый отправитель (СБП TESTBANK)")

    def test_900_court_collection_is_negative(self):
        for amount in ("1 234,56", "-1 234,56"):
            with self.subTest(amount=amount):
                text = (
                    "14.10.26 по требованию взыскателя с карты VISA0001 взыскана сумма "
                    f"{amount}р по судебному приказу №TEST-3 от 01.10.2026."
                )
                row, error = sync.parse_900(message(text, cid=277))
                self.assertIsNone(error)
                self.assertEqual(row["op_type"], "court_collection")
                self.assertEqual(row["amount"], -1234.56)

    def test_900_informative_messages_have_no_monetary_amount(self):
        # Confirmation and rejection describe an attempted transfer or purchase;
        # adjust these two expectations here if their accounting policy changes.
        texts = (
            ("Дмитрий Владимирович, синтетическое рекламное сообщение.", "promo"),
            ("Никому не сообщайте код. Синтетический код 000000.", "otp"),
            ("Подтвердите перевод с карты VISA0001 на карту MIR0002 на сумму 500р", "confirm"),
            ("Счёт карты VISA0001 Недостаточно средств. Покупка 500р SYNTHETIC SHOP Баланс: 20р", "declined"),
        )
        for text, kind in texts:
            with self.subTest(kind=kind):
                row, error = sync.parse_900(message(text, cid=277))
                self.assertIsNone(error)
                self.assertEqual(row["op_type"], kind)
                self.assertIsNone(row.get("amount"))
                fields = sync.fields_txn(row, "900")
                self.assertNotIn("amount", fields)
                self.assertNotIn("op_type", fields)

    def test_fields_omit_internal_and_user_managed_fields(self):
        row, error = sync.parse_expense(message(expense_text()))
        self.assertIsNone(error)
        row.update({name: "user-owned synthetic value" for name in USER_FIELDS})
        fields = sync.fields_txn(row, "0321")
        self.assertTrue(USER_FIELDS.isdisjoint(fields))
        self.assertEqual(fields["amount"], -1234.56)
        self.assertEqual(fields["notes"], expense_text())
        self.assertEqual(fields["datetime"], TIMESTAMP // 1000)

    def test_unparsed_and_0321_otp_have_only_four_persisted_base_fields(self):
        for text in ("Синтетическое неизвестное сообщение", "Не сообщайте код никому: 000000"):
            with self.subTest(text=text):
                row = sync.unparsed_row(message(text))
                if sync.classify_0321(text) == "otp":
                    row["op_type"] = "otp"
                fields = sync.fields_txn(row, "0321")
                self.assertEqual(set(fields), {"datetime", "notes", "mid", "source"})


class MemoryGrist:
    """Strict in-memory transport: only filtered GET and insert-only POST."""

    def __init__(self, records=(), readback_overrides=None):
        self.records = {fields["mid"]: copy.deepcopy(fields) for fields in records}
        self.readback_overrides = readback_overrides or {}
        self.calls = []
        self.read_count = 0

    def __call__(self, key, table, method, path, body=None, **kwargs):
        assert key == "offline", "Unexpected credential passed to offline Grist"
        assert table == TABLE, "Unexpected table passed to offline Grist"
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "GET":
            parsed = urllib.parse.urlsplit(path)
            query = urllib.parse.parse_qs(parsed.query)
            assert parsed.path == "/records" and set(query) == {"filter"}, "Unfiltered Grist read"
            filters = json.loads(query["filter"][0])
            assert set(filters) == {"mid"}, "Unexpected Grist filter"
            mids = filters["mid"]
            assert mids and len(mids) <= sync.GRIST_CHUNK, "Invalid filter chunk"
            self.read_count += 1
            records = []
            for mid in mids:
                fields = self.records.get(mid)
                if self.read_count > 1 and mid in self.readback_overrides:
                    fields = self.readback_overrides[mid]
                if fields is not None:
                    records.append({"id": mid, "fields": copy.deepcopy(fields)})
            return {"records": records}
        assert method == "POST" and path == "/records", "Existing Grist rows must not be modified"
        for record in body["records"]:
            fields = record["fields"]
            assert USER_FIELDS.isdisjoint(fields), "User-managed field persisted"
            assert fields["mid"] not in self.records, "Existing Grist row overwritten"
            self.records[fields["mid"]] = copy.deepcopy(fields)
        return {"records": [{"id": record["fields"]["mid"]} for record in body["records"]]}


class VerificationTests(OfflineTestCase):
    def signed_row(self, amount=-1234.56):
        return {"mid": 1001, "sms_ts": TIMESTAMP, "text": "Synthetic debit", "amount": amount}

    def test_signed_write_readback_and_existing_user_fields_are_preserved(self):
        existing = {"mid": 1002, "amount": 50.0, "notes": "Manual note", "op_type": "Manual choice",
                    "category": "Manual category", "performance": 7, "student": "Synthetic student",
                    "sprint_week": "Manual sprint"}
        grist = MemoryGrist([existing])
        new = self.signed_row()
        known = {"mid": 1002, "sms_ts": TIMESTAMP, "text": "Original SMS", "amount": 50.0}
        with patch.object(sync, "grist_call", side_effect=grist):
            written, verified = sync.write_and_verify(
                "offline", TABLE, [new, known], lambda row: sync.fields_txn(row, "0321"), True
            )
        self.assertEqual(written, 1)
        self.assertEqual(verified, {1001, 1002})
        self.assertEqual(grist.records[1001]["amount"], -1234.56)
        self.assertEqual(grist.records[1002], existing)
        self.assertEqual([method for method, _, _ in grist.calls], ["GET", "POST", "GET"])

    def test_opposite_sign_existing_row_is_not_verified_or_overwritten(self):
        existing = {"mid": 1001, "amount": 1234.56, "notes": "Manual note", "op_type": "Manual choice"}
        grist = MemoryGrist([existing])
        with patch.object(sync, "grist_call", side_effect=grist):
            written, verified = sync.write_and_verify(
                "offline", TABLE, [self.signed_row()], lambda row: sync.fields_txn(row, "0321"), True
            )
        self.assertEqual(written, 0)
        self.assertEqual(verified, set())
        self.assertEqual(grist.records[1001], existing)
        self.assertTrue(all(method == "GET" for method, _, _ in grist.calls))

    def test_missing_readback_row_is_not_verified(self):
        grist = MemoryGrist(readback_overrides={1001: None})
        with patch.object(sync, "grist_call", side_effect=grist):
            written, verified = sync.write_and_verify(
                "offline", TABLE, [self.signed_row()], lambda row: sync.fields_txn(row, "0321"), True
            )
        self.assertEqual(written, 1)
        self.assertEqual(verified, set())

    def test_numeric_zero_is_distinct_from_blank_and_boolean(self):
        for expected, actual, matches in (
            (0.0, 0, True),
            (0.0, 0.0, True),
            (0.0, None, False),
            (0.0, False, False),
            (1.0, True, False),
        ):
            with self.subTest(expected=expected, actual=actual):
                grist = MemoryGrist([{"mid": 1001, "amount": actual}])
                with patch.object(sync, "grist_call", side_effect=grist):
                    _, verified = sync.write_and_verify(
                        "offline", TABLE, [self.signed_row(expected)],
                        lambda row: sync.fields_txn(row, "0321"), True
                    )
                self.assertEqual(verified, {1001} if matches else set())

    def test_blank_informative_amount_is_distinct_from_numeric_zero(self):
        for fields, matches in (
            ({"mid": 1001}, True),
            ({"mid": 1001, "amount": None}, True),
            ({"mid": 1001, "amount": 0}, False),
            ({"mid": 1001, "amount": 1}, False),
        ):
            with self.subTest(fields=fields):
                grist = MemoryGrist([fields])
                row = {"mid": 1001, "sms_ts": TIMESTAMP, "text": "Synthetic informative SMS"}
                with patch.object(sync, "grist_call", side_effect=grist):
                    _, verified = sync.write_and_verify(
                        "offline", TABLE, [row], lambda item: sync.fields_txn(item, "900"), True
                    )
                self.assertEqual(verified, {1001} if matches else set())

    def test_filtered_reads_are_chunked_and_empty_mid_list_makes_no_request(self):
        mids = list(range(2001, 2018))
        grist = MemoryGrist([{"mid": mid, "amount": -1.0} for mid in mids])
        with patch.object(sync, "grist_call", side_effect=grist):
            self.assertEqual(sync.grist_fetch_fields("offline", TABLE, []), {})
            self.assertEqual(grist.calls, [])
            result = sync.grist_fetch_fields("offline", TABLE, mids)
        self.assertEqual(set(result), set(mids))
        chunks = [json.loads(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["filter"][0])["mid"]
                  for _, path, _ in grist.calls]
        self.assertEqual(chunks, [mids[:8], mids[8:16], mids[16:]])


class PipelineTests(OfflineTestCase):
    def exercise_sender(self, msgs, source, grist):
        cid = 276 if source == "0321" else 277
        deleted = []

        def delete_fake(delete_cid, headers, targets):
            self.assertEqual(delete_cid, cid)
            self.assertEqual(headers, CONFIG["headers"])
            deleted.extend(target["mid"] for target in targets)
            return len(targets), [], len(targets)

        codes = []
        with patch.object(sync, "paginate_conversation", return_value=msgs), \
                patch.object(sync, "grist_call", side_effect=grist), \
                patch.object(sync, "delete_with_verification", side_effect=delete_fake) as deletion:
            entry = sync.run_sender(copy.deepcopy(CONFIG), cid, source, codes)
        return entry, codes, deleted, deletion

    def test_verified_signed_rows_are_deleted_after_filtered_readback(self):
        msgs = [message(expense_text()), message(income_text(), mid=1002)]
        grist = MemoryGrist()
        entry, _, deleted, _ = self.exercise_sender(msgs, "0321", grist)
        self.assertEqual(grist.records[1001]["amount"], -1234.56)
        self.assertEqual(grist.records[1002]["amount"], 1234.56)
        self.assertEqual(set(deleted), {1001, 1002})
        self.assertEqual(entry["rowsWritten"], 2)
        self.assertEqual(entry["smsDeleted"], 2)
        self.assertEqual([method for method, _, _ in grist.calls], ["GET", "POST", "GET"])

    def test_no_deletion_on_missing_or_invalid_monetary_readback(self):
        invalid_rows = (
            None,
            {"mid": 1001},
            {"mid": 1001, "amount": None},
            {"mid": 1001, "amount": 1234.56},
            {"mid": 1001, "amount": "not-a-number"},
            {"mid": 1001, "amount": True},
            {"mid": 1001, "amount": float("nan")},
            {"mid": 1001, "amount": float("inf")},
            {"mid": 1001, "amount": float("-inf")},
        )
        for readback in invalid_rows:
            with self.subTest(readback=readback):
                grist = MemoryGrist(readback_overrides={1001: readback})
                entry, _, deleted, deletion = self.exercise_sender(
                    [message(expense_text())], "0321", grist
                )
                self.assertEqual(deleted, [])
                deletion.assert_not_called()
                self.assertEqual(entry["smsDeleted"], 0)
                self.assertEqual(entry["pending"], 1)

    def test_readback_timeout_cannot_trigger_deletion(self):
        grist = MemoryGrist()

        # The read counter is one after dedup; allow the insert, then fail GET.
        def transport(key, table, method, path, body=None, **kwargs):
            if method == "GET" and grist.read_count:
                raise TimeoutError("Synthetic Grist read timeout")
            return grist(key, table, method, path, body, **kwargs)

        with patch.object(sync, "paginate_conversation", return_value=[message(expense_text())]), \
                patch.object(sync, "grist_call", side_effect=transport), \
                patch.object(sync, "delete_with_verification") as deletion:
            with self.assertRaises(TimeoutError):
                sync.run_sender(copy.deepcopy(CONFIG), 276, "0321", [])
        deletion.assert_not_called()
        self.assertEqual(grist.records[1001]["amount"], -1234.56)

    def test_existing_positive_debit_is_preserved_and_phone_sms_remains(self):
        existing = {"mid": 1001, "amount": 1234.56, "notes": "Manual note", "category": "Manual category",
                    "op_type": "Manual choice"}
        grist = MemoryGrist([existing])
        entry, _, deleted, deletion = self.exercise_sender([message(expense_text())], "0321", grist)
        deletion.assert_not_called()
        self.assertEqual(deleted, [])
        self.assertEqual(entry["rowsWritten"], 0)
        self.assertEqual(grist.records[1001], existing)
        self.assertTrue(all(method == "GET" for method, _, _ in grist.calls))

    def test_legacy_backlog_cannot_bypass_signed_verification(self):
        existing = {"mid": 1001, "amount": 1234.56, "notes": "Manual note",
                    "op_type": "Manual choice", "category": "Manual category"}
        grist = MemoryGrist([existing])
        cfg = copy.deepcopy(CONFIG)
        cfg["f900_unsent"] = [1001]
        msgs = [message(card_text("Покупка 1 234,56р SYNTHETIC SHOP"), cid=277)]
        with patch.object(sync, "load_config", return_value=cfg), \
                patch.object(sync, "paginate_conversation", return_value=msgs), \
                patch.object(sync, "grist_call", side_effect=grist), \
                patch.object(sync, "rescan_conversation") as rescan, \
                patch.object(sync, "delete_with_verification") as deletion:
            result = sync.run({"input": {"cid": "900"}})
        sender = result["artifact"]["senders"]["900"]
        self.assertEqual(sender["smsDeleted"], 0)
        self.assertEqual(sender["pending"], 1)
        deletion.assert_not_called()
        rescan.assert_not_called()
        self.assertEqual(grist.records[1001], existing)
        # Dedup and signed read-back are the only lookups. A separate presence-
        # only backlog lookup must not authorize the otherwise rejected delete.
        self.assertEqual([method for method, _, _ in grist.calls], ["GET", "GET"])

    def test_0321_code_strip_and_parse_failures_keep_internal_classification(self):
        msgs = [message("Не сообщайте код никому: 000000"),
                message("Синтетическое неизвестное сообщение", mid=1002)]
        grist = MemoryGrist()
        entry, codes, deleted, _ = self.exercise_sender(msgs, "0321", grist)
        self.assertEqual(entry["parseFailures"], 1)
        self.assertEqual(len(codes), 1)
        self.assertEqual(codes[0]["sender"], "0321")
        self.assertEqual(codes[0]["text"], msgs[0]["data"])
        self.assertTrue(codes[0]["date"].endswith("+10:00"))
        self.assertEqual(set(deleted), {1001, 1002})
        for fields in grist.records.values():
            self.assertEqual(set(fields), {"datetime", "notes", "mid", "source"})

    def test_900_code_keeps_internal_classification_without_persisted_amount(self):
        msgs = [message("Никому не сообщайте код. Синтетический код 000000.", cid=277)]
        grist = MemoryGrist()
        entry, codes, deleted, _ = self.exercise_sender(msgs, "900", grist)
        self.assertEqual(entry["parseFailures"], 0)
        self.assertEqual(len(codes), 1)
        self.assertEqual(codes[0]["sender"], "900")
        self.assertEqual(deleted, [1001])
        self.assertNotIn("amount", grist.records[1001])
        self.assertNotIn("op_type", grist.records[1001])


if __name__ == "__main__":
    unittest.main()
