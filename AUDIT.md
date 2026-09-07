# Hĺbkový audit a vylepšenia — Flowise MCP Research Server (v0.9.4)

Tento dokument zhŕňa výsledky hĺbkového auditu celej kódovej základne (~6 700 riadkov Pythonu),
implementované opravy (v0.2.0), kvalitatívne rozšírenie hĺbkovej analýzy zdrojov (v0.3.0,
sekcia 8), vylepšenia vyhľadávania a výstupu (v0.4.0, sekcia 9), spracovanie celých
dokumentov pre pokrytie prvkov (v0.5.0, sekcia 10), skutočnú AlphaXiv MCP integráciu
(v0.6.0, sekcia 11), opravy vyplývajúce z analýzy skutočného produkčného behu vo
Flowise (v0.7.0, sekcia 12), obídenie blokovania Google Patents cez oficiálne
patentové PDF (v0.8.0, sekcia 13), merateľné zlepšenie hodnotenia relevancie
(v0.9.0, sekcia 14), čistotu textu vo výslednom reporte (v0.9.1, sekcia 15)
kvalitu patentových výsledkov pri zablokovanom provideri (v0.9.2, sekcia 16)
a kvalitu patentových nálezov overenú celým behom (v0.9.4, sekcia 17).
Všetky zmeny sú overené: **270 automatických testov prechádza**, server po
zmenách naštartoval a MCP `initialize` handshake vrátil platnú odpoveď.

## 1. Opravené chyby (korektnosť)

### 1.1 OpenAlex provider bol úplne nefunkčný — `tools/publications_search.py`
`_openalex_search` posielal do OpenAlex API parameter `select` s neexistujúcim poľom
`authors_count`. API na to odpovedá **HTTP 400** pri každom volaní a výnimka sa potichu
zahadzovala v `_openalex_blocks_safe`, takže jeden z piatich publikačných providerov
nikdy nevrátil žiadny výsledok. Overené živým volaním API pred opravou (400) aj po nej (200).
**Oprava:** odstránené neplatné pole zo `select`.

### 1.2 Chybná logika pokrytia požiadaviek v reporte — `tools/user_answer.py`
V záložnej vetve `_render_uncertainty_section` sa porovnával **počet** pokrytých požiadaviek
zdroja s **poradovým číslom** požiadavky (`count >= index`). Výsledok: report mohol tvrdiť
„verified disclosure by: patent", aj keď zdroj overil úplne inú požiadavku. Navyše sa
používali labely (`verified_disclosure_by`, `not_verified`), ktoré v slovníku labelov
neexistovali, takže slovenský report obsahoval anglické frázy.
**Oprava:** záložná vetva teraz zobrazuje čestný per-zdrojový súhrn („patent: 2/5 požiadaviek
plne overených") a pre detailné pokrytie po prvkoch sa vždy, keď existujú kritické požiadavky,
generujú „pseudo-atómy", takže tabuľka *Element-by-element coverage* sa zobrazí aj bez
atomického rozkladu. Pseudo-atómy sa používajú **iba na zobrazenie** — logika verdiktu
a dôvery zostáva nezmenená.

### 1.3 Únik SQLite spojení — `tools/research_session.py`
`with _connect() as conn:` využíval kontextový manažér `sqlite3.Connection`, ktorý transakciu
commitne/rollbackne, ale **spojenie nikdy nezatvorí**. Každé volanie nástroja tak nechalo
otvorený handle na databázu (a WAL/SHM súbory) až do garbage collection — problém pre
dlhobežiaci server aj pre Windows (zamknuté súbory).
**Oprava:** `_connect()` je teraz `@contextmanager`, ktorý spojenie po použití vždy zavrie.
Existujúcich ~20 volacích miest funguje bez zmeny. Nový test `test_db_file_not_locked_after_operations`
overuje, že po operáciách sa dá databázový súbor na Windows premenovať (t. j. nie je držaný handle).

### 1.4 Zastarané arXiv API — `tools/arxiv_search.py`
Používalo sa deprecated `Search.results()` (odstraňované v novších verziách knižnice `arxiv`).
**Oprava:** prechod na `arxiv.Client(...).results(search)` s obmedzeným `page_size` a retry.

### 1.5 Startup banner ukazoval neexistujúce nástroje — `terminal_ui.py`
Banner vypisoval štyri staré názvy (`patent_evidence_pack`, `merge_evidence_pack`, ...),
ktoré server vôbec neregistruje.
**Oprava:** banner vypisuje presne 7 registrovaných MCP nástrojov; test kontroluje zhodu
so skutočnou registráciou cez `server.mcp.list_tools()`.

### 1.6 Neobmedzený rast cache v pamäti — `tools/patent_search.py`, `tools/patent_fetch.py`
`_PATENT_SEARCH_CACHE` aj `_PATENT_FETCH_CACHE` boli obyčajné slovníky bez limitu — pamäť
dlhobežiaceho servera rástla bez obmedzenia.
**Oprava:** nový modul `tools/_ttl_cache.py` (`TTLCache`) s TTL aj stropom počtu záznamov
(64 pre vyhľadávanie, 256 pre fetch) a LRU-štýlovým vyraďovaním najstarších položiek.

### 1.7 Pád zápisu pri poškodených dátach providera — `tools/research_session.py`
`_insert_raw_items` volal `float(hit.get("relevance_score") ...)` bez ochrany — nečíselná
hodnota od providera zhodila celý zápis dôkazov.
**Oprava:** nový `_safe_float` helper; rovnaké bezpečné pretypovanie aj v `_row_quality_key`.

### 1.8 Playwright ako tvrdá závislosť — `tools/chromium_scraper.py`
Import Playwrightu na úrovni modulu znamenal, že bez nainštalovaného Playwrightu sa nedal
importovať ani celý balík `tools` (a teda ani spustiť server či testy).
**Oprava:** lenivý import vnútri funkcie + náhradná trieda `PlaywrightTimeoutError`.
Web/patent fetch fallbacky (Jina, Wayback, Crossref, statické HTTP) tento stav už korektne
zachytávajú, takže server je použiteľný aj bez Chromium vrstvy.

## 2. Menšie opravy a čistenie

- `tools/evidence_quality.py`: zlúčená duplicitná vetva `weak` a odstránená mŕtva premenná.
- `tools/patent_search.py`: odstránená mŕtva funkcia `_matches_required_concept` (vždy vracala True).
- `tools/web_evidence_pack.py`: podmienka preskočenia nepodporovaných URL bola nedosiahnuteľná,
  lebo titulok má vždy placeholder „Untitled result" — placeholder sa už nepočíta ako reálny titulok.
- `tools/user_answer.py` `_detect_language`: envelope hodnota `non_english` (ktorú reálne generuje
  `research_session._detect_query_language`) sa teraz mapuje priamo na slovenský výstup;
  predtým sa spoliehalo len na regex nad dotazom.
- `tools/research_session.py`: `trace_events` filter `source_type != ''` (stĺpec je NOT NULL),
  odstránené duplicitné hodnotenie kvality v `_row_quality_key`, presunuté importy pred `LOGGER`.
- `tools/web_search.py`: kozmetika (f-string bez placeholderu).

## 3. Konfigurácia a nasadenie

- **`pyproject.toml`**: verzia 0.2.0; doplnené `server_http` a `terminal_ui` do `py-modules`
  (predtým `pip install .` nezahŕňal HTTP vstupný bod); nový skript `mcp-research-server-http`;
  `[project.optional-dependencies] dev` (pytest, pytest-asyncio); `[tool.pytest.ini_options]`.
- **`.env.example`**: doplnené všetky premenné, ktoré kód reálne číta a chýbali:
  `PUBMED_API_KEY`, `ALPHA_CLI_PATH`, `FLOWISE_USERNAME/PASSWORD`, `MCP_HOST/PORT/PATH`,
  `MCP_ALLOWED_HOSTS/ORIGINS`, `RESEARCH_SESSION_DB`, `NO_COLOR`/`MCP_PLAIN_UI`/`MCP_NO_UI`.
- **`docker-compose.yml`**: pridaný passthrough `PUBMED_API_KEY`; healthcheck MCP servera
  (TCP kontrola portu 8000); Flowise teraz čaká na `condition: service_healthy`, takže sa
  nespustí skôr, než je MCP server pripravený.
- **`Dockerfile`**: `HEALTHCHECK` inštrukcia pre samostatné spustenie mimo compose.

## 4. Nová testovacia sada (predtým: žiadne testy)

`tests/` — **126 testov**, všetky bez prístupu na sieť (siete sa stubujú cez monkeypatch):

| Súbor | Pokrýva |
|---|---|
| `test_query_normalize.py` | čistenie dotazov z Flowise (dict/JSON/list vstupy) |
| `test_relevance.py` | tokenizácia, skórovanie, prahy relevancie, subject anchors |
| `test_result_contract.py` | stavové markery a ich round-trip parsovanie |
| `test_evidence_quality.py` | známkovanie zdrojov, verdikty, dôvera, degradácie |
| `test_hit_sort_and_cache.py` | triedenie nálezov, TTLCache (expirácia, vyraďovanie) |
| `test_patent_filters_and_bounds.py` | extrakcia patentových čísel, validácia výsledkov, plán dotazov |
| `test_merge_evidence_pack.py` | zlučovanie zdrojov, legacy bundle, opravy URL, flagy |
| `test_final_answer_pack.py` | debug report, sekcie, konzervatívne správanie pri chybe |
| `test_user_answer.py` | payload odpovede, jazyk (SK/EN), tabuľka pokrytia, pseudo-atómy |
| `test_research_session.py` | celý SQLite workflow: start → understand → save → checklist → answer, duplicity, budget, uzavretá session, zamykanie súborov, async writery so stubmi |
| `test_evidence_packs_stubbed.py` | patent/publication/web evidence pack pipeline so zastubovanou sieťou |
| `test_web_and_publication_helpers.py` | tracking parametre, low-value URL, MDPI→DOI, PubMed XML, OpenAlex abstrakt |
| `test_server_and_ui.py` | registrácia presne 7 MCP nástrojov, `_safe_writer_ack`, banner |
| `test_output_cleaner.py` | čistenie HTML, claim coverage, normalizácia identity patentu |

Spustenie: `pip install -e .[dev]` (alebo `pip install pytest pytest-asyncio`) a `python -m pytest`.

## 5. Overenie funkčnosti

1. `python -m compileall` — bez chýb.
2. `python -m pytest tests` — **126 passed**.
3. `python server_http.py` — server naštartoval, `POST /mcp` s JSON-RPC `initialize`
   vrátil **HTTP 200** a platný `serverInfo` ("Research Server").

## 6. Čo sa zámerne nemenilo

- Rozhrania všetkých 7 MCP nástrojov (mená, parametre, tvar JSON odpovedí) — plná
  kompatibilita s exportovanou Flowise architektúrou `flowise_architecture/Flowise_agent.json`.
- Logika verdiktov a dôvery (`decide_verdict_and_confidence`) — správanie je teraz
  zafixované testami, nie zmenené.
- Deterministický charakter systému (žiadne LLM volania na strane servera).

## 8. Hĺbková analýza zdrojov (v0.3.0)

Rozšírenie zamerané na body „Prehĺbiť analýzu zdrojov" a „Zlepšiť hodnotenie relevancie"
z prezentácie obhajoby: systém teraz spracúva podstatne viac obsahu z každého zdroja
a meria pokrytie prvkov dotazu jednotne naprieč celým tokom.

### 8.1 Spracovanie PDF dokumentov — `tools/pdf_fetch.py` (nový modul)
PDF zdroje (datasheety, manuály, odborné články) sa predtým zahadzovali ako
„snippet-only" dôkaz — obsah dokumentu sa nikdy nečítal. Nový modul PDF stiahne
(limit 15 MB, streaming), extrahuje text z prvých 25 strán cez `pypdf` a webový
evidence pack ho spracuje ako plnohodnotný `fetched_excerpt` dôkaz. Pri zlyhaní
extrakcie zostáva pôvodné snippet-only správanie. Overené naživo na reálnom
arXiv PDF (6 123 extrahovaných slov, správne vypočítané pokrytie prvkov).

### 8.2 Hlbší webový fetch — `tools/web_search.py`
Načítaná stránka doteraz poskytla max. ~450 slov (8 viet). Fetch výstup má teraz
navyše pole `ANALYSIS` s až 900 slovami z 24 najrelevantnejších viet. Zobrazovaný
súhrn ostáva krátky; rozšírený text sa používa na skórovanie a výpočet pokrytia.

### 8.3 Plné patentové nároky — `tools/patent_fetch.py`
Z patentovej stránky sa doteraz extrahoval iba nárok 1 a abstrakt. Teraz sa extrahuje
aj celá sekcia nárokov (`CLAIMS_TEXT`, do 1 200 slov) a pokrytie požiadaviek sa počíta
nad nárokom 1 + všetkými nárokmi + abstraktom. Kombinácia prvkov roztrúsená medzi
závislými nárokmi (typický prípad) sa tak už zachytí.

### 8.4 Jednotné meranie pokrytia — `tools/requirement_match.py` (nový modul)
Stem-aware porovnávanie požiadaviek (doteraz len vo finálnom reporte) je vyčlenené
do zdieľaného modulu a používajú ho všetky tri evidence packy aj report. Patentový
pack predtým používal naivné `term in text` (bez skloňovania). Zároveň opravený
defekt stemmera: `codes` sa stemovalo na `cod`, ale `code` na `code`, takže jednotné
a množné číslo sa nikdy nespárovalo; nové poradie prípon to rieši.

### 8.5 Atom coverage pre všetky zdroje + oživenie exact_match
Audit ukázal, že `exact_combination_candidate_found` **nikde nič nenastavovalo** —
vetva verdiktu `exact_match` a stop_reason `exact_combination_found` boli mŕtva
funkcionalita. Teraz:
- atomické požiadavky z query envelope dostávajú všetky tri writery (predtým len patentový),
- každý nález nesie `atom_coverage`/`atom_match_count` (podiel prvkov dotazu plne
  pokrytých obsahom dokumentu),
- pri plnom pokrytí overeným dokumentom sa nastaví `exact_combination_candidate_found=True`,
  čo sa cez SQLite prenesie do checklistu (`stop_reason=exact_combination_found`)
  aj verdiktu (`exact_match`) — overené integračným testom,
- relevancia sa pri vysokom pokrytí (≥ 50 %) upgraduje na `direct`/`focused`,
  a report pri každom náleze zobrazuje „pokrytie prvkov: X %".

### 8.6 Overenie v0.3.0
- `python -m pytest` — **145 passed** (19 nových testov: requirement_match, pdf_fetch
  s ručne zostaveným validným PDF, PDF pipeline vo web packu, exact candidate z plných
  nárokov, upgrade relevancie publikácií, prenos atómov do writerov, end-to-end prenos
  exact flagu až do verdiktu).
- Živý test PDF extrakcie na reálnom arXiv dokumente.
- Server naštartoval a MCP `initialize` vrátil HTTP 200.

## 9. Vylepšenia vyhľadávania a výstupu (v0.4.0)

### 9.1 Google Patents ako bezkľúčový patentový provider — `tools/patent_search.py`
Patentové vyhľadávanie doteraz stálo na Tavily/Exa API kľúčoch a krehkom WIPO scrapingu —
bez kľúčov systém prakticky nenachádzal patenty. Pridaný provider
`_google_patents_xhr_search` používa natívne JSON rozhranie `patents.google.com/xhr/query`
(bez API kľúča) a je zaradený medzi primárnych providerov (jeho dokončenie bez nálezov je
spoľahlivý negatívny signál). Overené naživo: dotaz na inteligentný zámok vrátil 5
relevantných patentov (US, EP, CN) úplne bez nakonfigurovaných kľúčov. Zároveň rozšírené
`include_domains` pre Tavily/Exa o patents.justia.com a worldwide.espacenet.com.

### 9.2 Synonymná a akronymová expanzia dotazov — `tools/query_expansion.py` (nový modul)
Pole `synonyms` v query envelope bolo doteraz vždy prázdne a retry pokusy často opakovali
takmer rovnaké varianty dotazu. Nový deterministický lexikón (vysoko spoľahlivé technické
ekvivalenty: IoT ↔ internet of things, ML ↔ machine learning, anomaly ↔ outlier detection,
predictive ↔ condition-based maintenance, smart ↔ intelligent…) generuje varianty so
zachovaným významom. Envelope teraz vypĺňa `synonyms` a variantné zoznamy pre
patent/publication/web obsahujú aj expanzie — retry pokusy tak prehľadávajú reálne
odlišný priestor výsledkov.

### 9.3 arXiv ako plnohodnotný paralelný provider — `tools/publications_search.py`
arXiv sa doteraz používal len ako núdzový fallback po zlyhaní Semantic Scholar. Teraz beží
paralelne s ostatnými piatimi providermi, jeho bloky majú štandardný formát (deduplikácia
podľa názvu/DOI funguje naprieč providermi) a kvalitatívny multiplikátor 0.9.

### 9.4 Query-focused výňatky abstraktov — `tools/publications_search.py`
Abstrakty sa doteraz orezávali na prvých 60 slov — relevantná veta na konci dlhého
abstraktu sa do dôkazu nikdy nedostala. `_relevant_abstract_excerpt` teraz vyberá vety
s najväčším prekryvom s dotazom (pri zachovaní poradia) pre všetkých providerov
(Semantic Scholar, Crossref, PubMed, OpenAlex, AlphaXiv).

### 9.5 Kros-zdrojová korohorácia dokumentov — `tools/user_answer.py`
Keď rovnaký dokument (patentové číslo bez kind kódu, DOI alebo URL) potvrdí viac typov
zdrojov nezávisle, systém to teraz deteguje: report obsahuje riadok „Nezávislé
potvrdenie: …", dotknuté nálezy nesú značku „potvrdené viacerými zdrojmi" a payload
pole `corroborated_documents`. Nezávislé potvrdenie z rôznych vyhľadávacích ciest je
silný signál správnosti nálezu.

### 9.6 Overenie v0.4.0
- `python -m pytest` — **159 passed** (14 nových testov: expanzia dotazov a envelope
  integrácia, parsovanie XHR odpovede Google Patents, arXiv adaptér a dedup kľúče,
  query-focused výňatky, detekcia korohorácie vrátane zhody patentovej rodiny).
- Živý test: `patent_search` bez API kľúčov vrátil 5 relevantných patentov.
- Živý test tvaru XHR odpovede pred implementáciou parsera.
- Server naštartoval a MCP `initialize` vrátil HTTP 200.

## 10. Pokrytie prvkov z celých dokumentov (v0.5.0)

Do v0.4.0 sa pokrytie prvkov dotazu počítalo z kondenzácií (výber viet relevantných
k dotazu). To malo slabinu: veta obsahujúca chýbajúci prvok, ktorá sa nedostala do
výberu, sa do pokrytia nikdy nezapočítala. Teraz pokrytie vidí obsah celého dokumentu.

### 10.1 Web: tokeny celej stránky — `tools/web_search.py`, `tools/web_evidence_pack.py`
Výstup fetch nástroja obsahuje nové pole `COVERAGE_TOKENS` — deduplikované tokeny
**celého** extrahovaného textu stránky (strop 2 500 unikátnych tokenov). Keďže matching
pokrytia overuje prítomnosť termínov, deduplikovaná množina tokenov zachováva výsledok
zhody a prenesie obsah celej stránky kompaktne. Zobrazovaný súhrn a skórovanie relevancie
sa nemenia (tam by viac textu signál riedilo). Rovnako pre PDF dokumenty vo web packu.

### 10.2 Patenty: sekcia description — `tools/patent_fetch.py`, `tools/patent_evidence_pack.py`
Z patentovej stránky sa okrem nárokov a abstraktu extrahuje aj opis vynálezu
(`DESCRIPTION_TEXT`, do 1 500 slov; selektory `section[itemprop='description']` a
alternatívy). Opis býva technicky bohatší než právny text nárokov — pokrytie prvkov
sa počíta nad nárokmi + abstraktom + opisom.

### 10.3 Publikácie: plné PDF texty — `tools/publication_evidence_pack.py`
Pre kandidátov s voľne dostupným plným textom (arXiv `abs` → `pdf`, priame `.pdf` odkazy)
sa stiahne celý článok cez existujúcu PDF pipeline (max. 3 dokumenty na pokus, len keď
existujú atomické požiadavky). Plný text sa použije na pokrytie prvkov; nález nesie
`fulltext_analyzed` a `fulltext_word_count`. Ak zlyhal fetch stránky publikácie, úspešné
prečítanie plného textu zdvihne úroveň dôkazu na `fetched_excerpt` — publikácia tak môže
byť podkladom pre `exact_combination_candidate_found` aj bez overeného abstraktu.

### 10.4 Overenie v0.5.0
- `python -m pytest` — **166 passed** (7 nových testov: COVERAGE_TOKENS obsahujú vety mimo
  kondenzácie, pokrytie z tokenov celej stránky, extrakcia a využitie patentového opisu,
  odvodenie PDF URL, pokrytie z plného textu publikácie s upgrade úrovne dôkazu,
  šetriace správanie bez atomických požiadaviek).
- Server naštartoval a MCP `initialize` vrátil HTTP 200.

## 11. Skutočná AlphaXiv MCP integrácia (v0.6.0)

Pri overovaní `.env` premenných sa ukázalo, že `ALPHA_CLI_PATH` (voliteľný lokálny CLI
nástroj `alpha`) bol od začiatku nefunkčný predpoklad — AlphaXiv v skutočnosti nevystavuje
žiadny inštalovateľný CLI, ale **vlastný vzdialený MCP server** na
`https://api.alphaxiv.org/mcp/v1` s autorizáciou cez `Authorization: Bearer <kľúč>`
(kľúč sa vytvára v Settings > API Keys na alphaxiv.org). Overené priamym stiahnutím
oficiálnej dokumentácie MCP servera (`/docs/mcp`), ktorá popisuje nástroj `discover_papers`
(vstup: `keywords`, `question`, `difficulty`; výstup: 5-15 článkov s title, publication
date, organizations, abstract preview, arXiv ID).

### 11.1 Nový modul `tools/alphaxiv_client.py`
Keďže projekt už závisí od balíka `mcp` (na strane vlastného servera), rovnaký balík
poskytuje aj klientsku stranu (`mcp.client.streamable_http.streamablehttp_client` +
`mcp.ClientSession`). Nový modul sa pripojí, zavolá `discover_papers` a výsledok
(`CallToolResult`) spracuje robustne: najprv `structuredContent` (kľúče `result`,
`papers`, `results`, `data`, `items`), potom textové bloky (JSON na blok — presne
takto FastMCP serializuje vrátené zoznamy — overené priamym testom voči knižnici pred
implementáciou), napokon spojený text ako záložná možnosť pre textový parser.
Bez `ALPHAXIV_API_KEY` sa nepokúša o žiadne sieťové pripojenie; pri zlyhaní (zlý kľúč,
timeout, chyba nástroja) ticho vráti prázdny zoznam — provider je vždy len doplnkový.
Overené naživo: neplatný kľúč zlyhá čisto za 0,83 s bez zaseknutia.

### 11.2 Napojenie do `tools/publications_search.py`
Pôvodné `_alpha_executable`/`_run_alpha_command` (subprocess na lokálny binárny súbor)
nahradené volaním `alphaxiv_client.discover_papers`. `_format_alpha_item` rozšírený o
dokumentované polia AlphaXiv (`abstractPreview`, `publicationDate`, `organizations`) —
keď položka nesie iba `organizations` (inštitúcie) bez `authors` (osoby), report to
korektne označí ako „Organizations listed…", nie zavádzajúco ako autorov.

### 11.3 Testovanie skutočným MCP protokolom bez siete
Balík `mcp` poskytuje `mcp.shared.memory.create_connected_server_and_client_session` —
pomôcku na prepojenie klienta so serverom cez in-memory transport bez HTTP. Testy preto
postavia skutočný dočasný FastMCP server s nástrojom `discover_papers` a náš klient
naň volá reálnym MCP protokolom (JSON-RPC framing, initialize handshake, serializácia
výsledkov) — nejde o obyčajný mock, ale o overenie proti skutočnej implementácii
knižnice, len bez sieťovej vrstvy.

### 11.4 Konfigurácia
`.env.example` a `docker-compose.yml`: `ALPHA_CLI_PATH` nahradené `ALPHAXIV_API_KEY`.
Startup banner (`terminal_ui.py`) teraz sleduje aj `ALPHAXIV_API_KEY` a `PUBMED_API_KEY`.

### 11.5 Overenie v0.6.0
- `python -m pytest` — **189 passed** (23 nových testov: parsovanie `CallToolResult` pre
  všetky tvary odpovede, plný protokolový beh cez in-memory FastMCP server vrátane
  viacpoložkových výsledkov a chyby nástroja, napojenie do `_alpha_search_blocks`
  s dict aj textovou vetvou, deduplikácia, tichý fallback pri výnimke).
- Živý test proti reálnemu `mcp` balíku potvrdil presný tvar `CallToolResult` pre
  nástroje vracajúce zoznam (samostatný JSON blok na položku + `structuredContent`
  pod kľúčom `result`) — na tomto zistení stojí parsovacia logika.
- Živý test odolnosti: neplatný `ALPHAXIV_API_KEY` zlyhá za 0,83 s, nezasekne sa.
- Server naštartoval a MCP `initialize` vrátil HTTP 200.

## 12. Opravy z analýzy reálneho produkčného behu (v0.7.0)

Používateľ spustil reálny dotaz cez Flowise (anglická otázka o detekcii anomálií
v logoch, 227 s, ~249k tokenov) a poslal celý transkript vrátane surových MCP
odpovedí. Analýza tohto behu — vrátane naživo overených hypotéz priamo proti
Google Patents, Justia a Wayback Machine — odhalila šesť konkrétnych problémov.

### 12.1 Google Patents blokuje automatizované požiadavky (najvýznamnejší nález)
Live test (`httpx.get` na `patents.google.com/patent/...`) vrátil **HTTP 503**
s telom *"...your computer or network may be sending automated queries..."* —
identický blok, aký zažíval kontajner používateľa. Dôsledok: vo všetkých 4
pokusoch o patentové dôkazy takmer každý `patent_fetch` skončil s maskovanou
chybou (`STATUS: FAILED`, `CLAIM1: No first claim was extracted`), hoci v
skutočnosti stránka nikdy nebola reálne dostupná — patentové číslo sa dalo
vytiahnuť aj z URL bloku, takže chyba vyzerala ako bežné zlyhanie parsovania.
V dôsledku toho sa `claim_coverage`/`DESCRIPTION_TEXT`/`exact_combination_candidate_found`
z v0.3.0–v0.5.0 pre patenty **nikdy nespustili** — potvrdené aj vo výstupe:
žiadny patentový nález neukazoval "element coverage".

**Oprava (`tools/patent_fetch.py`):**
- `_is_bot_block_page()` rozpozná blokovaciu stránku podľa charakteristického textu.
- Nová trieda `BotBlockedError` a jasný stav `STATUS: BLOCKED` / `EVIDENCE_LEVEL: FETCH_BLOCKED`
  namiesto zavádzajúceho "no claim/abstract" — `patent_evidence_pack.py` teraz do
  varovania píše zrozumiteľnú vetu o zablokovaní, nie orezaný surový text.
- `asyncio.Semaphore` obmedzujúci súbežné požiadavky na `patents.google.com`
  na 2 naraz naprieč celým behom (predtým sa mohlo naraz spustiť až 6 fetchov
  na tú istú doménu — pravdepodobne práve to blok spúšťa).
- Best-effort fallback na Wayback Machine snímku pri detegovanom blokovaní.
  **Poctivé priznanie:** live test ukázal, že Wayback dostupnosť pre konkrétnu
  patentovú stránku nie je spoľahlivá (v mojom teste vrátila 0 bajtov) — je to
  bonus, keď vyjde, nie garantovaná oprava. Justia bola vyskúšaná a zavrhnutá:
  chránená Cloudflare výzvou (`"Just a moment..."`), nedá sa použiť bez plného
  prehliadača.
- Live overenie opravy: reálne volanie `patent_fetch` na skutočne zablokovanú
  stránku teraz vráti čisté `STATUS: BLOCKED` namiesto matúceho `STATUS: FAILED`.

### 12.2 Jina Reader bez API kľúča (403 pri viacerých publikáciách)
`tools/jina_reader.py` nepodporoval žiadny kľúč; bezplatný anonymný limit sa
vo výstupe prejavoval ako opakované `403 Forbidden` pri overovaní DOI odkazov.
**Oprava:** `JINA_API_KEY` env premenná, poslaná ako `Authorization: Bearer`.

### 12.3 web_evidence_pack strácal skutočný dôvod chyby
Na rozdiel od `publication_evidence_pack.py` (má `_errors_from_markers`),
`web_evidence_pack.py` nikdy neparsoval `ERROR: typ - správa` riadky z
`web_search` výstupu — pri chybe providera dostal supervisor len generickú
vetu "see server logs". **Oprava:** pridaný rovnaký parser, `errors` pole
teraz obsahuje skutočný typ a správu, varovanie cituje konkrétny dôvod.

### 12.4 Výber variantu dotazu zahodil dôležitý prvok pozične, nie podľa významu
V reálnom behu štvrtý (posledný) patentový pokus stratil "processes events in
real time" — `_query_variants_from_atoms` v `tools/research_session.py`
mechanicky orezávala `labels[len//2:]` bez ohľadu na dôležitosť. **Oprava:**
zoradenie ostatných atómov tak, aby konkrétnejšie kategórie (`function`,
`mechanism_or_principle`, `constraint`) mali prednosť pred všeobecným
`object_or_form_factor` pri prípadnom orezaní na limit tokenov — jadro dotazu
sa teraz drží kompletné namiesto ľubovoľného zahodenia polovice podľa pozície.

### 12.5 Plný text PDF sa neskúšal pre DOI odkazy na časopisy (len arXiv)
Väčšina publikačných nálezov v reálnom behu boli `doi.org/10.3390/...` a
podobné odkazy na časopisy — PDF pipeline z v0.5.0 sa aktivovala len pre
arXiv/priame `.pdf` URL. **Oprava:** nová funkcia `_unpaywall_pdf_url()`
v `publication_evidence_pack.py` volá bezplatné Unpaywall API (bez kľúča,
len kontaktný e-mail — placeholder `*@example.com` je odmietnutý, preto
vlastná `.local` doména) a pre otvorene dostupné DOI nájde priamy PDF odkaz.
Live overené na troch reálnych DOI z produkčného behu — všetky tri sa
rozlíšili na funkčný PDF odkaz (napr. `mdpi.com/.../pdf?version=...`).

### 12.6 ACK odpovede duplikovali obsah a nafukovali tokeny
Writer ACK obsahoval popri `compact_warnings` aj plné needitované pole
`warnings` s tým istým (dlhším) textom vrátane úryvkov zo zlyhaného scrapovania
(napr. `"...CLAIM1: No fir"`) — v jemnom rozpore s inštrukciou promptu "never
inspect fetched pages", a zbytočne to nafukovalo kontext (beh mal 249k tokenov).
Zároveň `research_session_checklist` posielal ten istý zoznam retry akcií
dvakrát pod dvomi kľúčmi (`retry_actions` a `recommended_next_actions`).
**Oprava:** nová funkcia `_truncated_warnings()` skráti text každej položky na
240 znakov (počet položiek zostáva pre `warning_count` nezmenený); duplicitný
kľúč `recommended_next_actions` odstránený, interná funkcia
`research_session_plan_next` teraz číta z `retry_actions`.

### 12.7 Overenie v0.7.0
- `python -m pytest` — **217 passed** (21 nových testov vrátane plnej simulácie
  Google blokovania a Wayback zotavenia, obmedzenia súbežnosti semaforom,
  Jina Bearer hlavičky, parsovania chýb z web_search markerov, Unpaywall
  rozlíšenia s mockovaným aj naživo overeným API tvarom, dôležitosťou vedeného
  výberu variantu dotazu, orezania warnings a zjednotenia retry_actions).
- Tri nezávislé live overenia priamo proti internetu **pred** písaním kódu
  (Google Patents blok, Justia Cloudflare blok, Wayback dostupnosť) a jedno
  **po** oprave (skutočné `patent_fetch` volanie na zablokovanú stránku
  teraz vracia čisté `STATUS: BLOCKED` namiesto zavádzajúceho `STATUS: FAILED`).
- Live overenie Unpaywall na presne tých DOI, ktoré sa objavili v reálnom behu.
- Server naštartoval a MCP `initialize` vrátil HTTP 200.

## 13. Obídenie blokovania cez oficiálne patentové PDF (v0.8.0)

Verzia 0.7.0 spravila blokovanie Google Patents **viditeľným a čestným**, ale
nevyriešila ho — celá hĺbková patentová analýza (nároky, opis, `claim_coverage`,
`exact_combination_candidate_found`) sa naďalej nespúšťala, lebo obsah patentu sa
nedal získať. Verzia 0.8.0 to rieši.

### 13.1 Nález: XHR odpoveď obsahuje cestu k oficiálnemu PDF
Pri skúmaní surovej odpovede `patents.google.com/xhr/query` (rozhranie, ktoré už
používame na vyhľadávanie) sa ukázalo, že každý výsledok nesie pole `pdf` s cestou
na **samostatný Google storage bucket**:

```
"pdf": "30/12/f3/1b616ac6c5a32a/US10831585.pdf"
```

Live overenie potvrdilo, že `patentimages.storage.googleapis.com` **nepodlieha
ochrane proti automatizovaným požiadavkam**, ktorá blokuje `patents.google.com`.
Zároveň XHR vracia aj metadáta, ktoré sa predtým márne škrabali z HTML
(`assignee`, `filing_date`, `grant_date`) — v reálnom behu boli všetky `Unknown`.

### 13.2 Implementácia: PDF-first stratégia
- `tools/patent_search.py`: `PatentCandidate` rozšírený o `pdf_url`, `assignee`,
  `filing_date`, `grant_date`; XHR parser ich zachytáva a `_json_response` posiela ďalej.
- `tools/patent_fetch.py`: `patent_fetch(url, timeout_ms, pdf_url="")` skúša
  **najprv oficiálne PDF** (plný text nárokov aj opisu vynálezu) a HTML stránku
  používa len ako zálohu. Nová funkcia `_pdf_fields()` zostaví výstup z textu PDF.
- `tools/patent_evidence_pack.py`: odovzdáva `pdf_url` a započítava `COVERAGE_TOKENS`
  do výpočtu `claim_coverage`.

### 13.3 Poctivé obmedzenie: dvojstĺpcová sadzba
Patentové PDF majú dvojstĺpcovú sadzbu a pri extrakcii sa riadky oboch stĺpcov
prekladajú (overené na reálnom dokumente — nárok 1 sa premiešal s textom vedľajšieho
stĺpca). Testoval som aj `pypdf` layout režim; ten síce zachováva pozície, ale šírky
riadkov sú natoľko nekonzistentné, že detekcia hranice stĺpcov by bola krehká.

Zvolené riešenie preto **nepredstiera**, že vie odcitovať doslovné znenie nároku:
- plný text ide do `COVERAGE_TOKENS` a slúži na **overenie pokrytia prvkov**, kde
  ide o výskyt termínov — tam je prekladanie stĺpcov úplne neškodné,
- polia `CLAIM1` a `ABSTRACT` zámerne nesú štandardné „nenájdené" sentinely, takže
  existujúce filtre ich vylúčia zo zobrazovaného súhrnu aj z výpočtu pokrytia
  (inak by meta-vety o PDF spôsobovali falošné zhody na slová ako „claims" či „layout"),
- na čitateľné zobrazenie sa naďalej používa čistý snippet a metadáta z XHR.

### 13.4 Nameraný efekt (live, rovnaký typ dotazu ako v produkčnom behu)
| Metrika | pred (v0.7.0) | po (v0.8.0) |
|---|---|---|
| Úroveň dôkazu patentov | 5× `search_snippet_only` | 3× `claim_verified`, 1× `abstract_verified` |
| `claim_coverage` | vždy 0 (nespustilo sa) | 0.25 / **1.0** / 0.5 |
| `exact_combination_candidate_found` | nikdy | **áno** (US10762444B2) |
| Extrahovaný text na patent | 0 slov | 7 706 slov (US10831585B2) |
| Čas jedného fetchu | 15 198 ms (blokovaný) | **737 ms** |

Zrýchlenie ~20× na operácii, ktorá v produkčnom behu dominovala celkovému času
(227 s), je vedľajší, ale významný efekt.

### 13.5 Overenie v0.8.0
- `python -m pytest` — **229 passed** (12 nových testov: detekcia sekcie nárokov,
  emisia coverage tokenov, uprednostnenie PDF pred HTML, tri fallback scenáre
  (prázdne PDF, výnimka, príliš krátky text), zachytenie `pdf` cesty a metadát
  z XHR, prechod `pdf_url` cez evidence pack, a osobitný regresný test overujúci,
  že meta-vety o PDF nikdy nekontaminujú pokrytie ani súhrn).
- Live overenia: dostupnosť storage bucketu, plná reťaz `patent_fetch` na patente,
  ktorý v produkčnom behu zlyhal, a kompletný `patent_evidence_pack` beh s
  atomickými požiadavkami.
- Tri existujúce testovacie stuby bolo treba aktualizovať na novú signatúru
  `patent_fetch` — zámerná zmena API, nie regresia.

## 14. Merateľné zlepšenie hodnotenia relevancie (v0.9.0)

Analýza reálneho behu ukázala, že z desiatich vrátených publikácií boli k téme
len dve. Namiesto odhadovania som najprv postavil **merací aparát**, potom
opravil príčiny a zmeny odmeral — čo zároveň napĺňa bod „Rozšíriť testovanie:
viac scenárov, opakované behy a merateľné metriky" z obhajoby.

### 14.1 Evaluačný harness — `eval/`
- `eval/dataset.json` — gold-standard dataset s troma scenármi. Kandidáti
  scenára `anomaly_logs_real_run` sú **doslovne prevzatí z reálneho behu**
  systému vo Flowise, takže meranie nie je na umelých dátach. Zvyšné dva
  scenáre zodpovedajú demonštračným príkladom z obhajoby a zámerne obsahujú
  dokumenty, ktoré zdieľajú generickú slovnú zásobu, ale patria inam.
- `eval/relevance_eval.py` — počíta presnosť, úplnosť, F1, P@k, MAP a MRR.
  Je offline nad uloženými kandidátmi, takže výsledky sú reprodukovateľné
  a nezávisia od toho, čo práve vrátia externí poskytovatelia.

Spustenie: `python -m eval.relevance_eval` (prepínač `--no-idf` zapne pôvodné
správanie na priame porovnanie).

### 14.2 Nameraná príčina: chybný stemmer
```
logs         -> logs        log          -> log            nikdy sa nezhodnú
application  -> applicate   applications -> application    nikdy sa nezhodnú
```
Strážna podmienka `len(token) <= 4` nechávala „logs" nezmenené a derivačné
pravidlo `ation → ate` sa uplatňovalo pred odstránením množného čísla. Dokument
o „log analysis" tak dostal voči dotazu s „logs" **nulový kredit** — chyba
zasahovala každý dotaz. Oprava: množné číslo sa odstraňuje ako prvé, strážna
dĺžka znížená na 3 a doplnená výnimka pre slová končiace na „ss" (aby
„process" neskončilo ako „proces").

### 14.3 Nameraná príčina: všetky termíny mali rovnakú váhu
Dotaz o anomáliách v logoch zdieľa so seizmológiou, ionosférou aj hasiacim
robotom generickú slovnú zásobu (`machine`, `learning`, `real`, `time`,
`detection`, `anomaly`). Pri rovnakých váhach to stačilo na prejdenie prahom —
robotický hasiaci systém dokonca skóroval **vyššie (3.95)** než obe skutočne
relevantné práce o logoch (3.53 a 3.01).

Riešenie: `build_corpus_idf()` počíta váhu termínov podľa ich vzácnosti
**v práve získanej množine kandidátov**. Zámerne nejde o pevný zoznam slov —
systém tak zostáva bez zabudovaných doménových slovníkov (v súlade s pôvodným
návrhom) a prispôsobí sa ľubovoľnej téme. Termíny prítomné takmer vo všetkých
kandidátoch stratia váhu, vzácne doménové (`logs`, `incident`, `administrator`)
ju získajú. Doplnená je aj doménová kotva `salient_query_tokens()`.

Keďže jednotliví poskytovatelia filtrujú každý zvlášť a v tej chvíli ešte nie
je známe, ktoré termíny sú rozlišujúce, prebieha váženie ako **druhý prechod
nad zlúčenou množinou** kandidátov (`_rerank_with_corpus_idf`) — s poistkou,
aby prísnejšie filtrovanie nikdy nevyprázdnilo celý zdroj. Rovnaký mechanizmus
je zapojený aj do webového vyhľadávania a do patentového rankingu.

### 14.4 Nameraný výsledok — offline dataset

> **Correction (v0.9.5).** The table originally published here reported precision
> 0.486 → 0.667 and F1 0.600 → 0.733, a +22 % F1 gain. Those numbers were measured
> at threshold **3.5**, which the harness hardcoded but which **no source type
> actually uses** — the server runs at 3.0 (patent, publication) and 2.8 (web).
> The figures below are measured at the production thresholds. The gain is real
> but much smaller than published, and it costs recall.

Measured at the production threshold for the dataset's source type (publication,
3.0), after anchor selection was made deterministic (section 14.8):

| Metrika | v1.0-thesis | súčasný stav | zmena |
|---|---|---|---|
| Presnosť | 0.519 | **0.611** | +18 % |
| Úplnosť | 1.000 | **0.833** | **−17 %** |
| F1 | 0.655 | **0.683** | +4 % |

**The recall regression is the important line.** The v1.0-thesis scorer accepted
every relevant document in the dataset; the current one drops one. IDF weighting
buys precision by discarding candidates, and on this dataset one of the discarded
candidates is relevant. Whether that trade is worth it cannot be decided here —
see the limitation below.

#### Threshold sweep

A single metric at a single threshold hides that trade-off entirely. Reproduce
with `python -m eval.relevance_eval --sweep`:

| prah | v1.0 P | v1.0 R | v1.0 F1 | teraz P | teraz R | teraz F1 | |
|---|---|---|---|---|---|---|---|
| 2.50 | 0.374 | 1.000 | 0.534 | 0.651 | 1.000 | 0.748 | |
| 2.80 | 0.463 | 1.000 | 0.610 | 0.611 | 0.833 | 0.683 | ← produkcia (web) |
| 3.00 | 0.519 | 1.000 | 0.655 | 0.611 | 0.833 | 0.683 | ← produkcia (patent, publication) |
| 3.20 | 0.486 | 0.833 | 0.600 | 0.611 | 0.833 | 0.683 | |
| 3.50 | 0.486 | 0.833 | 0.600 | 0.667 | 0.833 | 0.733 | ← pôvodne publikované |
| 4.00 | 0.556 | 0.667 | 0.600 | 0.556 | 0.667 | 0.600 | |
| 4.50 | 0.556 | 0.667 | 0.600 | 0.556 | 0.500 | 0.489 | |

The published +22 % was the single best cell in this table. At 4.00 the two
scorers are identical; at 4.50 the current one is **worse**.

#### Reprodukovanie pôvodného skórovania (v1.0-thesis)

`--no-idf` disables IDF weighting but **keeps the stemmer fix**, so it is not the
original scorer — it produces a third set of numbers (0.431 / 0.556 at 3.5)
matching neither column above. The help text used to claim otherwise; it has been
corrected. To measure the true v1.0-thesis behaviour, inject the historical scorer
through the `score_fn` / `accept_fn` parameters the harness already exposes:

```bash
git show v1.0-thesis:tools/relevance.py > /tmp/relevance_v1.py
sed -i 's/^from \.result_contract/from tools.result_contract/' /tmp/relevance_v1.py
```

```python
import sys; sys.path.insert(0, "/tmp")
import relevance_v1 as old
from eval.relevance_eval import evaluate_dataset

_results, summary = evaluate_dataset(
    score_fn=old.evidence_score, accept_fn=old.is_relevant, use_idf=False
)
```

#### Prečo sú tieto čísla slabý dôkaz

The dataset is **3 queries, 20 candidates, 6 relevant documents**, and only one
query (`anomaly_logs_real_run`) comes from a real run — the other two were
constructed by the author, which means the negatives were chosen by the same
person who wrote the scorer. A difference of 0.028 in F1 across 3 queries is not
a measurable improvement; it is one document changing side. **No claim of the
form "+X % better" is supportable at this dataset size**, and the numbers above
should be read as a smoke test that the component is not broken, not as evidence
that it is good. Expanding the dataset is the prerequisite for any further tuning
work, and production thresholds are deliberately left unchanged until then.

Scenár z reálneho behu samostatne: presnosť 0.12 → **0.33**, F1 0.20 → **0.40**
(prijatých kandidátov 8 → 3, z toho relevantných stále 1 z 2).

### 14.5 Nameraný výsledok — živý beh na pôvodnom dotaze
| | pôvodný beh | po zmene |
|---|---|---|
| Seizmika, ionosféra, GNSS, hasiaci robot, sociálne siete | 5 výsledkov | **0** |
| Práce priamo o logoch | 2 | **3** |
| Odfiltrovaných kandidátov | – | **19** |

Nový nález, ktorý sa predtým nedostal do výsledkov: *„Detecting Anomalies in
Logs by Combining NLP features with Embedding or TF-IDF"*. Zvyšné výsledky sa
posunuli z úplne cudzích domén na príbuznú bezpečnostnú (IDS, threat hunting).

### 14.6 Poctivo priznaný zostávajúci limit
Metrika **P@2 v scenári z reálneho behu zostáva 0.00**. Dôvod je principiálny:
dokument o detekcii anomálií v *sociálnych sieťach* obsahuje takmer celú slovnú
zásobu dotazu (`anomalies`, `patterns`, `machine learning`, `real-time`,
`automated incident response`) a od dotazu o *aplikačných logoch* sa líši
predmetom, nie slovami. **Žiadna čisto lexikálna metóda toto spoľahlivo
nerozlíši** — potrebné je sémantické porozumenie (vektorové reprezentácie).
Harness je na to pripravený: stačí doplniť skórovaciu funkciu a zmerať rovnakým
spôsobom.

### 14.7 Overenie v0.9.0
- `python -m pytest` — **243 passed** (14 nových testov: zhoda jednotného
  a množného čísla, ochrana slov na „ss", výpočet a účinok IDF, doménová kotva,
  kontrola integrity datasetu a dva **regresné testy kvality**, ktoré zlyhajú,
  ak presnosť klesne pod nameranú úroveň alebo ak IDF prestane byť lepšie než
  pôvodné rovnaké váhy).
- Živý beh publikačného vyhľadávania na pôvodnom dotaze (tabuľka 14.5).

### 14.8 Nedeterminizmus výberu doménových kotiev (v0.9.5)

Aligning the harness with the production thresholds immediately exposed a latent
bug that the wrong threshold had been hiding: **the relevance filter was not
deterministic**. Forty identical runs of `python -m eval.relevance_eval` in
separate processes produced three different results:

```
29 runs   precision 0.611   recall 0.833   F1 0.683
11 runs   precision 0.556   recall 0.833   F1 0.639
 (rarer)  precision 0.500   recall 0.667   F1 0.550   <- a relevant document rejected
```

Príčina — `tools/relevance.py`, `salient_query_tokens`:

```python
candidates = discriminative_tokens(query) or tokens(query)   # a set
ranked = sorted(candidates, key=lambda token: idf.get(token, _DEFAULT_IDF_WEIGHT), reverse=True)
return set(ranked[: max(1, top_n)])
```

`sorted` is stable, so tokens with equal IDF keep their input order — the
iteration order of a **set**, which depends on Python's per-process string hash
randomisation. IDF ties are common by construction: any two tokens appearing in
the same number of candidate documents get exactly the same weight. The first
dataset query alone produces five distinct tie groups among 17 candidate tokens.
When a tie group straddles the `top_n = 6` cut, which anchors survive changes
between runs, and `is_relevant` rejects any document sharing none of them.

This was a **production** defect, not only an evaluation one: the same query
submitted to the running server could return different documents after a restart.

**Oprava:** a deterministic secondary sort key.

```python
ranked = sorted(candidates, key=lambda token: (-idf.get(token, _DEFAULT_IDF_WEIGHT), token))
```

The token itself carries no semantic meaning as a tie-break; it only has to be
stable. `tools/patent_search.py` already used the same pattern
(`key=lambda item: (-item.score, item.patent_number)`).

Deliberately **not** done: widening `top_n` to keep every token tied at the cut
boundary. That is arguably more principled — splitting two equally salient tokens
is unjustifiable — but it changes filter semantics, and with 20 candidates there
is no way to measure whether it helps. Deferred until the dataset is larger.

Why the bug survived this long: at threshold 3.5 the acceptance decisions happen
to land identically regardless of which anchors are chosen, so the old regression
guard passed on every run. Only at the production thresholds do the outcomes
diverge. **The wrong measurement configuration was masking the defect.**

### 14.9 Overenie v0.9.5

- `python -m pytest` — **276 passed**, six consecutive runs, no flakes. Before the
  fix the re-pinned guard failed 2 runs in 6.
- 25 independent processes of `python -m eval.relevance_eval` now return an
  identical result; `--sweep` output is byte-identical across 5 processes
  (verified by `md5sum`).
- Anchor selection is identical under `PYTHONHASHSEED` values 0, 1, 7, 42, 12345.
- New tests: deterministic tie-break on an all-tied candidate set; anchor
  stability under 20 shuffles of the query word order; and a reproducibility test
  that runs the evaluation in **subprocesses**, since `PYTHONHASHSEED` is fixed
  for the life of a process and a single-process loop cannot detect this bug.
- The harness now imports `THRESHOLDS` from `tools.relevance` instead of keeping
  its own constant, and a drift test asserts the two cannot disagree again.

## 15. Čistota textu vo výslednom reporte (v0.9.1)

Pri opätovnom čítaní reportu z reálneho behu sa ukázalo, že do výstupu
prechádza balast, ktorý nemá dôkaznú hodnotu a kazí dojem z výsledku.
Overením v kóde sa potvrdilo, že **žiadny z týchto prípadov sa nečistil**.

| Defekt v reporte | Príklad z reálneho behu |
|---|---|
| Nerozkódované HTML entity | `Log Intelligence &amp; SIEM Platform` |
| Markdown hlavička z čítačky README | `# Repository: PhilipLykov/LogPulseAI` |
| Metadáta repozitára | `- Stars: 0 - Forks: 0 - Watchers:` |
| Navigácia a marketingové výzvy | `is available now. Click here to check it out.` |
| Zdvojený výraz z názvu stránky | `Rootly \| Rootly Anomaly Scoring Engine` |
| Zopakované slovo | `Product Product` |
| Názov zopakovaný na začiatku súhrnu | nález uvedený dvakrát za sebou |

### 15.1 Riešenie
Do `tools/output_cleaner.py` pribudli štyri opakovane použiteľné funkcie:
`unescape_entities()` (rozkóduje aj viacnásobné zakódovanie `&amp;amp;`),
`collapse_repeats()`, `strip_boilerplate()` a `strip_leading_title()`.
Zapojené sú vo vykresľovacej vrstve `tools/user_answer.py`, takže sa
uplatnia bez ohľadu na to, z ktorého zdroja text pochádza, a **nemenia
uložené dôkazy** — iba ich prezentáciu.

### 15.2 Súhrny bez dôkaznej hodnoty
Po očistení balastu môže zo stránky zostať čitateľný, ale s dotazom
nesúvisiaci text (v reálnom behu napríklad *„Top People Making the World
More Reliable"*). Taký súhrn je v reporte horší než žiadny, preto sa
zobrazí len vtedy, ak obsahuje aspoň jeden termín z dotazu. Samotný nález
vrátane URL a úrovne dôkazu zostáva zobrazený.

### 15.3 Overenie v0.9.1
`python -m pytest` — **258 passed** (15 nových testov, z toho dva pracujú
priamo s doslovnými reťazcami z reálneho reportu a jeden overuje, že nález
s bezobsažným súhrnom sa zobrazí, ale samotný súhrn nie).

## 16. Kvalita patentových výsledkov pri zablokovanom provideri (v0.9.2)

Pri prvom kompletnom behu systému mimo Flowise (priame volanie MCP nástrojov,
bez akýchkoľvek API kľúčov) sa vo výstupe objavili tri patenty s názvom
*„Method, system and computer program for comparing images"* — teda porovnávanie
obrázkov ako odpoveď na dotaz o detekcii anomálií v logoch.

### 16.1 Príčina: dvojité zlyhanie
Bezkľúčový Google Patents provider z v0.4.0 vrátil pri tomto behu **HTTP 503 pre
každý dotaz** — tá istá ochrana proti automatizovaným požiadavkam, akú rieši
sekcia 12. Provider teda nie je nefunkčný, ale ani spoľahlivý: raz prejde, inokedy
je zablokovaný. Vyhľadávanie preto spadlo na scraping WIPO PATENTSCOPE, ktorý
odhalil dva samostatné defekty.

### 16.2 Klasifikačný balast ako dôkazový text
Ako „snippet" sa posielal **celý riadok výsledkovej tabuľky**, vrátane rozpisu
medzinárodného patentového triedenia:

```
Int.Class G06K 9/00 G PHYSICS 06 COMPUTING; CALCULATING OR COUNTING K GRAPHICAL
DATA READING; ... 9 Methods or arrangements for recognising patterns
```

Riadok tabuľky vôbec neobsahuje abstrakt, takže tento text nebol dôkazom, ale
metadátami. Navyše obsahuje všeobecné technické slová (*recognising patterns*,
*computing*, *data*), ktoré spôsobovali **falošné zhody s ľubovoľným technickým
dotazom** — presne tak sa porovnávanie obrázkov dostalo k dotazu o logoch.

**Oprava:** `_wipo_snippet()` odstraňuje klasifikáciu, formulárové polia,
poradové číslo riadku aj dátum. Ak po očistení nezostane aspoň päť vecných slov,
snippet sa nevytvorí vôbec a relevancia sa hodnotí len podľa názvu patentu.

### 16.3 Núdzový režim vracal čokoľvek
Keď žiaden kandidát neprešiel prahom relevancie, `_rank(allow_low_confidence=True)`
vrátil **všetkých** kandidátov vrátane skóre 0.0 — v reálnom behu napríklad
*„PHOTOELECTRIC CONVERSION DEVICE"*.

**Oprava:** aj núdzový režim má spodnú hranicu `MIN_RELEVANCE_FLOOR`. Radšej sa
nevráti nič a stav sa čestne označí za neúplný, než aby report obsahoval zjavne
nesúvisiace patenty.

### 16.4 Overenie v0.9.2
- `python -m pytest` — **266 passed** (8 nových testov postavených na doslovnom
  riadku z reálneho behu: odstránenie klasifikácie, zachovanie vecného obsahu,
  pokles skóre voči nesúvisiacemu dotazu, spodná hranica v núdzovom režime
  vrátane prípadu, keď je správne nevrátiť nič).
- Overené na skutočnom dáte: skóre očisteného snippetu voči dotazu o logoch kleslo.

## 17. Kvalita patentových nálezov overená celým behom (v0.9.4)

Prvý kompletný beh systému mimo Flowise odhalil tri ďalšie defekty, ktoré sa
prejavia až vtedy, keď je hlavný patentový provider zablokovaný.

### 17.1 Patenty nemali doménovú kotvu
Publikačná vetva vyžaduje, aby nález zdieľal s dotazom predmetový termín;
patentová takú podmienku nemala. Patent o **porovnávaní obrázkov** tak dosiahol
skóre **3.58** voči dotazu o anomáliách v logoch a prešiel aj cez hlavný prah 2.8,
lebo ich spájala len všeobecná technická slovná zásoba (*method*, *system*, *device*).

Váženie vzácnosťou termínov z v0.9.0 to nezachytilo, a to zo štrukturálneho dôvodu:
všetci kandidáti pochádzali z **jednej patentovej rodiny**, takže každý termín mal
rovnakú dokumentovú frekvenciu a IDF nemalo čo rozlíšiť.

**Oprava:** `_shares_discriminative_term()` sa uplatňuje na všetkých kandidátov vo
všetkých vetvách hodnotenia. Po zmene sa na prvé miesta dostali skutočne súvisiace
patenty (statická analýza kódu, detekcia anomálií v sieťových operáciách).

### 17.2 Zlyhané overenie mazalo už získaný dôkaz
Keď `patent_fetch` zlyhal alebo bol zablokovaný, úroveň dôkazu sa prepísala na
`fetch_failed` a vykresľovanie taký nález skrylo. Výsledok bol prevrátený: patenty,
ktoré sa systém **pokúsil overiť, z reportu zmizli**, zatiaľ čo kandidát mimo
limitu na overovanie v ňom zostal.

**Oprava:** ak kandidát nesie použiteľný vyhľadávací úryvok, po neúspešnom
overení sa vráti na `search_snippet_only` namiesto zlyhanej úrovne. Publikačná
vetva sa takto správala už predtým.

### 17.3 Jedna prihláška zaberala celú sekciu
Deduplikácia porovnávala len publikačné číslo, takže ten istý vynález podaný pod
štyrmi číslami (národné, medzinárodné, pokračovania) vyplnil štyri zo šiestich
miest. **Oprava:** deduplikácia aj podľa normalizovaného názvu.

### 17.4 Overenie v0.9.4
- `python -m pytest` — **270 passed** (4 nové testy: kotva v hlavnej vetve,
  zachovanie skutočne súvisiacich patentov, zlúčenie rodiny, ponechanie
  odlišných krátkych názvov).
- Opakovaný beh celého workflow po každej oprave; výsledný report je uložený
  ako `docs/example-report.md`.

## 18. Audit závislostí na poradí iterácie (v0.9.6)

After the v0.9.5 nondeterminism fix, the whole codebase was audited for other
places where the iteration order of an unordered container could reach
externally observable behaviour. **Result: no further correctness-affecting
dependency was found.** This section records what was checked and how, so the
negative result is verifiable rather than asserted.

### 18.1 Metóda

*Static.* An AST pass over `tools/*.py` and `server*.py` — not grep, which misses
comprehensions and chained calls — flagged 379 candidate sites across `sorted`,
`min`/`max`, `next`, `join`, and slicing. Each was then traced by hand to
determine whether an unordered value can actually reach it.

*Dynamic.* Two workloads were digested and re-run in a fresh interpreter per
`PYTHONHASHSEED`, because the seed is fixed for the life of a process and a
single-process loop cannot detect this class of bug at all.

**The first dynamic probe was wrong, and that matters.** A probe driving the full
workflow through `research_session_save_evidence` produced identical output
across 12 seeds — but so did it with the v0.9.5 bug deliberately reintroduced.
Injecting pre-built evidence packs bypasses retrieval and filtering entirely, so
the probe never reached the code the bug lived in. A second probe targeting the
filtering path directly was then validated the same way, and with the bug
reintroduced it produced **12 different digests for 12 seeds**.

Both probes are now permanent tests (`tests/test_determinism.py`). Every
differential check of this kind must be validated against a known bug before its
passing result means anything.

### 18.2 Čo bolo nájdené

| Site | Verdict |
|---|---|
| `relevance.py` `salient_query_tokens` | Fixed in v0.9.5. |
| `patent_search.py:482` `sorted(ranked, key=(-score, patent_number))` | Safe — explicit unique tie-break. |
| `patent_search.py` `_dedupe` | Safe — sets used for membership tests only; output order follows the input list. |
| `_hit_sort.py` `sort_hits_by_relevance` | Tie-prone `(rank, score)` key, but input is a list and the sort is stable. Deterministic. |
| `evidence_quality.py:107` `scored_hits[0]` | Tie-prone — decides which hit is "top" and reaches the output. Input is a list, so deterministic. |
| `publications_search.py:757` `rejected.sort(key=score)` | Tie-prone — decides which rejected candidates are restored. Input is a list. Deterministic. |
| `web_search.py:728` `sorted(merged_results.values(), key=(-score, _rank_domain(url)))` | `_rank_domain` returns `(int, domain)`, so ties survive only for equal score *and* equal domain, falling back to dict insertion order. Deterministic, but the narrowest margin in the codebase. |
| `query_expansion.py:63` `sorted(_MAPPING, key=-len)` | Heavy ties, but `_MAPPING` is built from a tuple and a dict literal with `tuple(sorted(...))` values, so insertion order is deterministic. |
| `research_session.py:905` `sorted(other_atoms, key=binary)` | Binary key, near-total ties, list input, stable sort. Deterministic. |
| 9 keyless `sorted()` calls over sets | Safe by construction — alphabetical or numeric order. |
| SQL view `deduped_best_evidence_view` | Safe — `ORDER BY verified_url DESC, relevance_score DESC, id ASC` ends in a unique column. |

No occurrence of `list(set(...))` or `tuple(set(...))` exists anywhere in the
codebase, and no `next(iter(...))` over an unordered container.

The recurring reason nothing else broke is that this codebase already sorts sets
*without* a key in the places it converts them to output, which yields
alphabetical order. `salient_query_tokens` was the outlier precisely because it
needed a ranking key, and that is where the secondary key was forgotten.

### 18.3 Vedome nezmenené

`web_search.py:728` and `evidence_quality.py:107` are deterministic today but rely
on the caller passing an ordered container. Adding a final unique tie-break
(`url`, `canonical_id`) would make them robust rather than merely correct — but
it would change the current output ordering, which is a behavioural change and
was therefore not made as part of an audit. Recorded here as a candidate.

### 18.4 Overenie

- `python -m pytest` — **278 passed**, unchanged across `PYTHONHASHSEED` values
  0, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144.
- Both digests identical across 20 seeds.
- Negative control: reverting the v0.9.5 fix makes
  `test_filtering_path_is_hash_order_independent` fail with 8 distinct digests.
- The guard costs about 9 s (16 subprocess spawns); the suite runs in ~13 s.

## 19. Námety na ďalšie zlepšenia (nezaradené)

- Znovupoužitie jednej Playwright browser inštancie namiesto spúšťania novej pre každý fetch.
- Perzistentná (SQLite) cache pre patent_fetch medzi reštartmi kontajnera.
- Rozšírenie `_MDPI_ISSN_TO_CODE` alebo generické DOI resolvovanie cez Crossref pre viac vydavateľov.
- Meranie latencie per-provider a export metrík (napr. Prometheus endpoint) pre kapitolu o vyhodnotení.
- Extrakcia textu z DOCX/PPTX príloh podobne ako pri PDF.
- Espacenet OPS API (bezplatné s registráciou) ako ďalší štruktúrovaný patentový zdroj.
- **Sémantický reranking vektorovými reprezentáciami** — jediný spôsob, ako
  rozlíšiť dokumenty s takmer zhodnou slovnou zásobou, ale iným predmetom
  (limit z bodu 14.6). Merateľné existujúcim harnessom.
- Ak by sa blokovanie Google Patents opakovalo aj po znížení súbežnosti, zvážiť
  Playwright s perzistentným prehliadačovým kontextom (cookies/fingerprint)
  namiesto novej anonymnej relácie pre každý fetch — bloky bývajú citlivé aj
  na "čerstvosť" fingerprintu, nielen na objem požiadaviek.
- Využiť ďalšie AlphaXiv MCP nástroje (`get_paper_content`, `answer_pdf_queries`) na
  hĺbkovú analýzu konkrétnych AlphaXiv/arXiv článkov nad rámec `discover_papers`.
