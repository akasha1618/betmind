#!/bin/sh
# Ruleaza ca root DOAR ca sa pregateasca volumul, apoi coboara la utilizatorul
# betmind si porneste aplicatia.
#
# De ce e nevoie: chown-ul din Dockerfile se aplica la BUILD, dar Railway
# monteaza volumul persistent peste /data la RUNTIME. Mount-ul apartine lui
# root, deci fara pasul asta procesul non-root nu poate scrie betmind.db
# (sqlite3.OperationalError: unable to open database file).
set -e

DATA_DIR="${DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    chown -R betmind:betmind "$DATA_DIR" || \
        echo "[entrypoint] Atentie: chown pe $DATA_DIR a esuat; verifica montarea volumului."
    echo "[entrypoint] $DATA_DIR pregatit pentru betmind; pornesc aplicatia."
    exec gosu betmind "$@"
fi

# Containerul ruleaza deja non-root (alt host): nu putem face chown, mergem mai departe.
echo "[entrypoint] Rulez ca UID $(id -u); sar peste chown pe $DATA_DIR."
exec "$@"
