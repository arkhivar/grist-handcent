"""Plan and apply the one-time bank amount migration without touching Handcent.

Read GRIST_API_KEY from the environment only. Default action is a read-only plan.
The private plan contains before/after amounts and note hashes, never SMS text or
credentials. Deploy the new writer and let old runs finish before applying it.
"""

import argparse
from collections import Counter
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sms_grist_sync as sync


OUTGOING = {"expense", "purchase", "purchase_sbp", "transfer_out", "payment",
            "fee", "court_collection"}
INCOMING = {"income", "transfer_in", "sbp_in", "refund"}
INFORMATIONAL = {"confirm", "declined", "promo", "otp", "unparsed"}
PAGE_SIZE = 50
BATCH_SIZE = 8


def snapshot(fields):
    """Only the migration inputs; no raw SMS or user-managed columns on disk."""
    return {**{k: fields.get(k) for k in ("mid", "source", "op_type", "amount")},
            "notes_sha256": hashlib.sha256(
                (fields.get("notes") or "").encode("utf-8")).hexdigest()}


def build_change(row):
    """Return a reversible plan entry; reject ambiguous or malformed bank data."""
    f = row["fields"]
    if f.get("source") not in {"0321", "900"}:
        raise ValueError("not a bank SMS row")
    if type(f.get("mid")) is not int or f["mid"] <= 0:
        raise ValueError("bank row has no positive integer mid")
    amount = f.get("amount")
    if amount is not None and (type(amount) not in (int, float) or not math.isfinite(amount)):
        raise ValueError("amount is neither blank nor a finite number")
    kind = f.get("op_type") or ""
    inferred = False
    magnitude_mismatch = False
    if not kind:
        message = {"mid": f["mid"], "ts": 0, "data": f.get("notes") or ""}
        if f["source"] == "0321":
            kind = sync.classify_0321(message["data"])
            if kind == "expense":
                parsed, error = sync.parse_expense(message)
            elif kind == "income":
                parsed, error = sync.parse_income(message)
            elif kind == "otp":
                parsed, error = {"amount": None}, None
            else:
                raise ValueError("missing type and unrecognized stored SMS")
        else:
            parsed, error = sync.parse_900(message)
            kind = parsed.get("op_type") if parsed else None
        if error or kind not in OUTGOING | INCOMING | INFORMATIONAL:
            raise ValueError("missing type and stored SMS cannot be parsed")
        if kind in OUTGOING | INCOMING:
            current = f.get("amount")
            if current is None:
                raise ValueError("monetary row has no finite numeric amount")
            # The SMS establishes direction; preserve any curated magnitude.
            magnitude_mismatch = not sync.amount_matches(abs(parsed["amount"]), abs(current))
        inferred = True

    if kind in OUTGOING | INCOMING:
        if type(amount) not in (int, float) or not math.isfinite(amount):
            raise ValueError("monetary row has no finite numeric amount")
        expected = -abs(amount) if kind in OUTGOING else abs(amount)
    elif kind in INFORMATIONAL:
        if kind == "unparsed" and amount is not None:
            raise ValueError("unparsed row has an amount requiring manual review")
        expected = None
    else:
        raise ValueError("unknown operation type")
    return {"id": row["id"], "before": snapshot(f), "kind": kind,
            "inferred": inferred, "after": expected,
            "magnitude_mismatch": magnitude_mismatch,
            "change": not sync.amount_matches(expected, amount)}


class Grist:
    def __init__(self, doc_id, table, key):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", doc_id):
            raise ValueError("invalid document ID")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise ValueError("invalid table ID")
        self.doc_id, self.table, self.key = doc_id, table, key
        self.base = "https://seoffice.getgrist.com/api/docs/" + doc_id

    def request(self, method, path, body=None):
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body, allow_nan=False).encode("utf-8") if body is not None else None,
            headers={"Authorization": "Bearer " + self.key,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as response:
            data = response.read()
        return json.loads(data) if data else None

    def columns(self):
        return self.request("GET", f"/tables/{self.table}/columns")["columns"]

    def bank_rows(self):
        last_id = 0
        while True:
            result = self.request("POST", "/sql", {"sql":
                f'SELECT id, mid, source, op_type, amount, notes FROM "{self.table}" '
                f"WHERE source IN ('0321', '900') AND id > {last_id} "
                f"ORDER BY id LIMIT {PAGE_SIZE}"})
            rows = result["records"]
            if not rows:
                return
            for row in rows:
                fields = row["fields"]
                row_id = fields["id"]
                if type(row_id) is not int or row_id <= last_id:
                    raise ValueError("SQL keyset pagination did not advance")
                last_id = row_id
                yield {"id": row_id, "fields": fields}

    def by_ids(self, ids):
        if not ids or len(ids) > BATCH_SIZE:
            raise ValueError("record reads must be bounded to 1..8 IDs")
        query = urllib.parse.urlencode({"filter": json.dumps({"id": ids})})
        result = self.request("GET", f"/tables/{self.table}/records?{query}")
        rows = {r["id"]: r["fields"] for r in result["records"]}
        if set(rows) != set(ids):
            raise ValueError("filtered read did not return exactly the requested row IDs")
        return rows


def require_schema(api):
    columns = {c["id"]: c["fields"] for c in api.columns()}
    required = {"amount", "mid", "source", "notes", "op_type"}
    if (not required <= set(columns) or columns["amount"]["type"] != "Numeric"
            or columns["amount"].get("isFormula")):
        raise ValueError("expected bank schema with Numeric amount and legacy op_type")
    return columns


def make_plan(api):
    columns = require_schema(api)
    entries, errors = [], []
    for row in api.bank_rows():
        try:
            entries.append(build_change(row))
        except ValueError as error:
            errors.append({"id": row["id"], "reason": str(error)})
    return {"version": 1, "document": api.doc_id, "table": api.table,
            "createdAt": datetime.datetime.now(sync.VLAT).isoformat(),
            "op_type_column": columns["op_type"], "entries": entries, "errors": errors}


def apply_plan(api, plan):
    require_schema(api)
    if plan["errors"]:
        raise ValueError("plan has unresolved rows; no changes applied")
    entries = plan["entries"]
    ids = [e["id"] for e in entries]
    if (any(type(row_id) is not int or row_id <= 0 for row_id in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("plan must contain unique positive integer row IDs")
    # Check the whole bank scope before writing: new/edited SMS require a new plan.
    current_rows = {r["id"]: r for r in api.bank_rows()}
    if set(current_rows) != set(ids):
        raise ValueError("bank row set changed; generate a fresh plan")
    for entry in entries:
        # A plan is a review artifact, not permission to write arbitrary amounts.
        # Recompute direction from current data, including for resumed writes.
        derived = build_change(current_rows[entry["id"]])
        target = entry["after"]
        original = entry["before"]["amount"]
        for value in (target, original):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError("plan amounts must be blank or finite numbers")
        if (target != derived["after"] or entry["kind"] != derived["kind"]
                or entry["inferred"] != derived["inferred"]
                or type(entry["change"]) is not bool
                or entry["change"] != (not sync.amount_matches(target, original))):
            raise ValueError("plan target is inconsistent with the signed-amount rules")
        after = {**entry["before"], "amount": entry["after"]}
        if snapshot(current_rows[entry["id"]]["fields"]) not in (entry["before"], after):
            raise ValueError(f"row {entry['id']} changed; generate a fresh plan")

    applied = 0
    changes = [e for e in entries if e["change"]]
    for i in range(0, len(changes), BATCH_SIZE):
        batch = changes[i:i + BATCH_SIZE]
        before = api.by_ids([e["id"] for e in batch])
        updates = []
        for entry in batch:
            actual = snapshot(before[entry["id"]])
            after = {**entry["before"], "amount": entry["after"]}
            if actual == after:
                continue  # Resume safely after an accepted write whose response timed out.
            if actual != entry["before"]:
                raise ValueError(f"row {entry['id']} changed before update")
            updates.append({"id": entry["id"], "fields": {"amount": entry["after"]}})
        if updates:
            api.request("PATCH", f"/tables/{api.table}/records", {"records": updates})
        verified = api.by_ids([e["id"] for e in batch])
        for entry in batch:
            after = {**entry["before"], "amount": entry["after"]}
            if snapshot(verified[entry["id"]]) != after:
                raise ValueError(f"read-back mismatch for row {entry['id']}")
        applied += len(updates)

    final = {r["id"]: snapshot(r["fields"]) for r in api.bank_rows()}
    expected = {e["id"]: {**e["before"], "amount": e["after"]} for e in entries}
    if final != expected:
        raise ValueError("bank rows changed during migration; column must be retained")
    return applied


def summary(plan):
    return {"bank_rows": len(plan["entries"]),
            "amount_changes": sum(e["change"] for e in plan["entries"]),
            "inferred_types": sum(e["inferred"] for e in plan["entries"]),
            "preserved_magnitude_differences": sum(e["magnitude_mismatch"] for e in plan["entries"]),
            "by_type": dict(sorted(Counter(e["kind"] for e in plan["entries"]).items())),
            "unresolved_rows": len(plan["errors"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doc-id", required=True)
    parser.add_argument("--table", default="Transactions")
    parser.add_argument("--plan", type=Path, default=Path(".local/signed-amounts-plan.json"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writers-updated", action="store_true",
                        help="Confirm new writer deployed and old runs finished before applying")
    args = parser.parse_args()
    key = os.environ.get("GRIST_API_KEY")
    if not key:
        parser.error("GRIST_API_KEY must be set in the environment")
    api = Grist(args.doc_id, args.table, key)
    if args.apply:
        if not args.writers_updated:
            parser.error("applying requires --writers-updated after coordinating deployment")
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        if (plan.get("version"), plan.get("document"), plan.get("table")) != (
                1, args.doc_id, args.table):
            raise ValueError("plan version or target does not match")
        print(json.dumps({"amount_updates_applied": apply_plan(api, plan)}))
    else:
        plan = make_plan(api)
        payload = json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False)
        args.plan.parent.mkdir(parents=True, exist_ok=True)
        # Never overwrite an earlier rollback snapshot: use a new filename.
        with args.plan.open("x", encoding="utf-8") as output:
            output.write(payload)
        print(json.dumps(summary(plan)))
        return 1 if plan["errors"] else 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as error:
        print(f"Grist HTTP {error.code}; stopped without retrying writes", file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError, TimeoutError) as error:
        # The exceptions above never include request headers or SMS bodies.
        print(f"Migration stopped: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
