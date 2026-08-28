import os, json, sqlite3, httpx
from dotenv import load_dotenv

load_dotenv()
ECHIPA = "Rapid Vien"   # schimbă cu orice echipă din meciul care te interesează

con = sqlite3.connect("data/betmind.db")
row = con.execute(
    "SELECT fixture_id, home_name, away_name, date_local, time_local FROM fixtures "
    "WHERE home_name LIKE ? OR away_name LIKE ? ORDER BY date_local DESC LIMIT 1",
    (f"%{ECHIPA}%", f"%{ECHIPA}%"),
).fetchone()
con.close()

if not row:
    raise SystemExit(f"Nu am gasit meci pentru '{ECHIPA}' in baza locala.")

print(f"MECI: {row[1]} - {row[2]}  ({row[3]} {row[4]})  id={row[0]}")

r = httpx.get(
    "https://v3.football.api-sports.io/odds",
    params={"fixture": row[0]},
    headers={"x-apisports-key": os.environ["API_FOOTBALL_KEY"]},
    timeout=25,
).json()

resp = r.get("response") or []
if not resp:
    raise SystemExit("API-ul nu are cote pentru acest meci.")

print("ACTUALIZAT LA:", resp[0].get("update"))

for bk in resp[0].get("bookmakers", []):
    if bk.get("id") != 34:      # 34 = Superbet
        continue
    print("BOOKMAKER:", bk.get("name"))
    for bet in bk.get("bets", []):
        if "Both" in bet["name"] or "BTTS" in bet["name"]:
            print(json.dumps(bet, ensure_ascii=False, indent=1))