# JARVIS — lokalni Windows AI asistent — handoff za nastavak rada

> Presjek: 12. oktobar 2026, grana `main` na `5dc51e64` prije ove dokumentacione izmjene. Opis koda je provjeren u repozitorijumu; stanje hostinga koje nije izričito potvrđeno označeno je kao neprovjereno. Ovaj dokument nije zamjena za aktuelni kod, CI ili odluke vlasnika.

## Svrha i trenutni status

Lokalni Windows asistent: desktop shell, razgovor i glas, odobravanje sistemskih radnji, audit, dijagnostika i odvojeni Coding Workspace. `main` je 6. septembra 2026. spojio PR #20 — redizajn interfejsa oko stvarnih runtime događaja. `docs/ai/PROJECT_STATE.md` ima stariji odjeljak koji #20 naziva otvorenim draftom; to je istorijski presjek i više nije trenutni status. Verzija u izvoru je RC/alpha i nema potvrđenog javnog izdanja ili instalacije na fizičkom korisničkom računaru. Otvoreni draftovi #13, #14 i #16 zahtijevaju zasebnu provjeru baza i naslaganih zavisnosti.

## Arhitektura i sigurnosne granice

- Python paket `app/`, lokalni API u `app/api/`, provider interfejsi `app/core/ai/`, akcije sa politikom/odobrenjima i auditom, te `app/coding/` za izolovani workspace, browser QA i kontrolisane izmjene. Baza i migracije su u `db/`, distribucija u `installer/` i `packaging/`.
- `README.md`, `QUICKSTART.md` i `docs/ai/PROJECT_STATE.md` objašnjavaju opcije lokalnog pokretanja; `CLAUDE.md`, `SECURITY.md`, `docs/THREAT_MODEL.md` i `docs/release-process.md` su mjerodavne granice. Posebno sačuvati loopback-only API, odobrenja prije sistemskih radnji, redakciju tajni, potpunu audit evidenciju i činjenicu da UI stanje dolazi od runtime događaja, a ne animacije koja glumi aktivnost.
- Za Coding Workspace držati ograničenja putanja, kontrolu git promjena i zabranu samostalnog izvršavanja van odobrenog prostora. Cloud AI/glas je opcioni spoljašnji servis; ne opisivati sve kao offline.

## Provjera i sljedeći korak

`python -m compileall app db` i `pytest` po `README.md`/CI; izolovanu bazu i varijable koristiti kao u `docs/ai/PROJECT_STATE.md`. Windows installer provjeriti preko odgovarajućih workflowa, ali unsigned CI artefakt nije javno izdanje. Mikrofon, zvuk, SmartScreen, instalacija/deinstalacija i fizički PC zahtijevaju ljudsku provjeru. Prije nastavka prvo uskladi #20 stanje u starijem PROJECT_STATE odjeljku i odluči da li draft PR-ovi još imaju vrijednost.

## Kako nastaviti u novom chatu

1. Otvori ovaj dokument, `README.md`, najnoviji commit na `main` i otvorene PR-ove. Stariji planovi i nacrti ne nadjačavaju noviji kod.
2. Za konkretan zadatak pročitaj navedene izvorne fajlove i provjeri odgovarajuće podatke sa vlasnikom; ne dopunjavaj činjenice nagađanjem.
3. Pokreni relevantne provjere, pregledaj promjenu na telefonu i računaru kada dira UI, pa dokumentuj šta je stvarno objavljeno. Nikad ne upisuj lozinke, tokene, lične podatke ili sadržaj privatnih razgovora u Git.

## Detaljni pregled — 21. oktobar 2026.

**Izvor provjere:** `main` na `11ca22a8a09eba9929e76ebfebbddbf8d9ccffac` (dokumentacioni commit); README, `SECURITY.md`, `docs/ai/PROJECT_STATE.md` i otvoreni PR-ovi. Ovaj pregled nije novo testiranje na Windows računaru niti potvrda javnog izdanja.

| Područje | Gdje se održava | Granica |
| --- | --- | --- |
| Lokalni API i razgovor | `app/api/`, `app/core/ai/` | Podrazumijevani loopback `127.0.0.1:5555`; cloud AI je opcioni servis, a bez ključa ostaju lokalne determinističke komande. |
| Radnje i sigurnost | politika odobravanja, audit, `SECURITY.md`, `docs/THREAT_MODEL.md` | Rizične radnje prolaze provjeru/odobrenje; tajne se rediguju; Coding Workspace je ograničen na odobrene putanje. |
| Desktop i distribucija | `app/desktop/`, `installer/`, `packaging/`, `docs/WINDOWS_INSTALLER.md` | Windows 10/11 i WebView2; installer per-user čuva podatke odvojeno. CI artefakt je unsigned i privremen, nije javni release. |
| Podaci i testovi | `db/`, `tests/`, `.github/workflows/` | SQLite i lokalna evidencija; `python -m compileall app db` i `pytest` su komande za svježu provjeru. Ovdje nijesu ponovo izvršene. |

**Otvoren rad:** draft [#13](https://github.com/Dado211207/jarvis-windows-ai-assistant/pull/13) (settings/memory) cilja `main`; draft [#14](https://github.com/Dado211207/jarvis-windows-ai-assistant/pull/14) (installer/onboarding) je naslagan na granu #13; draft [#16](https://github.com/Dado211207/jarvis-windows-ai-assistant/pull/16) popravlja ISPP packaging. Nijedan od njih nije dio ovog `main` presjeka samo zato što postoji PR. Prije spajanja provjeri bazu svake grane, CI i konfliktne promjene.

**CI nalaz pri pregledu:** run [38202719413](https://github.com/Dado211207/jarvis-windows-ai-assistant/actions/runs/38202719413) na prethodnom `main` `11ca22a8` je završio neuspjehom: Playwright + axe posao je pao na koraku browser/accessibility, dok su Python 3.11 i Windows smoke prošli. Uzrok iz loga nije utvrđen ovim dokumentacionim pregledom. Naknadni CI run [39635674789](https://github.com/Dado211207/jarvis-windows-ai-assistant/actions/runs/39635674789) i Windows Build [39635674966](https://github.com/Dado211207/jarvis-windows-ai-assistant/actions/runs/39635674966) za dokumentacioni `main` `b56c2b6b` završili su uspješno. To ne zamjenjuje fizičku Windows prihvatnu provjeru niti dijagnostiku ranijeg prolaznog pada.

**Prije distribucije:** fizički Windows PC, mikrofon i zvuk, WebView2, SmartScreen, instalacija/deinstalacija, čuvanje korisničkih podataka i provjera SHA-256 instalera. GitHub Actions release tok je u `docs/release-process.md`; ne pokreći ga samo na osnovu prolaza statičkih testova. Sljedeći korak je uskladiti starije statusne odjeljke u `docs/ai/PROJECT_STATE.md`, pa zasebno pregledati naslagane draftove.
