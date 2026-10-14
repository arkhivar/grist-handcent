"""Offline, synthetic tests for the bounded one-time amount migration."""

import copy
import json
import re
import unittest
import urllib.parse

from test_signed_amounts import OfflineTestCase, TABLE, card_text, expense_text, income_text
from tools import migrate_signed_amounts as migration


def bank_row(row_id=1, kind="expense", amount=1234.56, source="0321", notes=None):
    return {"id": row_id, "fields": {
        "mid": 1000 + row_id, "source": source, "op_type": kind, "amount": amount,
        "notes": expense_text() if notes is None else notes,
        "category": "Manual category", "performance": 7,
        "student": "Synthetic student", "sprint_week": "Manual sprint",
    }}


class FakeGrist(migration.Grist):
    """Execute only bounded SQL/record calls against synthetic in-memory rows."""

    def __init__(self, rows=()):
        super().__init__("synthetic", TABLE, "offline")
        self.rows = {row["id"]: copy.deepcopy(row["fields"]) for row in rows}
        self.calls = []
        self.amount_type = "Numeric"
        self.amount_is_formula = False
        self.missing_columns = set()
        self.ignore_patches = False
        self.timeout_after_patch_once = False
        self.filtered_read_hook = None
        self.filtered_read_count = 0

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "GET" and path == f"/tables/{TABLE}/columns":
            return {"columns": [
                {"id": key, "fields": {
                    "type": self.amount_type if key == "amount" else "Text",
                    "isFormula": self.amount_is_formula if key == "amount" else False,
                }}
                for key in ("amount", "mid", "source", "notes", "op_type")
                if key not in self.missing_columns
            ]}
        if method == "POST" and path == "/sql":
            sql = body["sql"]
            assert f'FROM "{TABLE}"' in sql
            assert "WHERE source IN ('0321', '900') AND id > " in sql
            assert "ORDER BY id LIMIT 50" in sql and "OFFSET" not in sql
            last_id = int(re.search(r"AND id > (\d+)", sql).group(1))
            ids = [row_id for row_id in sorted(self.rows)
                   if row_id > last_id and self.rows[row_id]["source"] in {"0321", "900"}]
            return {"records": [
                {"fields": {**copy.deepcopy(self.rows[row_id]), "id": row_id}}
                for row_id in ids[:migration.PAGE_SIZE]
            ]}
        if method == "GET":
            parsed = urllib.parse.urlsplit(path)
            query = urllib.parse.parse_qs(parsed.query)
            assert parsed.path == f"/tables/{TABLE}/records" and set(query) == {"filter"}
            filters = json.loads(query["filter"][0])
            assert set(filters) == {"id"}
            ids = filters["id"]
            assert 1 <= len(ids) <= migration.BATCH_SIZE
            self.filtered_read_count += 1
            result = {"records": [
                {"id": row_id, "fields": copy.deepcopy(self.rows[row_id])}
                for row_id in ids if row_id in self.rows
            ]}
            if self.filtered_read_hook:
                return self.filtered_read_hook(self.filtered_read_count, result)
            return result
        assert method == "PATCH" and path == f"/tables/{TABLE}/records"
        assert 1 <= len(body["records"]) <= migration.BATCH_SIZE
        for record in body["records"]:
            assert set(record) == {"id", "fields"}
            assert set(record["fields"]) == {"amount"}, "Migration modified a user-managed field"
            assert record["id"] in self.rows
            if not self.ignore_patches:
                self.rows[record["id"]]["amount"] = record["fields"]["amount"]
        if self.timeout_after_patch_once:
            self.timeout_after_patch_once = False
            raise TimeoutError("Synthetic accepted-write response timeout")
        return {"records": [{"id": record["id"]} for record in body["records"]]}

    def patches(self):
        return [body for method, _, body in self.calls if method == "PATCH"]


class BuildChangeTests(OfflineTestCase):
    def test_outgoing_and_incoming_preserve_magnitude_and_are_idempotent(self):
        for kind in migration.OUTGOING | migration.INCOMING:
            source = "0321" if kind in {"expense", "income"} else "900"
            expected = -1234.56 if kind in migration.OUTGOING else 1234.56
            for amount in (1234.56, -1234.56):
                with self.subTest(kind=kind, amount=amount):
                    row = bank_row(kind=kind, amount=amount, source=source)
                    original = copy.deepcopy(row)
                    change = migration.build_change(row)
                    self.assertEqual(change["after"], expected)
                    self.assertEqual(change["change"], amount != expected)
                    self.assertFalse(change["inferred"])
                    self.assertEqual(row, original)

    def test_informative_rows_clear_attempted_amounts_and_keep_blank_blank(self):
        for kind in migration.INFORMATIONAL:
            amounts = (None,) if kind == "unparsed" else (None, 0, 500.0)
            for amount in amounts:
                with self.subTest(kind=kind, amount=amount):
                    change = migration.build_change(bank_row(kind=kind, amount=amount, source="900"))
                    self.assertIsNone(change["after"])
                    self.assertEqual(change["change"], amount is not None)

    def test_missing_legacy_type_is_inferred_from_complete_synthetic_sms(self):
        cases = (
            ("0321", expense_text(), 1234.56, "expense", -1234.56),
            ("0321", income_text(), -1234.56, "income", 1234.56),
            ("0321", "Не сообщайте код никому: 000000", None, "otp", None),
            ("900", card_text("Перевод по СБП из TESTBANK +1 234,56р от Тестовый отправитель"),
             -1234.56, "sbp_in", 1234.56),
            ("900", "Подтвердите перевод с карты VISA0001 на карту MIR0002 на сумму 500р",
             500.0, "confirm", None),
        )
        for source, notes, amount, kind, expected in cases:
            with self.subTest(source=source, kind=kind):
                change = migration.build_change(bank_row(kind="", source=source, amount=amount, notes=notes))
                self.assertEqual(change["kind"], kind)
                self.assertEqual(change["after"], expected)
                self.assertTrue(change["inferred"])
                self.assertFalse(change["magnitude_mismatch"])

    def test_untyped_direction_preserves_curated_magnitude_and_flags_mismatch(self):
        for kind, text, amount, expected in (
            ("expense", expense_text(), 100.0, -100.0),
            ("expense", expense_text(), -100.0, -100.0),
            ("income", income_text(), -100.0, 100.0),
        ):
            with self.subTest(kind=kind, amount=amount):
                row = bank_row(kind="", amount=amount, notes=text)
                change = migration.build_change(row)
                self.assertEqual(change["kind"], kind)
                self.assertEqual(change["after"], expected)
                self.assertEqual(change["before"]["amount"], amount)
                self.assertTrue(change["inferred"])
                self.assertTrue(change["magnitude_mismatch"])

    def test_untyped_unknown_or_incomplete_sms_requires_review(self):
        for row in (
            bank_row(kind="", notes="Синтетическое неизвестное сообщение"),
            bank_row(kind="", source="900", notes="Синтетическое неизвестное сообщение"),
            bank_row(kind="", notes="СберБизнес. Списание синтетического неполного текста"),
        ):
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    migration.build_change(row)

    def test_manual_rows_invalid_mids_and_unknown_types_are_rejected(self):
        rows = [bank_row(source="AVq"), bank_row(kind="Manual custom type")]
        for mid in (None, 0, -1, True, "1001"):
            row = bank_row()
            row["fields"]["mid"] = mid
            rows.append(row)
        for row in rows:
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    migration.build_change(row)

    def test_monetary_rows_reject_missing_and_invalid_amounts(self):
        for amount in (None, "1234.56", True, False, float("nan"), float("inf"), float("-inf")):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    migration.build_change(bank_row(amount=amount))

    def test_informative_rows_also_reject_invalid_nonblank_amounts(self):
        for kind in migration.INFORMATIONAL:
            for amount in ("500", True, False, float("nan"), float("inf"), float("-inf")):
                with self.subTest(kind=kind, amount=amount):
                    with self.assertRaises(ValueError):
                        migration.build_change(bank_row(kind=kind, amount=amount, source="900"))

    def test_unparsed_row_with_amount_requires_manual_review(self):
        for amount in (0, 1234.56, -1234.56):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    migration.build_change(bank_row(kind="unparsed", amount=amount))

    def test_snapshot_retains_only_reversible_inputs_and_hashes_sms(self):
        row = bank_row()
        change = migration.build_change(row)
        self.assertEqual(set(change["before"]), {"mid", "source", "op_type", "amount", "notes_sha256"})
        self.assertEqual(len(change["before"]["notes_sha256"]), 64)
        self.assertNotIn(row["fields"]["notes"], json.dumps(change))
        self.assertNotIn("category", json.dumps(change))
        self.assertEqual(change["before"], migration.snapshot(row["fields"]))


class MigrationPlanTests(OfflineTestCase):
    def test_plan_is_read_only_and_excludes_manual_rows(self):
        api = FakeGrist([bank_row(1), bank_row(2, source="AVq")])
        original = copy.deepcopy(api.rows)
        plan = migration.make_plan(api)
        self.assertEqual([entry["id"] for entry in plan["entries"]], [1])
        self.assertEqual(plan["errors"], [])
        self.assertEqual(api.rows, original)
        self.assertEqual(api.patches(), [])
        self.assertTrue(plan["createdAt"].endswith("+10:00"))
        self.assertEqual(migration.summary(plan)["amount_changes"], 1)

    def test_wrong_amount_schema_prevents_scanning_or_writing(self):
        api = FakeGrist([bank_row()])
        api.amount_type = "Text"
        with self.assertRaises(ValueError):
            migration.make_plan(api)
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(api.patches(), [])

    def test_formula_amount_schema_prevents_scanning_or_writing(self):
        api = FakeGrist([bank_row()])
        api.amount_is_formula = True
        with self.assertRaises(ValueError):
            migration.make_plan(api)
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(api.patches(), [])

    def test_unresolved_rows_are_recorded_and_stop_apply_before_any_write(self):
        api = FakeGrist([bank_row(1), bank_row(2, kind="unparsed", amount=10)])
        plan = migration.make_plan(api)
        self.assertEqual([entry["id"] for entry in plan["entries"]], [1])
        self.assertEqual([error["id"] for error in plan["errors"]], [2])
        with self.assertRaises(ValueError):
            migration.apply_plan(api, plan)
        self.assertEqual(api.patches(), [])

    def test_bank_scan_is_filtered_and_keyset_paged(self):
        api = FakeGrist([bank_row(i) for i in range(1, 102)] + [bank_row(102, source="AVq")])
        rows = list(api.bank_rows())
        self.assertEqual([row["id"] for row in rows], list(range(1, 102)))
        sql = [body["sql"] for method, path, body in api.calls if (method, path) == ("POST", "/sql")]
        self.assertEqual(len(sql), 4)
        for text, last_id in zip(sql, (0, 50, 100, 101)):
            self.assertIn(f"AND id > {last_id} ORDER BY id LIMIT 50", text)


class ApplyMigrationTests(OfflineTestCase):
    def test_tampered_target_sign_or_magnitude_cannot_authorize_patch(self):
        for target in (1234.56, -999.99):
            with self.subTest(target=target):
                api = FakeGrist([bank_row()])
                original = copy.deepcopy(api.rows)
                plan = migration.make_plan(api)
                plan["entries"][0]["after"] = target
                with self.assertRaises(ValueError):
                    migration.apply_plan(api, plan)
                self.assertEqual(api.patches(), [])
                self.assertEqual(api.rows, original)

    def test_duplicate_plan_row_ids_cannot_authorize_patch(self):
        api = FakeGrist([bank_row()])
        original = copy.deepcopy(api.rows)
        plan = migration.make_plan(api)
        plan["entries"].append(copy.deepcopy(plan["entries"][0]))
        with self.assertRaises(ValueError):
            migration.apply_plan(api, plan)
        self.assertEqual(api.patches(), [])
        self.assertEqual(api.rows, original)

    def test_false_change_flag_cannot_skip_required_signed_amount_update(self):
        api = FakeGrist([bank_row()])
        original = copy.deepcopy(api.rows)
        plan = migration.make_plan(api)
        plan["entries"][0]["change"] = False
        with self.assertRaises(ValueError):
            migration.apply_plan(api, plan)
        self.assertEqual(api.patches(), [])
        self.assertEqual(api.rows, original)

    def test_stale_schema_halts_apply_before_scanning_or_writing(self):
        for change in ("amount_type", "amount_formula", "missing_op_type"):
            with self.subTest(change=change):
                api = FakeGrist([bank_row()])
                plan = migration.make_plan(api)
                if change == "amount_type":
                    api.amount_type = "Text"
                elif change == "amount_formula":
                    api.amount_is_formula = True
                else:
                    api.missing_columns.add("op_type")
                api.calls.clear()
                with self.assertRaises(ValueError):
                    migration.apply_plan(api, plan)
                self.assertEqual(len(api.calls), 1)
                self.assertEqual(api.patches(), [])

    def test_patches_only_amount_in_bounded_batches_and_preserves_user_fields(self):
        api = FakeGrist([bank_row(i, amount=i + 0.25) for i in range(1, 19)] + [bank_row(19, source="AVq")])
        original = copy.deepcopy(api.rows)
        plan = migration.make_plan(api)
        api.rows[1]["category"] = "New manual category"
        applied = migration.apply_plan(api, plan)
        self.assertEqual(applied, 18)
        self.assertEqual([len(body["records"]) for body in api.patches()], [8, 8, 2])
        for row_id, before in original.items():
            if before["source"] == "AVq":
                self.assertEqual(api.rows[row_id], before)
                continue
            self.assertEqual(api.rows[row_id]["amount"], -abs(before["amount"]))
            actual_others = {k: v for k, v in api.rows[row_id].items() if k != "amount"}
            expected_others = {k: v for k, v in before.items() if k != "amount"}
            if row_id == 1:
                expected_others["category"] = "New manual category"
            self.assertEqual(actual_others, expected_others)

    def test_stale_notes_amount_type_or_mid_halts_before_writing(self):
        edits = {"notes": "New synthetic SMS text", "amount": 99.0,
                 "op_type": "income", "mid": 9999, "source": "900"}
        for key, value in edits.items():
            with self.subTest(key=key):
                api = FakeGrist([bank_row()])
                plan = migration.make_plan(api)
                api.rows[1][key] = value
                with self.assertRaises(ValueError):
                    migration.apply_plan(api, plan)
                self.assertEqual(api.patches(), [])

    def test_missing_or_added_bank_rows_halt_before_writing(self):
        for action in ("missing", "added"):
            with self.subTest(action=action):
                api = FakeGrist([bank_row()])
                plan = migration.make_plan(api)
                if action == "missing":
                    del api.rows[1]
                else:
                    api.rows[2] = bank_row(2)["fields"]
                with self.assertRaises(ValueError):
                    migration.apply_plan(api, plan)
                self.assertEqual(api.patches(), [])

    def test_filtered_record_reads_require_exact_requested_ids(self):
        for mode in ("missing", "extra"):
            with self.subTest(mode=mode):
                api = FakeGrist([bank_row()])
                plan = migration.make_plan(api)

                def malformed_filtered_read(count, result):
                    if mode == "missing":
                        result["records"] = []
                    else:
                        result["records"].append({"id": 999, "fields": bank_row(999)["fields"]})
                    return result

                api.filtered_read_hook = malformed_filtered_read
                with self.assertRaises(ValueError):
                    migration.apply_plan(api, plan)
                self.assertEqual(api.patches(), [])

    def test_readback_mismatch_stops_before_later_batch(self):
        api = FakeGrist([bank_row(i) for i in range(1, 10)])
        plan = migration.make_plan(api)
        api.ignore_patches = True
        with self.assertRaisesRegex(ValueError, "read-back mismatch"):
            migration.apply_plan(api, plan)
        self.assertEqual([len(body["records"]) for body in api.patches()], [8])

    def test_stale_filtered_row_is_checked_again_before_patch(self):
        api = FakeGrist([bank_row()])
        plan = migration.make_plan(api)

        def edit_before_patch(count, result):
            if count == 1:
                result["records"][0]["fields"]["notes"] = "Edited synthetic SMS"
            return result

        api.filtered_read_hook = edit_before_patch
        with self.assertRaisesRegex(ValueError, "changed before update"):
            migration.apply_plan(api, plan)
        self.assertEqual(api.patches(), [])

    def test_rerunning_completed_plan_does_not_flip_amounts_again(self):
        api = FakeGrist([bank_row(1), bank_row(2, kind="income", amount=-1234.56),
                         bank_row(3, kind="confirm", amount=500, source="900")])
        plan = migration.make_plan(api)
        self.assertEqual(migration.apply_plan(api, plan), 3)
        original = copy.deepcopy(api.rows)
        patch_count = len(api.patches())
        self.assertEqual(migration.apply_plan(api, plan), 0)
        self.assertEqual(api.rows, original)
        self.assertEqual(len(api.patches()), patch_count)

    def test_resume_after_accepted_write_timeout_updates_only_remaining_rows(self):
        api = FakeGrist([bank_row(i) for i in range(1, 10)])
        plan = migration.make_plan(api)
        api.timeout_after_patch_once = True
        with self.assertRaises(TimeoutError):
            migration.apply_plan(api, plan)
        self.assertEqual([api.rows[i]["amount"] for i in range(1, 9)], [-1234.56] * 8)
        self.assertEqual(api.rows[9]["amount"], 1234.56)
        self.assertEqual(migration.apply_plan(api, plan), 1)
        self.assertEqual([len(body["records"]) for body in api.patches()], [8, 1])
        self.assertTrue(all(fields["amount"] == -1234.56 for fields in api.rows.values()))

    def test_by_ids_rejects_unbounded_reads_without_requesting(self):
        api = FakeGrist()
        for ids in ([], list(range(1, 10))):
            with self.subTest(ids=ids):
                with self.assertRaises(ValueError):
                    api.by_ids(ids)
        self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()
