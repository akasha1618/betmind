# BetMind — Versiunea 1

Document de prezentare pentru stakeholderi. Descrie ce face aplicația azi, nu ce ar putea face.

---

## 1. Ce este aplicația

BetMind este un asistent de chat pentru recomandări de bilete la fotbal. Nu e o casă de pariuri și nu plasează pariuri. Utilizatorul scrie ce vrea — de exemplu „un bilet cotă 5 din meciurile de azi, risc mediu” — și primește un bilet argumentat, cu cote reale și cu șansa estimată a întregului bilet spusă pe față.

Interacțiunea e o conversație, pe telefon sau pe calculator, în română sau engleză. În timp ce lucrează, asistentul spune ce face: ce meciuri a găsit, pe care le analizează, când pregătește biletul. La final apare un tabel (meci, oră în România, pariu, cotă, nivel de încredere), cota totală, de ce a ales fiecare selecție, ce a evitat și ce merită verificat singur înainte de pariu.

Poate continua discuția: de ce o alegere, cine lipsește, forma recentă, un bilet mai scurt sau fără un meci. Răspunsurile despre meciuri vin din date actuale, nu din „ce își amintește” asistentul.

---

## 2. Ce poate face

### Generarea biletelor
- **Cerere în limbaj obișnuit.** Cotă țintă, risc (sigur / mediu / riscant), ligi, număr de selecții, miză opțională — fără formulare.
- **Bilet calculat, nu ghicit.** Sistemul combină selecțiile ca să atingă cota cerută cu cea mai bună șansă de ansamblu, maximum un pariu per meci, cu piețe diferite (rezultat, goluri, ambele marchează, șansă dublă, handicap, pauză).
- **Onestitate la bilete mai lungi.** Dacă utilizatorul vrea mai multe meciuri decât e nevoie pentru cotă, i se spune clar cum scade probabilitatea.
- **Mod Avansat, la alegere.** Analiză mai profundă, meci cu meci, în paralel. Implicit e modul standard, mai rapid; Avansat se pornește din interfață.

### Conversația și întrebările ulterioare
- **Explicație, nu rescriere.** „De ce Juve?” lasă biletul neschimbat. Biletul se reface doar la o cerere clară („scoate X”, „înlocuiește Y”).
- **Întrebări libere despre fotbal.** Formă, accidentări, istoric direct, clasament, cote, amânări — din date, nu din memorie.
- **Oprire și editare.** Se poate opri un răspuns în curs sau rescrie un mesaj deja trimis; discuția continuă de acolo.
- **Date proaspete la reluare.** Dacă o conversație e redeschisă după câteva ore, programul și cotele se reverifică înainte de afirmații despre meciuri viitoare.

### Transparența recomandărilor
- **Motive cu cifre.** Fiecare argument citează un dat concret (scor, medie, jucător, loc în clasament). Formulările goale de tipul „echipă de calitate” sunt evitate.
- **Șansa reală a biletului.** Un bilet cotă 30 nu e prezentat ca „sigur”.
- **Ce lipsește din date.** Dacă lipsesc statistici sau cote, o spune și sugerează ce să verifice utilizatorul (de exemplu primul 11).
- **Progres vizibil.** Nu tace în timpul lucrului: se vede ce analizează și când scrie recomandarea.

### Istoricul și feedback-ul
- **Conversațiile rămân.** Lista din meniu, titlu automat, conversație nouă, ștergere. La repornirea aplicației, discuțiile nu se pierd.
- **Biletele primite se țin minte.** „Ce mi-ai dat ieri?” e răspuns din recomandări salvate, nu inventat.
- **Apreciere.** Sub bilet: bun / slab și comentariu. În Versiunea 1 se doar strâng; nu schimbă încă recomandările.

### Siguranța și jocul responsabil
- **Doar 18+.** Confirmare la primul acces; fără recomandări dacă utilizatorul e minor.
- **Mesaj legal pe fiecare bilet.** 18+, fără garanție de câștig, trimitere la jocresponsabil.ro.
- **Fără îndemn la recuperarea pierderilor** sau la mize pe care utilizatorul nu și le permite.
- **Acces controlat pentru testare.** O parolă comună poate închide aplicația față de public; nu înlocuiește conturile individuale.

---

## 3. Cum funcționează, pe scurt

Datele nu vin din „ce știe” inteligența artificială. Vin de la un furnizor specializat de fotbal: program, rezultate, clasamente, accidentări, istoric între echipe, cote de la case. Aplicația ține la zi un program local al ligilor urmărite (trecut recent, azi, următoarele două săptămâni) și îl reîmprospătează singură. Orele sunt cele din România.

Când cineva cere un bilet, întâi se ia programul. Apoi se alege un set rezonabil de meciuri viitoare — nu tot ce e în lume. Pentru fiecare, se adună un dosar de fapte: formă, goluri, absențe, clasament, cote, zile de pauză, un eventual meci european în timpul săptămânii. Dosarul e pregătit din date, nu „din cap”. Dacă o informație lipsește, e marcată ca lipsă, nu umplută.

În modul Avansat, mai mulți analiști lucrează în același timp, câte un meci fiecare — ca o echipă de scouteri, nu ca o singură persoană care citește tot la rând. Fiecare întoarce probabilități, variante de pariu cu cote reale, factori concreți și un unghi (de exemplu oboseala după Europa). Dacă analiza unui meci eșuează, meciul e sărit, nu inventat. O analiză recentă se refolosește la întrebări ulterioare, fără a o lua de la capăt.

Biletul nu e scris „din burtă”. Un calcul alege combinația care atinge cota țintă cu cea mai bună probabilitate de ansamblu, evită două pariuri pe același meci și amestecă piețele și ligile când se poate. Textul către utilizator doar explică acea alegere, cu cifrele din dosar.

Analogia scurtă: inteligența artificială e analistul care citește dosarul și vorbește cu omul; dosarul și calculele de bilet sunt contabilitatea. Nu se inversează rolurile.

---

## 4. Ce face aplicația diferit

- **Analiză per meci, cu dosar complet.** Nu e un pont scos dintr-o propoziție generică. Fiecare meci din shortlist e cercetat cu aceleași tipuri de date (formă, absențe, cote, clasament). Dacă dosarul e incomplet, se spune, nu se maschează.
- **Explicații verificabile.** „4 victorii din ultimele 5 acasă, 11–3 golaveraj” — nu „formă bună”. Utilizatorul poate verifica.
- **Probabilități spuse onest.** Șansa biletului e produsul șanselor selecțiilor, fără înfrumusețare. Cotele mari rămân șanse mici, spuse ca atare.
- **Biletul e un calcul, nu o inspirație.** Același set de candidați dă același bilet. Se optimizează șansa întregului bilet, nu „cel mai frumos” pariu izolat.
- **Nu inventează date.** Meciuri, cote, accidentări, rezultate — doar ce a venit de la furnizor sau din programul ținut la zi. Lipsa cotelor (casele nu au deschis încă linia) e tratată ca situație normală, nu ca defect.

---

## 5. Limitări actuale

Informație pentru decizii, nu listă de scuze.

- **Acoperirea competițiilor.** Implicit: marile ligi din Anglia, Spania, Germania, Italia, Franța, Olanda, Portugalia, Liga I, cupele europene și Campionatul Mondial. Alte competiții se pot adăuga la cerere în conversație (cupă națională, ligă mai mică). Nu e acoperit tot fotbalul din start; ce nu e urmărit nu are program ținut la zi.
- **Cotele depind de case.** Dacă furnizorul nu are încă linii pentru un meci (de obicei se deschid cu 2–3 zile înainte), acel meci nu poate intra pe bilet cu cotă. Nu e o eroare a aplicației.
- **Început de sezon.** Formă, statistici și accidentări sunt subțiri. Aplicația coboară încrederea și o spune; nu completează golurile cu presupuneri.
- **Timpul unui bilet.** Un bilet gata durează mai mult decât un răspuns de chatbot obișnuit, pentru că se cercetează date reale. Modul Avansat e și mai lung (zeci de secunde, uneori peste un minut), cu pași vizibili. Nu e instant.
- **Fără conturi individuale.** Identitatea e per browser. Ștergerea datelor din browser rupe legătura cu istoricul. Parola de acces, dacă e folosită, e comună testerilor — nu e un sistem de membri.
- **Nu verifică automat dacă biletul a ieșit.** Recomandările se salvează; rezultatul (câștig / pierdere) nu e urmărit singur. Nu există, azi, un scor de performanță al ponturilor.
- **Utilizare simultană limitată.** Construită pentru un grup mic de testeri, nu pentru trafic mare. Furnizorul de date are plafon pe minut și pe zi; prea multe cereri odată înseamnă așteptare sau date mai vechi, spuse onest.
- **Nu plasează pariuri** și nu e conectată la un cont de casă.
- **Feedback-ul nu personalizează încă** recomandările.
- **Abonamentele (gratuit / premium) nu sunt active.** Comutatorul Avansat există; plata și plafoanele pe număr de întrebări, nu.

---

## 6. Ce urmează în Versiunea 2

Direcții deja planificate, neimplementate:

- Conturi individuale, cu istoric care nu depinde de un singur browser
- Personalizare pe baza feedback-ului (ce tip de bilete preferă omul)
- Plan gratuit / premium, cu limite clare de utilizare
- Canal WhatsApp, pe lângă chat-ul web
- Analize la scară mai mare (mai multe meciuri, mai rapid)
- Indicatori avansați de performanță ofensivă/defensivă (gen goluri așteptate), printr-o sursă de date mai bogată
- Urmărirea automată a rezultatelor biletelor recomandate

---

*Versiunea 1, august 2026. Recomandările nu garantează câștiguri. 18+ | Pariază responsabil.*
