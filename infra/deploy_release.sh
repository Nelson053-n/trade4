#!/bin/bash
# Выкладка кода trade4 на прод БЕЗ git (прод 91.212.150.163 = `ssh amn-ru3`, /opt/trade4 без .git).
#
# Запуск на vm-claude из корня репозитория:
#   infra/deploy_release.sh check [commit]  — только чтение: sha256 файлов прода против коммита
#   infra/deploy_release.sh apply [commit]  — выложить изменённые файлы коммита, БЕЗ рестарта
#
# Источник истины — git: apply разрешён только для коммита, уже лежащего в origin/main.
# В архив попадают только файлы из git, поэтому рантайм не затрагивается никогда:
# session_state_*, recovery_metadata.json, sandbox_server.py (корень), app/st4/.tbank*, out/, .venv.
# Перед заменой сохраняется копия старых версий файлов и session_state в /root/trade4-deploy-backup-*.
# Рестарт сервиса — ОТДЕЛЬНО и только после флэт-гейта (скилл /trade-deploy). До рестарта процесс
# работает на старом коде (но Restart=on-failure после падения поднимет уже новый код).
set -euo pipefail

MODE=${1:-check}
COMMIT=$(git rev-parse --verify "${2:-HEAD}^{commit}")
SHORT=${COMMIT:0:7}
HOST=${TRADE4_PROD_HOST:-amn-ru3}
DST=/opt/trade4
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 "$HOST")

case "$MODE" in check|apply) ;; *) echo "режим: check|apply" >&2; exit 2;; esac

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir "$TMP/tree"
git archive "$COMMIT" | tar -x -C "$TMP/tree"
git ls-tree -r --name-only -z "$COMMIT" > "$TMP/files0"
(cd "$TMP/tree" && xargs -0 sha256sum -- < "$TMP/files0") | sort -k2 > "$TMP/local.sha"

compare() {   # → $TMP/changed0 (NUL-список изменённых+новых), печать сводки
  "${SSH[@]}" "cd $DST && xargs -0 sha256sum -- 2>/dev/null" < "$TMP/files0" | sort -k2 > "$TMP/remote.sha" || true
  python3 - "$TMP" <<'PY'
import sys, pathlib
tmp = pathlib.Path(sys.argv[1])
def load(p):
    out = {}
    for line in (tmp / p).read_text().splitlines():
        h, _, name = line.partition('  ')
        out[name] = h
    return out
loc, rem = load('local.sha'), load('remote.sha')
changed = sorted(n for n in loc if n in rem and rem[n] != loc[n])
missing = sorted(n for n in loc if n not in rem)
(tmp / 'changed0').write_bytes(b''.join(n.encode() + b'\0' for n in changed + missing))
print(f'совпадают: {len(loc) - len(changed) - len(missing)} из {len(loc)}')
for n in changed: print('  ИЗМЕНЁН ', n)
for n in missing: print('  НОВЫЙ   ', n)
PY
}

echo "== коммит $SHORT ($(git log -1 --format='%ad %s' --date=short "$COMMIT")) → $HOST:$DST"
compare
echo "== на проде есть код вне коммита (не трогается, к сведению):"
"${SSH[@]}" "cd $DST && find app tests infra -type f ! -path '*/__pycache__/*' ! -name '.tbank*' ! -name '*.pyc' 2>/dev/null | sort" \
  </dev/null > "$TMP/remote.list" || true
tr '\0' '\n' < "$TMP/files0" | sort > "$TMP/local.list"
comm -23 "$TMP/remote.list" "$TMP/local.list" | sed 's/^/  ЛИШНИЙ  /'
remote_release=$("${SSH[@]}" "cat $DST/RELEASE 2>/dev/null || echo 'нет файла RELEASE'" </dev/null)
echo "== RELEASE на проде: $remote_release"

[ "$MODE" = check ] && exit 0

# ---------- apply ----------
git fetch -q origin
git merge-base --is-ancestor "$COMMIT" origin/main \
  || { echo "СТОП: $SHORT нет в origin/main — сначала push" >&2; exit 3; }
if [ ! -s "$TMP/changed0" ]; then echo "== изменений нет, выкладывать нечего"; exit 0; fi

TS=$(date +%Y%m%d-%H%M%S)
PKG="trade4-release-$SHORT-$TS.tgz"
BK="/root/trade4-deploy-backup-$TS"
tar -C "$TMP/tree" --null -T "$TMP/changed0" -czf "$TMP/$PKG"
SUM=$(sha256sum "$TMP/$PKG" | cut -d' ' -f1)
scp -q -o BatchMode=yes "$TMP/$PKG" "$HOST:/root/$PKG"
"${SSH[@]}" "echo '$SUM  /root/$PKG' | sha256sum -c --quiet" </dev/null
echo "== архив на проде, sha256 сверен"

# бэкап старых версий заменяемых файлов + рантайм-состояния (копия, оригиналы не трогаются)
"${SSH[@]}" "set -e; mkdir -p $BK; cd $DST; tar --null -T - --ignore-failed-read -czf $BK/code-before.tgz 2>/dev/null || true; \
  cp -a session_state_*.json recovery_metadata.json $BK/ 2>/dev/null || true; cp -a RELEASE $BK/ 2>/dev/null || true; ls $BK" \
  < "$TMP/changed0"
echo "== бэкап: $HOST:$BK"

"${SSH[@]}" "tar -xzf /root/$PKG -C $DST --no-same-owner --no-overwrite-dir && echo '$COMMIT $(date -Is)' > $DST/RELEASE && rm -f /root/$PKG" </dev/null
echo "== файлы выложены; повторная сверка:"
compare
if [ -s "$TMP/changed0" ]; then echo "ОШИБКА: после выкладки остались расхождения" >&2; exit 4; fi
cat <<EOF
== ГОТОВО: $SHORT на диске прода. Сервис НЕ перезапускался — процесс работает на старом коде.
   Рестарт — только после флэт-гейта: скилл /trade-deploy, шаг 4–5.
   Откат файлов: ssh $HOST 'tar -xzf $BK/code-before.tgz -C $DST'  (новые файлы из списка выше удалить вручную)
EOF
