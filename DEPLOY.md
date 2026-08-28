# BetMind — deploy pe Railway (testare privată)

Ghid pas cu pas pentru a pune aplicația pe un URL public, cu disc persistent
pentru SQLite și protejată de o parolă comună de acces. Fără migrare de bază de
date, fără conturi de utilizator: fiecare browser își păstrează istoricul
propriu (`user_key` din localStorage) în spatele parolei comune.

## 1. Pregătirea repo-ului

1. Codul trebuie să fie într-un repo GitHub. Verifică înainte de push că
   `.gitignore` exclude secretele și datele locale (sunt deja acoperite):
   - `.env` (cheile API!)
   - `data/` și `*.db` (baza SQLite locală)
   - `venv/`, `__pycache__/`, `.pytest_cache/`
2. `Dockerfile`, `.dockerignore` și `railway.json` sunt deja în repo — Railway
   le detectează automat și construiește imaginea din Dockerfile.

## 2. Crearea proiectului Railway

1. Intră pe [railway.app](https://railway.app) și autentifică-te (ideal cu GitHub).
2. **New Project → Deploy from GitHub repo** → alege repo-ul BetMind.
3. Railway pornește primul build automat. Va eșua sănătos până setăm
   variabilele de mediu și volumul — e normal, continuă cu pașii de mai jos.

## 3. Atașarea volumului persistent la `/data`

SQLite trebuie să stea pe un disc care supraviețuiește redeploy-urilor.

1. În proiect, click dreapta pe serviciu (sau butonul **+ Create**) → **Volume**.
2. Atașează volumul la serviciul BetMind cu **mount path**: `/data`.
3. Dimensiunea implicită (0.5–1 GB) e mai mult decât suficientă.

> Notă: volumele nu se pot declara în `railway.json` — se atașează o singură
> dată din dashboard, apoi rămân legate de serviciu.

## 4. Variabilele de mediu

În serviciu → tab **Variables** → adaugă:

| Variabilă | Valoare / explicație |
|---|---|
| `ANTHROPIC_API_KEY` | cheia Claude, de la console.anthropic.com |
| `API_FOOTBALL_KEY` | cheia API-Football, de la dashboard.api-football.com |
| `CLAUDE_MODEL` | modelul orchestratorului (ex. `claude-sonnet-4-5`) |
| `ANALYST_MODEL` | modelul analiștilor de meci (ex. `claude-3-5-haiku-latest`) |
| `APP_TIMEZONE` | `Europe/Bucharest` |
| `MAX_TOKENS` | lungimea maximă a unui răspuns al coordonatorului (`128000` = plafonul Sonnet 4.6) |
| `MAX_DAILY_API_REQUESTS` | bugetul zilnic API-Football (ex. `95`) |
| `MAX_PARALLEL_ANALYSTS` | câți analiști rulează în paralel (ex. `3`) |
| `ORCHESTRATION_MODE` | `standard` sau `advanced` |
| `ACCESS_PASSWORD` | parola comună pe care o dai testerilor |
| `SESSION_SECRET` | șir aleator lung (ex. `openssl rand -hex 32`) — semnează cookie-ul de sesiune |
| `DATA_DIR` | `/data` — baza SQLite ajunge pe volumul persistent |
| `TRUST_PROXY_HEADERS` | `true` — Railway e un proxy, IP-ul real vine din `X-Forwarded-For` |

Important:

- **Fără `ACCESS_PASSWORD`** aplicația rulează deschisă — pe un URL public
  setează-l obligatoriu. Aplicația nu crapă dacă lipsește, doar loghează un
  avertisment la pornire.
- **Fără `SESSION_SECRET`** login-ul merge, dar sesiunile pică la fiecare
  restart (secret temporar per proces) — setează-l.
- Nu urca `.env`-ul local; toate valorile se setează doar în Railway.

După salvarea variabilelor, Railway redeployează automat.

## 5. URL public

Serviciu → **Settings → Networking → Generate Domain**. Primești un URL de tip
`https://betmind-production.up.railway.app`.

## 6. Verificarea deploy-ului

Deschide `https://<domeniul-tau>/api/health` (nu cere parolă) și verifică:

- `"env": "production"` — variabilele de mediu au fost citite;
- `"db_path": "/data/betmind.db"` — baza stă pe volum;
- `"disk_writable": true` — **volumul e montat corect și se poate scrie pe el**
  (dacă e `false`, verifică mount path-ul `/data` la pasul 3);
- `"access_gate_enabled": true` — parola de acces e activă;
- `"anthropic_key_set": true` și `"api_football_key_set": true`.

Apoi testul de acces:

1. Deschide `https://<domeniul-tau>/` → trebuie să te redirecționeze la `/login`.
2. Introdu parola → intri în chat și totul funcționează normal.
3. Parolă greșită de >10 ori în 15 minute → mesaj de limitare (rate-limit).

## 7. Depanare: `unable to open database file`

Dacă în loguri apare `sqlite3.OperationalError: unable to open database file`
deși volumul e montat (`Mounting volume on: /data` în log), cauza e permisiunea
pe punctul de montare: volumul se montează la **runtime**, peste directorul
pregătit la build, și aparține lui `root` — iar aplicația rulează non-root.

Rezolvarea e deja în repo: `docker-entrypoint.sh` pornește ca root, face
`mkdir -p` și `chown` pe `$DATA_DIR`, apoi coboară la utilizatorul `betmind`
(prin `gosu`) și lansează uvicorn. Dacă tot apare eroarea, verifică în loguri
linia de diagnostic scrisă de aplicație — conține calea bazei, UID-ul
procesului și permisiunile directorului:

```
Nu pot deschide baza de date. db_path=/data/betmind.db dir=/data uid=1000 gid=1000 dir_mode=drwxr-xr-x dir_owner=0:0 dir_exists=True dir_writable=False
```

- `dir_owner=0:0` cu `uid` diferit de 0 → chown-ul din entrypoint nu a rulat
  (verifică `ENTRYPOINT` în Dockerfile și că scriptul are permisiune de execuție).
- `dir_exists=False` → volumul nu e montat pe `/data` (revezi pasul 3).
- `DATA_DIR` setat altfel decât `/data` → entrypoint-ul face chown pe altă cale
  decât cea montată; ține-le identice.

## 8. Loguri și depanare

- Serviciu → tab **Deployments** → click pe deploy-ul activ → **View Logs**.
- La pornire vezi raportul BetMind (chei setate, model, buget) și eventualele
  avertismente `ACCESS_PASSWORD`/`SESSION_SECRET`.
- Sincronizarea de fundal loghează fiecare ciclu cu activitate
  (`Ciclu sync: X/Y zile sincronizate...`); o eroare într-un ciclu nu omoară
  task-ul — reîncearcă la următorul tick.
- `/api/health` rămâne cel mai rapid instrument: buget API folosit azi, ultima
  sincronizare, numărul de fixtures din DB.

## 9. Ce primesc testerii

- URL-ul public + parola comună (`ACCESS_PASSWORD`).
- Fiecare browser primește automat propriul istoric de conversații și bilete
  (identificatorul anonim din localStorage) — nu e nevoie de conturi.
