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

