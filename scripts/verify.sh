#!/usr/bin/env bash
# Canonical verification for hermes-trading. No pytest suite here by design:
# the worker is a thin loop over live adapters, so what needs guarding is the
# strategy logic and the reflection plumbing around it.
#
#   scripts/verify.sh
#
# Two conventions this file depends on, both learned the hard way:
#   * Windows Python cannot read MSYS '/c/...' paths (it resolves them to a
#     drive-less '\c\...'), so anything crossing that boundary is built native.
#   * Never `producer | grep -q`: grep exits on first match, SIGPIPEs the
#     producer and poisons its exit status. Capture to a var, then test.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/Scripts/python.exe"; [ -x "$PY" ] || PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || { echo "no venv python at $PY"; exit 1; }
WATCH="${HERMES_WATCH_SCRIPT:-/c/Users/tanay/AppData/Local/hermes/scripts/hermes_trading_watch.sh}"
MARK="$REPO/state/.last_reflection_count"
WTMP="$(cygpath -w "${TMPDIR:-/tmp}" 2>/dev/null || echo "${TMPDIR:-/tmp}")/hermes-verify-$$"
BTMP="$(cygpath -u "$WTMP" 2>/dev/null || echo "$WTMP")"
cd "$REPO" || exit 1

pass=0; fail=0
ok(){ echo "  PASS $1"; pass=$((pass+1)); }
no(){ echo "  FAIL $1"; fail=$((fail+1)); shift; [ $# -gt 0 ] && printf '        %s\n' "$@"; }
t(){ local out; out=$("$PY" -c "$2" 2>&1); [ "$out" = ok ] && ok "$1" || no "$1" "$out"; }

H='
from hermes_trading.loop import evaluate_exit
def md(close, rsi=50.0): return {"data": {"close": close, "rsi": rsi}}
def pos(entry=100.0, d="long"): return {"entry_price": entry, "direction": d, "size": 0.5}
S = {"exit": {"take_profit_pct": 1.5, "rsi_exit": 70}}
'

echo "== strategy.yaml: exit is real, tunable config =="
t "current version carries a populated exit block" '
import yaml; s=yaml.safe_load(open("state/strategy.yaml"))
e=s.get("exit") or {}
assert s["version"].isdigit(), s["version"]
assert {"take_profit_pct","rsi_exit"} <= set(e), e
print("ok")'
t "every archived version is valid yaml, older than current" '
import yaml, pathlib
cur=int(yaml.safe_load(open("state/strategy.yaml"))["version"])
for p in pathlib.Path("state/history").glob("v*.yaml"):
    v=yaml.safe_load(open(p))
    assert int(v["version"]) < cur, (p.name, v["version"], cur)
print("ok")'
t "hypotheses ledger is well-formed jsonl" '
import json
rows=[json.loads(l) for l in open("state/hypotheses.jsonl") if l.strip()]
for r in rows:
    assert {"from_version","to_version","variable"} <= set(r), r
print("ok")'

echo "== take-profit =="
t "gain over target closes"        "$H
r=evaluate_exit(md(101.5), S, pos()); assert r['action']=='close' and r['exit_reason']=='take_profit', r
print('ok')"
t "gain under target holds"        "$H
assert evaluate_exit(md(101.0), S, pos())['action']=='hold'
print('ok')"
t "boundary: exactly +1.5% closes" "$H
assert evaluate_exit(md(101.5), S, pos())['action']=='close'
print('ok')"
t "a loss never take-profits"      "$H
assert evaluate_exit(md(98.0), S, pos())['action']=='hold'
print('ok')"

echo "== rsi mean-reversion exit =="
t "rsi >= 70 closes"               "$H
assert evaluate_exit(md(100.2, rsi=71.0), S, pos())['exit_reason']=='rsi_exit'
print('ok')"
t "rsi 69 holds (boundary)"        "$H
assert evaluate_exit(md(100.2, rsi=69.0), S, pos())['action']=='hold'
print('ok')"
t "take-profit precedes rsi when both fire" "$H
assert evaluate_exit(md(102.0, rsi=75.0), S, pos())['exit_reason']=='take_profit'
print('ok')"

echo "== tunable by reflection, not hardcoded =="
t "raising take_profit_pct suppresses the exit" "$H
S2={'exit':{'take_profit_pct':5.0,'rsi_exit':70}}
assert evaluate_exit(md(101.5), S2, pos())['action']=='hold'
assert evaluate_exit(md(105.0), S2, pos())['action']=='close'
print('ok')"
t "lowering rsi_exit triggers earlier" "$H
S3={'exit':{'take_profit_pct':99.0,'rsi_exit':55}}
assert evaluate_exit(md(100.1, rsi=56.0), S3, pos())['exit_reason']=='rsi_exit'
print('ok')"
t "null disables a condition"      "$H
S4={'exit':{'take_profit_pct':None,'rsi_exit':None}}
assert evaluate_exit(md(200.0, rsi=99.0), S4, pos())['action']=='hold'
print('ok')"
t "missing exit block is safe (old configs)" "$H
assert evaluate_exit(md(200.0, rsi=99.0), {}, pos())['action']=='hold'
print('ok')"
t "absent price data holds"        "$H
assert evaluate_exit({'data':{}}, S, pos())['action']=='hold'
print('ok')"
t "short take-profit inverts"      "$H
assert evaluate_exit(md(98.0), S, pos(d='short'))['exit_reason']=='take_profit'
assert evaluate_exit(md(102.0), S, pos(d='short'))['action']=='hold'
print('ok')"
t "rsi exit stays long-only"       "$H
S5={'exit':{'take_profit_pct':None,'rsi_exit':70}}
assert evaluate_exit(md(101.0, rsi=90.0), S5, pos(d='short'))['action']=='hold'
print('ok')"

echo "== regression: the deadlock the exit rule fixes =="
t "long entry rule never emits sell (root cause)" '
from hermes_trading.loop import evaluate_strategy
S={"entry":{"indicator":"rsi","threshold":30,"direction":"long"}}
acts={evaluate_strategy({"data":{"rsi":r}}, S)["action"] for r in (5,25,30,50,75,95)}
assert acts=={"buy","hold"}, acts
print("ok")'

echo "== storage: outcomes durable, config from image =="
t "HERMES_DATA_DIR redirects trades, not strategy" "
import os; os.environ['HERMES_DATA_DIR']=r'$WTMP\\d1'
from hermes_trading import loop
assert 'd1' in str(loop.TRADES_PATH) and 'd1' in str(loop.HEARTBEAT_PATH)
assert 'd1' not in str(loop.STRATEGY_PATH) and 'd1' not in str(loop.GOAL_PATH)
assert loop.DATA_DIR.is_dir()
print('ok')"
t "unset falls back to state/" '
import os; os.environ.pop("HERMES_DATA_DIR", None)
from hermes_trading import loop
assert loop.TRADES_PATH.parent == loop.STATE_DIR
print("ok")'
t "log_trade appends; marker == jsonl row" "
import os, json; os.environ['HERMES_DATA_DIR']=r'$WTMP\\d2'
import io, contextlib
from hermes_trading import loop
buf=io.StringIO()
with contextlib.redirect_stdout(buf):
    for i in range(3): loop.log_trade({'i':i,'exit_reason':'take_profit'})
rows=[l for l in open(loop.TRADES_PATH) if l.strip()]
marks=[l[len('TRADE_CLOSED '):] for l in buf.getvalue().splitlines() if l.startswith('TRADE_CLOSED ')]
assert len(rows)==3 and len(marks)==3, (len(rows), len(marks))
assert [m.strip() for m in marks]==[r.strip() for r in rows]
assert json.loads(marks[0])['exit_reason']=='take_profit'
print('ok')"

echo "== Dockerfile: deploy-critical env =="
t "unbuffered stdout + data dir matching the volume" '
d=open("Dockerfile").read()
assert "ENV PYTHONUNBUFFERED=1" in d
assert "ENV HERMES_DATA_DIR=/app/data" in d
print("ok")'

echo "== end-to-end: run_loop records a WINNING trade =="
rm -rf "$BTMP/e2e"
out=$(HERMES_DATA_DIR="$WTMP\\e2e" "$PY" -c '
import asyncio, json
from unittest.mock import patch
from hermes_trading import loop
ticks=[{"schema_version":"1.0","data":{"close":100.0,"rsi":25.0}},
       {"schema_version":"1.0","data":{"close":102.0,"rsi":60.0}}]
seq=iter(ticks); other={"schema_version":"1.0","data":{}}
async def price(*a,**k):
    try: return next(seq)
    except StopIteration: raise KeyboardInterrupt
async def oth(*a,**k): return other
async def nosleep(*a,**k): return None
with patch.object(loop.price_adapter,"fetch",price), \
     patch.object(loop.onchain_adapter,"fetch",oth), \
     patch.object(loop.news_adapter,"fetch",oth), \
     patch.object(loop.macro_adapter,"fetch",oth), \
     patch.object(loop.asyncio,"sleep",nosleep):
    try: asyncio.run(loop.run_loop("BTC/USDT"))
    except KeyboardInterrupt: pass
print("ROWS "+json.dumps([json.loads(l) for l in open(loop.TRADES_PATH) if l.strip()]))' 2>&1)
row=$(printf '%s\n' "$out" | sed -n 's/^ROWS //p')
res=$(printf '%s' "$row" | "$PY" -c '
import json,sys, yaml
r=json.load(sys.stdin); assert len(r)==1, r
t=r[0]
assert t["exit_reason"]=="take_profit", t
assert t["pnl_pct"]>0, t
assert t["strategy_version"]==yaml.safe_load(open("state/strategy.yaml"))["version"], t
print("ok")' 2>&1)
[ "$res" = ok ] && ok "entry -> take_profit close, positive pnl, tagged current version" \
                || no "loop wiring" "$res" "$out"
case "$out" in *"TRADE_CLOSED "*) ok "closed trade emits TRADE_CLOSED marker";; *) no "marker missing";; esac

echo "== watcher: reflection cadence =="
if [ -f "$WATCH" ]; then
  SB="$BTMP/bin"; mkdir -p "$SB"
  mk(){ { printf '#!/usr/bin/env bash\n[ "$1" = logs ] || exit 0\ncat <<'"'"'EOF'"'"'\n'
          printf '%s\n' "$1"; printf 'EOF\n'; } > "$SB/railway"; chmod +x "$SB/railway"; }
  run(){ PATH="$SB:$PATH" bash "$WATCH" 2>/dev/null; }
  feed(){ local o="[t] Iteration 9: hold" i
          for ((i=1;i<=$1;i++)); do o+=$'\n'"TRADE_CLOSED {\"i\":$i}"; done; printf '%s' "$o"; }
  orig=$(cat "$MARK"); printf '0\n' > "$MARK"

  mk "$(printf '$VIX: possibly delisted\n[t] Iteration 1: hold')"; r=$(run)
  case "$r" in *"closed_trades_in_log_window: 0"*) ok "adapter noise is not a trade";; *) no "noise leak" "$r";; esac
  case "$r" in *WORKER_WARNING*) no "false liveness alarm" "$r";; *) ok "liveness via Iteration lines";; esac
  for n in 4:NO 5:YES 6:YES; do
    mk "$(feed "${n%:*}")"; r=$(run)
    case "$r" in *"reflection_due: ${n#*:}"*) ok "${n%:*} trades -> ${n#*:}";; *) no "gate at ${n%:*}" "$r";; esac
  done
  printf '4\n' > "$MARK"; r=$(run)
  case "$r" in *"since_last_reflection: 2"*) ok "high-water mark: 6-4=2 new";; *) no "delta math" "$r";; esac
  case "$r" in *"reflection_due: NO"*) ok "counted trades never re-trigger";; *) no "re-triggered" "$r";; esac
  printf '%s' "$orig" > "$MARK"
  mk "$(printf 'Starting Container\nStopping Container')"; r=$(run)
  case "$r" in *WORKER_WARNING*) ok "silent worker flagged";; *) no "missed dead worker" "$r";; esac
else
  echo "  SKIP watcher not at $WATCH"
fi

rm -rf "$BTMP"
echo; echo "RESULT: $pass passed, $fail failed"; [ "$fail" -eq 0 ]
