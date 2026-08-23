# ⚽ BetMind — recomandări AI pentru bilete de fotbal

Chatbot web care cercetează date reale despre meciuri (statistici, formă, accidentări, H2H, clasamente, cote) și construiește bilete cu reasoning transparent. Un singur agent Claude cu 9 tool-uri, conectat la API-Football.

---

## 1. Cerințe

- **Python 3.10+** (verifică cu `python3 --version`)
- Două chei API gratuite (vezi pasul 3)

## 2. Instalare

```bash
cd betmind
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Cheile API — unde le obții și unde le pui

Copiază fișierul de configurare și deschide-l într-un editor:

```bash
cp .env.example .env
```

Completează în **`.env`** cele două chei:

| Cheie | De unde o iei | Cost |
|---|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) → „Create Key". Ai nevoie de un cont Anthropic cu credit (minim $5). | ~$0.01–0.05 per bilet generat cu Sonnet |
| `API_FOOTBALL_KEY` | [dashboard.api-football.com](https://dashboard.api-football.com) → cont gratuit → pagina **Account** → câmpul „API Key" | Gratuit: 100 requesturi/zi. Pro: ~$19/lună |

> ⚠️ Folosește dashboard-ul **direct** api-football.com, NU varianta prin RapidAPI — aceea are alt header de autentificare și nu va funcționa cu acest cod.

`.env` final arată așa:

```
ANTHROPIC_API_KEY=sk-ant-api03-....
API_FOOTBALL_KEY=a1b2c3d4e5....
```

## 4. Pornire

```bash
python main.py
```

Deschide **http://localhost:8000** în browser (funcționează și de pe telefon dacă ești în aceeași rețea: `http://IP-ul-laptopului:8000`).

## 5. Cum testezi

1. **Verifică sănătatea:** http://localhost:8000/api/health — ambele chei trebuie să apară `true`.
2. **Test rapid (1–2 requesturi API):** scrie în chat
   `Ce meciuri sunt azi în ligile de top?`
3. **Test complet de bilet:**
   `Recomandă-mi un bilet cu cota 5 din meciurile de mâine, risc mediu.`
   Vei vedea pașii agentului („Caut meciurile…", „Preiau cotele…") și apoi biletul cu tabelul de selecții + reasoning.
4. **Test follow-up:** după bilet, întreabă
   `De ce ai ales over 2.5 la primul meci?` sau `Câți accidentați are echipa gazdă?`

**Notă free tier:** un bilet complet consumă ~10–25 requesturi API-Football (cu cache inclus). Cu 100/zi gratuite ai loc de ~4–6 bilete pe zi. Cache-ul intern (15 min–24 h în funcție de tipul datelor) reduce mult consumul la întrebări repetate.

## 6. Structura proiectului

```
betmind/
├── main.py            # server FastAPI + SSE + sesiuni
├── agent.py           # bucla agentului Claude + definițiile tool-urilor
├── football_data.py   # adapter API-Football + cache (schimbi furnizorul doar aici)
├── ticket_builder.py  # algoritmul determinist de construire a biletului
├── prompts.py         # system prompt-ul agentului
├── static/index.html  # UI-ul de chat (verde/negru, responsive)
├── requirements.txt
└── .env.example       # șablonul de configurare
```

## 7. Configurări opționale (în `.env`)

| Variabilă | Implicit | Rol |
|---|---|---|
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Modelul folosit. `claude-haiku-4-5-20251001` = mai ieftin, analize mai simple. |
| `MAX_TOKENS` | `4096` | Lungimea maximă a unui răspuns. |
| `PORT` | `8000` | Portul serverului. |

## 8. Probleme frecvente

- **„Lipsește ANTHROPIC_API_KEY"** în chat → nu ai completat `.env` sau nu ai repornit serverul după completare.
- **„API-Football a returnat erori"** → cheie greșită, sau ai depășit cele 100 req/zi (se resetează la miezul nopții UTC). Verifică `api_football_requests_remaining_today` în `/api/health`.
- **Agentul nu găsește meciuri** → în pauzele competiționale (vară, ferestre internaționale) ligile implicite pot să nu aibă meciuri; cere explicit o competiție activă (ex. World Cup) sau altă perioadă.
- **401 la Claude API** → cheia e invalidă sau contul nu are credit.

## 9. Ce urmează (V2 — deja pregătit în arhitectură)

Conturi + istoric bilete în Postgres, feedback 👍/👎 și personalizare, free/premium pe număr de mesaje, integrare WhatsApp, multi-agent pentru analize masive, xG (migrare pe Sportmonks prin `football_data.py`).

---

**18+ | Recomandările nu garantează câștiguri. Pariază responsabil. | jocresponsabil.ro**

Înainte de lansare publică în România, verificați cu un avocat încadrarea față de reglementările ONJN privind jocurile de noroc și publicitatea aferentă.
