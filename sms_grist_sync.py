"""SMS -> Grist sync pipeline for Kimi Work Blueprint Automation.

Zero-inbox edition: EVERY scanned message ends up as a verified Grist row in
the unified `RevenueBase` table and is then deleted from the phone — except
0321 OTP/code messages, which are deleted WITHOUT a row (standing rule).
Unparseable messages become op_type="unparsed" rows; nothing is kept.

Ports the proven logic from the battle-tested workspace scripts:
  - scan900.py          (REST pagination of Handcent msgs)
  - py_pipe.py          (browser-free websocket deletion pipe)
  - parse_expenses.py / parse_income.py / parse_900.py  (parsers, regexes verbatim)
  - grist_exp_write2.py / grist_t900_write2.py / grist_t900_verify.py
                        (chunked filter dedup, batch writes, read-back verify)
  - handcent-sms-dedup SKILL.md (0321 OTP classifier, ws protocol pitfalls)

Runtime contract: the runner imports this module and calls run(ctx).
No top-level side effects. No network at import time. Never prints.

Config resolution (load_config): env vars win — HANDCENT_AUTH (full JSON
string, replaces auth-headers.json) and GRIST_API_KEY (plain string) —
falling back to files next to this module (auth-headers.json,
grist_api.txt, f900-unsent.json stays file-based). Secrets are never
printed or logged.
"""

import datetime
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Constants (all proven values from the source scripts / SKILL.md)
# ---------------------------------------------------------------------------

HANDCENT_BASE = "https://aw.handcent.com"
DEVICE_ID = "2555969"
WS_URL = "wss://aw.handcent.com/ws"
WS_IDENTITY = "seoffice@0_android_acc2959783cb55fe"   # py_pipe.py / SKILL.md
DELETE_HASH = 366922896                                # py_pipe.py
CID_0321 = 276
CID_900 = 277

GRIST_DOC = "https://seoffice.getgrist.com/api/docs/tRknrJrfbW3L/tables"
TABLE = "Transactions"  # single Grist target (renames are a one-line change)

HTTP_TIMEOUT = 60          # seconds, per spec
SCAN_PAGE_PAUSE = 0.4      # scan900.py
GRIST_CHUNK = 8            # *_write2.py / grist_t900_verify.py
GRIST_CHUNK_PAUSE = 0.35   # 0.3-0.4s between chunks
WRITE_BATCH = 25           # *_write2.py
WRITE_BATCH_PAUSE = 0.5
FRAME_SPACING = 0.8        # py_pipe.py
QUIET_PERIOD = 240         # ~4 min server lag (SKILL.md)
MAX_ROUNDS = 3
MAX_LOG_LINES = 40

VLAT = datetime.timezone(datetime.timedelta(hours=10))  # Asia/Vladivostok

WORKSPACE_FALLBACK = r"C:\Users\Userman\Documents\kimi\workspace"

# ---------------------------------------------------------------------------
# In-memory log (never contains secrets)
# ---------------------------------------------------------------------------

_LOG = []


def log(msg):
    stamp = datetime.datetime.now(VLAT).strftime("%H:%M:%S")
    _LOG.append(f"{stamp} {msg}")
    if len(_LOG) > 200:
        del _LOG[:100]


class TokenExpired(Exception):
    """REST returned 401 — auth token needs a manual browser refresh."""


# ---------------------------------------------------------------------------
# Secrets / config resolution (same dir as __file__, then workspace fallback)
# ---------------------------------------------------------------------------

def _resolve(filename):
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if os.path.exists(here):
        return here
    there = os.path.join(WORKSPACE_FALLBACK, filename)
    if os.path.exists(there):
        return there
    return None


def load_config():
    cfg = {"headers": None, "grist_key": None, "f900_unsent": []}

    # Env vars first (GitHub Actions / cloud phase); file fallback keeps the
    # local Kimi Work automation working unchanged. Values never logged.
    env_auth = os.environ.get("HANDCENT_AUTH")
    if env_auth:
        cfg["headers"] = json.loads(env_auth)  # flat: res / token / username
    else:
        p = _resolve("auth-headers.json")
        if p:
            with open(p, encoding="utf-8") as f:
                cfg["headers"] = json.load(f)  # flat: res / token / username

    env_key = os.environ.get("GRIST_API_KEY")
    if env_key:
        cfg["grist_key"] = env_key.strip()
    else:
        p = _resolve("grist_api.txt")
        if p:
            with open(p, encoding="utf-8") as f:
                cfg["grist_key"] = f.read().strip()

    p = _resolve("f900-unsent.json")
    if p:
        try:
            with open(p, encoding="utf-8") as f:
                v = json.load(f)
            if isinstance(v, list):
                cfg["f900_unsent"] = [int(x) for x in v]
        except Exception:
            log("f900-unsent.json unreadable, ignoring")

    return cfg


# ---------------------------------------------------------------------------
# HTTP helpers (urllib only; secrets stay out of logs)
# ---------------------------------------------------------------------------

def http_json(method, url, headers=None, body=None, timeout=HTTP_TIMEOUT,
              tries=1, backoff=5.0, what=""):
    """JSON HTTP call. Raises TokenExpired on HTTP 401, RuntimeError after
    exhausting retries. Never logs request headers."""
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url, method=method,
                data=json.dumps(body).encode("utf-8") if body is not None else None,
                headers=dict(headers or {}),
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise TokenExpired(f"401 on {what or url}")
            last = e
        except TokenExpired:
            raise
        except Exception as e:  # URLError, timeout, JSON errors, ...
            last = e
        if attempt < tries - 1:
            log(f"retry {attempt + 1} {what} ({type(last).__name__})")
            time.sleep(backoff)
    raise RuntimeError(f"{what or url} failed: {type(last).__name__}: {last}")


def handcent_get(path, headers, what="", tries=2):
    return http_json("GET", HANDCENT_BASE + path, headers=headers,
                     timeout=HTTP_TIMEOUT, tries=tries, backoff=4.0,
                     what=what or path)


def grist_call(key, table, method, path, body=None, tries=4, what=""):
    return http_json(method, GRIST_DOC + "/" + table + path,
                     headers={"Authorization": "Bearer " + key,
                              "Content-Type": "application/json"},
                     body=body, timeout=HTTP_TIMEOUT, tries=tries, backoff=5.0,
                     what=what or f"grist {table} {method}")


def grist_fetch_fields(key, table, mids, chunk=GRIST_CHUNK, pause=GRIST_CHUNK_PAUSE):
    """Chunked mid-filter read. NEVER full-table /records reads.
    Returns {mid: fields}."""
    found = {}
    mids = [m for m in mids if m is not None]
    for i in range(0, len(mids), chunk):
        part = mids[i:i + chunk]
        f = urllib.parse.quote(json.dumps({"mid": part}))
        d = grist_call(key, table, "GET", f"/records?filter={f}",
                       what=f"grist {table} filter")
        for rec in (d or {}).get("records", []):
            m = (rec.get("fields") or {}).get("mid")
            if m is not None:
                found[m] = rec["fields"]
        if i + chunk < len(mids):
            time.sleep(pause)
    return found


# ---------------------------------------------------------------------------
# Handcent REST scan (ported from scan900.py)
# ---------------------------------------------------------------------------

def _page_items(d):
    """Defensive JSON shape fallback (scan900.py)."""
    if isinstance(d, list):
        return d
    if not isinstance(d, dict):
        return []
    return d.get("msgs") or d.get("data") or d.get("messages") or []


def _norm_msg(m, cid):
    return {
        "mid": m.get("mid") or m.get("id"),
        "ts": m.get("timestamp") or m.get("ts") or m.get("date"),
        "type": m.get("messageType", m.get("type")),
        "data": m.get("data") or m.get("text") or m.get("body") or "",
        "cid": cid,
    }


def paginate_conversation(cid, headers, max_pages=80):
    """Pages backwards through the FULL conversation, 30 msgs/page.
    No known-in-Grist early stop: under zero-inbox a message may already
    have a RevenueBase row and still be on the phone (it is then a
    deletion target), so every page must be scanned."""
    msgs, seen = {}, set()
    before = None
    for rnd in range(max_pages):
        url = f"/nws/any/a/msgs?deviceId={DEVICE_ID}&cid={cid}"
        if before is not None:
            url += f"&before_timestamp={before}"
        d = handcent_get(url, headers, what=f"scan cid={cid} p{rnd + 1}")
        items = [_norm_msg(m, cid) for m in _page_items(d)]
        items = [m for m in items if m["mid"] is not None and m["mid"] not in seen]
        if not items:
            break
        for m in items:
            seen.add(m["mid"])
            msgs[m["mid"]] = m
        log(f"scan cid={cid} page {rnd + 1}: {len(items)} items, total={len(msgs)}")
        if len(items) < 30:
            break
        before = min((m["ts"] or 0) for m in items) - 1
        time.sleep(SCAN_PAGE_PAUSE)
    return sorted(msgs.values(), key=lambda m: (m["ts"] or 0), reverse=True)


def rescan_conversation(cid, headers):
    """Full rescan for deletion verification."""
    return paginate_conversation(cid, headers)


# ---------------------------------------------------------------------------
# Parsers (regexes verbatim from parse_expenses.py / parse_income.py / parse_900.py)
# ---------------------------------------------------------------------------

AMT_EI = r"(-?[\d\s]+,\d{2})р"                       # 0321 amounts
AMT_900 = r"(\+?-?[\d\s\xa0]+(?:[.,]\d{1,2})?)р"    # 900 amounts

RE_EXPENSE = re.compile(  # parse_expenses.py
    r"^СберБизнес\. Списание " + AMT_EI +
    r" с р/c\*(\d+)(?: на р/с\*(\d+))?" +
    r"(?: по документу №(\S+) от (\d{2}\.\d{2}\.\d{4})| по (\d+) документам?)?" +
    r"(?: в пользу (.*))?" +
    r"\. Баланс " + AMT_EI + r"(?: (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}))?$"
)

RE_INCOME = re.compile(  # parse_income.py
    r"^СберБизнес\. Поступление " + AMT_EI +
    r" на р/c\*(\d+)(?: с р/с\*(\d+))?" +
    r"(?: по документу №(\S+) от (\d{2}\.\d{2}\.\d{4})| по (\d+) документам?)?" +
    r"(?: от (.*))?" +
    r"\. Баланс " + AMT_EI + r"(?: (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}))?$"
)

CARD_RX = re.compile(  # parse_900.py
    r"^Счёт карты (VISA\d+) (\d{2}:\d{2}) (.+?) Баланс: " + AMT_900 + r"(?:\s*«(.*)»)?$"
)
OPS_900 = [  # parse_900.py: (rx, op_type, direction, amount_group, cp_group)
    (re.compile(r"^Перевод по СБП из (\S+) \+" + AMT_900 + r" от (.+)$"), "sbp_in", "in", 1, 2),
    (re.compile(r"^Перевод " + AMT_900 + r" от (.+)$"), "transfer_in", "in", 0, 1),
    (re.compile(r"^перевод " + AMT_900 + r"\s*(.*)$"), "transfer_out", "out", 0, 1),
    (re.compile(r"^Покупка по СБП " + AMT_900 + r" (.+)$"), "purchase_sbp", "out", 0, 1),
    (re.compile(r"^Отмена покупки по СБП " + AMT_900 + r" (.+)$"), "refund", "in", 0, 1),
    (re.compile(r"^Покупка " + AMT_900 + r" (.+)$"), "purchase", "out", 0, 1),
    (re.compile(r"^Оплата уведомлений " + AMT_900 + r" до ([\d.]+)\.?$"), "fee", "out", 0, None),
    (re.compile(r"^Оплата " + AMT_900 + r"\s*(.*)$"), "payment", "out", 0, 1),
]
CONFIRM_RX = re.compile(  # parse_900.py
    r"^Подтвердите перевод с карты (VISA\d+) на карту (MIR\d+) на сумму " + AMT_900)
DECLINED_RX = re.compile(  # parse_900.py
    r"^Счёт карты (VISA\d+) Недостаточно средств\. (Покупка|перевод) " + AMT_900 +
    r"\s*(.*?) Баланс: " + AMT_900 + r"$")
COURT_RX = re.compile(  # parse_900.py
    r"^(\d{2}\.\d{2}\.\d{2}) по требованию взыскателя с карты (VISA\d+) взыскана сумма " +
    AMT_900 + r" по судебному приказу №(\S+) от ([\d.]+)\.")


def to_float(s):  # parse_expenses.py / parse_900.py
    if s is None:
        return None
    return float(s.replace("\xa0", "").replace(" ", "").replace(",", ".").lstrip("+"))


def is_otp_0321(text):
    """0321 OTP/code classifier (SKILL.md OTP purge): every code message
    contains the catch-all phrase — matched exactly the 489 codes and
    nothing else. OTPs are deleted WITHOUT a Grist row (standing rule)."""
    return "Не сообщайте код никому" in text


def classify_0321(text):
    """Returns 'expense' | 'income' | 'otp' | None.
    None means UNPARSED under zero-inbox rules: the message still gets a
    RevenueBase row (op_type="unparsed") and is deleted after verification.
    There are NO keeper/exempt messages anymore."""
    if text.startswith("СберБизнес. Списание"):
        return "expense"
    if text.startswith("СберБизнес. Поступление"):
        return "income"
    if is_otp_0321(text):
        return "otp"
    return None


def parse_expense(m):
    """Returns (row, None) or (None, error). Regex verbatim from parse_expenses.py."""
    r = RE_EXPENSE.match(m["data"])
    if not r:
        return None, f"expense-regex mid={m['mid']}"
    amount, acc_from, acc_to, doc_no, doc_dt, n_docs, cp, bal, bal_dt = r.groups()
    return {
        "mid": m["mid"], "sms_ts": m["ts"], "text": m["data"],
        "amount": to_float(amount),
        "account_from": acc_from, "account_to": acc_to,
        "doc_number": doc_no or (f"{n_docs} docs" if n_docs else None),
        "counterparty": (cp or "").strip() or None,
        "balance_after": to_float(bal),
    }, None


def parse_income(m):
    """Returns (row, None) or (None, error). Regex verbatim from parse_income.py."""
    r = RE_INCOME.match(m["data"])
    if not r:
        return None, f"income-regex mid={m['mid']}"
    amount, acc_to, acc_from, doc_no, doc_dt, n_docs, cp, bal, bal_dt = r.groups()
    return {
        "mid": m["mid"], "sms_ts": m["ts"], "text": m["data"],
        "amount": to_float(amount),
        "account_to": acc_to, "account_from": acc_from,
        "doc_number": doc_no or (f"{n_docs} док." if n_docs else None),
        "counterparty": (cp or "").strip() or None,
        "balance_after": to_float(bal),
    }, None


def parse_900(m):
    """Verbatim port of parse_900.py parse() (minus the dropped `card`
    column). Returns (row, err); err set means the caller writes an
    op_type="unparsed" row instead."""
    t = m["data"]
    base = {"mid": m["mid"], "sms_ts": m["ts"], "text": t,
            "op_type": None, "amount": None,
            "counterparty": None, "balance_after": None}
    c = CARD_RX.match(t)
    if c:
        _card, hhmm, op, bal, comment = c.groups()
        base.update(balance_after=to_float(bal))
        for rx, op_type, _dir, ia, ic in OPS_900:
            r = rx.match(op)
            if r:
                g = r.groups()
                base.update(op_type=op_type, amount=to_float(g[ia]))
                cp = (g[ic] or "").strip() if ic is not None else ""
                if op_type == "sbp_in":
                    cp = f"{(g[ic] or '').strip()} (СБП {g[0]})"
                base["counterparty"] = cp or None
                return base, None
        return base, f"NO-OP-MATCH mid={m['mid']}"
    d2 = DECLINED_RX.match(t)
    if d2:
        base.update(op_type="declined",
                    amount=to_float(d2.group(3)), counterparty=(d2.group(4) or "").strip() or None,
                    balance_after=to_float(d2.group(5)))
        return base, None
    c3 = COURT_RX.match(t)
    if c3:
        base.update(op_type="court_collection",
                    amount=to_float(c3.group(3)),
                    counterparty=f"взыскатель, судебный приказ №{c3.group(4)} от {c3.group(5)}")
        return base, None
    if "Никому не сообщайте код" in t:
        base.update(op_type="otp")
        return base, None
    c2 = CONFIRM_RX.match(t)
    if c2:
        base.update(op_type="confirm",
                    counterparty=f"на карту {c2.group(2)}", amount=to_float(c2.group(3)))
        return base, None
    if t.startswith("Дмитрий Владимирович"):
        base.update(op_type="promo")
        return base, None
    return base, f"NO-CLASS mid={m['mid']}"


def unparsed_row(m):
    """Zero-inbox fallback row for any message the parsers cannot classify."""
    return {"mid": m["mid"], "sms_ts": m["ts"], "text": m["data"],
            "op_type": "unparsed"}


# ---------------------------------------------------------------------------
# Row builder (RevenueBase schema; only known fields are sent, None-valued
# keys are OMITTED — so unparsed rows carry no amount/counterparty)
# ---------------------------------------------------------------------------

def fields_txn(r, source):
    f = {"datetime": r["sms_ts"] // 1000, "notes": r["text"], "mid": r["mid"],
         "source": source,
         "op_type": r.get("op_type"),
         "amount": r.get("amount"), "counterparty": r.get("counterparty"),
         "balance_after": r.get("balance_after"),
         "doc_number": r.get("doc_number"),
         "account_to": r.get("account_to"), "account_from": r.get("account_from")}
    dn = f.get("doc_number")
    if dn and dn.endswith(" docs"):
        f["doc_number"] = dn.replace(" docs", " док.")  # grist_exp_write2.py quirk
    return {k: v for k, v in f.items() if v is not None}


def fold_twins(rows):
    """TWIN-FOLD (parse_income.py / grist_*_write2.py): byte-identical texts
    fold to ONE row, newest mid wins. Returns (unique_rows, text_to_mids);
    every mid in a verified group becomes a deletion target."""
    by_text = {}
    for r in sorted(rows, key=lambda r: r["mid"], reverse=True):  # newest mid wins
        by_text.setdefault(r["text"], []).append(r)
    unique = [rs[0] for rs in by_text.values()]
    text_to_mids = {t: [r["mid"] for r in rs] for t, rs in by_text.items()}
    return unique, text_to_mids


# ---------------------------------------------------------------------------
# Writer: batch POST + chunked read-back verify (grist_*_write2.py pattern)
# ---------------------------------------------------------------------------

def write_and_verify(key, table, unique_rows, fields_fn, check_amount):
    """Writes missing rows in batches of 25, then read-back-verifies every
    mid via chunked filter. Returns (rows_written, verified_mids set)."""
    mids = [r["mid"] for r in unique_rows]
    have = grist_fetch_fields(key, table, mids) if mids else {}
    todo = [r for r in unique_rows if r["mid"] not in have]
    written = 0
    for i in range(0, len(todo), WRITE_BATCH):
        chunk = todo[i:i + WRITE_BATCH]
        grist_call(key, table, "POST", "/records",
                   {"records": [{"fields": fields_fn(r)} for r in chunk]},
                   what=f"grist {table} POST")
        written += len(chunk)
        log(f"grist {table}: wrote {written}/{len(todo)}")
        time.sleep(WRITE_BATCH_PAUSE)
    time.sleep(1)
    by_mid = grist_fetch_fields(key, table, mids) if mids else {}
    missing = [m for m in mids if m not in by_mid]
    bad = []
    if check_amount:
        bad = [r["mid"] for r in unique_rows if r["mid"] in by_mid
               and abs((by_mid[r["mid"]].get("amount") or 0) - (r.get("amount") or 0)) > 0.005]
    if missing or bad:
        log(f"grist {table}: VERIFY incomplete missing={missing[:8]} bad={bad[:8]}")
    else:
        log(f"grist {table}: VERIFY OK ({len(by_mid)}/{len(mids)})")
    return written, set(by_mid) - set(bad)


# ---------------------------------------------------------------------------
# Websocket deletion pipe (ported from py_pipe.py)
# ---------------------------------------------------------------------------

def ws_token(headers):
    d = http_json("POST", HANDCENT_BASE + "/nws/any/a/ws/token",
                  headers={**headers, "Content-Type": "application/json"},
                  body={"agent": "test"}, timeout=30, tries=2, what="ws/token")
    return (d or {}).get("data", {}).get("value") or (d or {}).get("value") \
        or (d or {}).get("token")


def open_pipe(headers):
    """Fresh token + fresh socket + MANDATORY ws_config subscribe, wait ws_info."""
    from websockets.sync.client import connect
    tok = ws_token(headers)
    sock = connect(WS_URL, open_timeout=20, close_timeout=5)
    cfg = {"type": "ws_config", "data": {
        "method": "aw_data", "name": WS_IDENTITY, "source": WS_IDENTITY,
        "topic": [WS_IDENTITY],
        "user_token": tok, "reply_topic": [f"topic:{WS_IDENTITY}"],
        "to": ["topic:aw_redis_server", f"source:{WS_IDENTITY}"]}}
    sock.send(json.dumps(cfg))
    t0 = time.time()
    while time.time() - t0 < 10:
        try:
            f = json.loads(sock.recv(timeout=8))
            if f.get("type") == "ws_info":
                return sock
        except Exception:
            break
    try:
        sock.close()
    except Exception:
        pass
    raise RuntimeError("ws subscribe failed (no ws_info)")


def delete_frame(m):
    return json.dumps({"type": "user_data", "data": {"type": "aw_data", "data": {
        "group": 1, "sms": {"date": m["ts"], "id": m["mid"], "cid": m["cid"],
                            "type": m["type"], "mode": "dm", "hash": DELETE_HASH}}}})


def delete_burst(targets, headers):
    """Self-healing send loop (py_pipe.py): reconnect with a FRESH token on any
    send error/closure, continue. Never trusts send() success."""
    from websockets.exceptions import ConnectionClosed
    sock, sent, i, consec_errors = None, 0, 0, 0
    try:
        while i < len(targets):
            try:
                if sock is None:
                    sock = open_pipe(headers)
                    consec_errors = 0
                    log(f"pipe subscribed ({sent}/{len(targets)} sent)")
                sock.send(delete_frame(targets[i]))
                sent += 1
                i += 1
                time.sleep(FRAME_SPACING)
            except (ConnectionClosed, OSError, RuntimeError) as e:
                consec_errors += 1
                log(f"pipe dead: {type(e).__name__} ({consec_errors})")
                try:
                    sock and sock.close()
                except Exception:
                    pass
                sock = None
                if consec_errors > 8:
                    raise RuntimeError("pipe keeps dying, aborting burst")
                time.sleep(3)
    finally:
        try:
            sock and sock.close()
        except Exception:
            pass
    return sent


def delete_with_verification(cid, headers, target_msgs):
    """HARD RULE: every mid here already has a Grist-verified RevenueBase row
    (or is a 0321 OTP). Sends frames, waits out the server lag, full REST
    rescan; resends stragglers up to MAX_ROUNDS total rounds. Deletion
    confirmed ONLY by absence in the msgs LIST (never /msg/text/<mid> — it
    200s on deleted). Returns (sms_deleted, final_msgs, sent_total)."""
    targets = {m["mid"]: m for m in target_msgs}
    remaining = set(targets)
    sent_total = 0
    final_msgs = []
    rounds = 0
    while rounds < MAX_ROUNDS and remaining:
        rounds += 1
        burst = [targets[mid] for mid in sorted(remaining)]
        sent = delete_burst(burst, headers)
        sent_total += sent
        log(f"round {rounds}: sent {sent} frames")
        if sent == 0:
            log("round sent nothing, stopping resend loop")
            break
        time.sleep(QUIET_PERIOD)  # server executes in bulk after quiet
        final_msgs = rescan_conversation(cid, headers)
        present = {m["mid"] for m in final_msgs}
        remaining &= present
        log(f"round {rounds}: {len(remaining)} stragglers remain")
    if not sent_total:
        log("nothing sent; pending from initial scan")
    sms_deleted = len(targets) - len(remaining)
    if remaining:
        log(f"undeleted after {rounds} rounds: {sorted(remaining)[:10]}")
    return sms_deleted, final_msgs, sent_total


# ---------------------------------------------------------------------------
# Per-sender pipelines (zero-inbox: every scanned mid lands in the deletion
# pipeline — parsed row, unparsed row, or 0321 OTP)
# ---------------------------------------------------------------------------

def _entry():
    return {"scanned": 0, "newFound": 0, "rowsWritten": 0, "smsDeleted": 0,
            "pending": 0, "parseFailures": 0, "lastSync": None, "ok": True}


def _finish(entry):
    entry["lastSync"] = datetime.datetime.now(VLAT).isoformat(timespec="seconds")


def run_sender(cfg, cid, source, codes):
    """Unified zero-inbox pipeline for one sender.
    Deletion set = union of:
      * mids of every twin group whose representative row read-back-verified
        in RevenueBase (parsed AND unparsed rows alike), plus
      * 0321 OTP mids (deleted WITHOUT any Grist row).
    No message class is exempt."""
    headers, key = cfg["headers"], cfg["grist_key"]
    entry = _entry()

    msgs = paginate_conversation(cid, headers)
    entry["scanned"] = len(msgs)
    by_mid = {m["mid"]: m for m in msgs}

    rows, otp_msgs, unparsed = [], [], []
    if source == "0321":
        for m in msgs:
            kind = classify_0321(m["data"])
            if kind == "expense":
                row, err = parse_expense(m)
                if err:
                    log(f"parse fail ({err}); -> unparsed row")
                    unparsed.append(m)
                else:
                    row["op_type"] = "expense"
                    rows.append(row)
            elif kind == "income":
                row, err = parse_income(m)
                if err:
                    log(f"parse fail ({err}); -> unparsed row")
                    unparsed.append(m)
                else:
                    row["op_type"] = "income"
                    rows.append(row)
            elif kind == "otp":
                otp_msgs.append(m)
            else:  # payroll notices, promos, strays — zero-inbox: unparsed row
                unparsed.append(m)
    else:  # 900
        for m in msgs:
            row, err = parse_900(m)
            if err:
                log(f"unclassified ({err}); -> unparsed row")
                unparsed.append(m)
            else:
                rows.append(row)

    for m in otp_msgs:
        codes.append({"sender": source,
                      "date": datetime.datetime.fromtimestamp(m["ts"] / 1000, VLAT)
                      .isoformat(timespec="seconds"),
                      "text": m["data"].strip()})
    for r in rows:
        if r.get("op_type") == "otp":
            codes.append({"sender": source,
                          "date": datetime.datetime.fromtimestamp(r["sms_ts"] / 1000, VLAT)
                          .isoformat(timespec="seconds"),
                          "text": r["text"].strip()})

    all_rows = rows + [unparsed_row(m) for m in unparsed]
    entry["newFound"] = len(all_rows) + len(otp_msgs)
    unique, text_to_mids = fold_twins(all_rows)
    for r in unique:
        if r.get("op_type") == "unparsed":
            log(f"unparsed mid={r['mid']} -> row")
    entry["parseFailures"] = sum(1 for r in unique if r.get("op_type") == "unparsed")

    written, verified = write_and_verify(
        key, TABLE, unique,
        lambda r: fields_txn(r, source), check_amount=True)
    entry["rowsWritten"] = written

    del_msgs = list(otp_msgs)  # 0321 OTPs: deleted WITHOUT any Grist row
    for text, mids in text_to_mids.items():
        if any(mid in verified for mid in mids):
            del_msgs.extend(by_mid[mid] for mid in mids if mid in by_mid)

    del_mids = {m["mid"] for m in del_msgs}
    target_msgs = [m for m in del_msgs if m["mid"] in del_mids]
    if target_msgs:
        sms_deleted, final_msgs, _ = delete_with_verification(cid, headers, target_msgs)
        entry["smsDeleted"] = sms_deleted
        entry["pending"] = len(final_msgs) if final_msgs else len(msgs)
    else:
        log(f"{source}: nothing to delete")
        entry["pending"] = len(msgs)
    _finish(entry)
    return entry


# ---------------------------------------------------------------------------
# Module entry point (Blueprint Automation contract)
# ---------------------------------------------------------------------------

def run(ctx):
    """Entry point. ctx.input: {"cid": "276"|"277"|"all"|"0321"|"900"}.
    Returns {"artifact": {...}}. Never raises for per-sender failures;
    a REST 401 sets tokenExpired and stops gracefully."""
    inp = getattr(ctx, "input", None)
    if inp is None and isinstance(ctx, dict):
        inp = ctx.get("input")
    inp = inp or {}
    requested = str(inp.get("cid", "all"))

    alias = {"276": "0321", "0321": "0321", "277": "900", "900": "900"}
    senders = ["0321", "900"] if requested.lower() == "all" \
        else [alias.get(requested)] if alias.get(requested) else ["0321", "900"]

    artifact = {
        "generatedAt": datetime.datetime.now(VLAT).isoformat(timespec="seconds"),
        "requested": requested,
        "tokenExpired": False,
        "senders": {"0321": _entry(), "900": _entry()},
        "latestCodes": [],
        "log": [],
    }
    for s in ("0321", "900"):
        _finish(artifact["senders"][s])  # not-requested senders: ok=true, zeros

    try:
        cfg = load_config()
    except Exception as e:
        for s in senders:
            artifact["senders"][s]["ok"] = False
            artifact["senders"][s]["error"] = f"config load failed: {type(e).__name__}"
        artifact["log"] = _LOG[-MAX_LOG_LINES:]
        return {"artifact": artifact}
    if not cfg["headers"] or not cfg["grist_key"]:
        for s in senders:
            artifact["senders"][s]["ok"] = False
            artifact["senders"][s]["error"] = "missing auth config (HANDCENT_AUTH or auth-headers.json; GRIST_API_KEY or grist_api.txt)"
        artifact["log"] = _LOG[-MAX_LOG_LINES:]
        return {"artifact": artifact}

    codes = []
    cur = None
    try:
        for cur in senders:
            cid = CID_0321 if cur == "0321" else CID_900
            artifact["senders"][cur] = run_sender(cfg, cid, cur, codes)
        # f900-unsent.json (900 run only): mids whose deletion frames were never
        # sent. Rows were verified yesterday; re-verify against RevenueBase,
        # then delete the ones still on the phone.
        if "900" in senders and cfg["f900_unsent"]:
            f900 = cfg["f900_unsent"]
            checked = grist_fetch_fields(cfg["grist_key"], TABLE, f900)
            ok_mids = set(checked)
            skipped = [m for m in f900 if m not in ok_mids]
            if skipped:
                log(f"f900-unsent not in {TABLE}, skipped: {skipped[:10]}")
            if ok_mids:
                scan = {m["mid"]: m for m in
                        rescan_conversation(CID_900, cfg["headers"])}
                present = [scan[mid] for mid in sorted(ok_mids) if mid in scan]
                gone = sorted(ok_mids - set(scan))
                if gone:
                    log(f"f900-unsent already off the phone: {gone[:10]}")
                if present:
                    log(f"f900-unsent: deleting {len(present)} verified mids")
                    deleted, final_msgs, _ = delete_with_verification(
                        CID_900, cfg["headers"], present)
                    artifact["senders"]["900"]["smsDeleted"] += deleted
                    if final_msgs:
                        artifact["senders"]["900"]["pending"] = len(final_msgs)
    except TokenExpired:
        artifact["tokenExpired"] = True
        if cur:
            artifact["senders"][cur]["ok"] = False
            artifact["senders"][cur]["error"] = "token expired (401)"
        log("REST token expired (401) — refresh via browser localStorage, then rerun")
    except Exception as e:
        if cur:
            artifact["senders"][cur]["ok"] = False
            artifact["senders"][cur]["error"] = f"{type(e).__name__}: {e}"
        log(f"sender {cur} crashed: {type(e).__name__}: {e}")

    codes.sort(key=lambda c: c["date"], reverse=True)  # newest first
    artifact["latestCodes"] = codes[:6]
    artifact["log"] = _LOG[-MAX_LOG_LINES:]
    return {"artifact": artifact}
