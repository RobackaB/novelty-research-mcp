# Novelty Research MCP

Python MCP backend na predbežný prieskum stavu techniky z patentových,
publikačných a webových zdrojov. SQLite uchováva pokusy o vyhľadávanie a dôkazy;
Python zabezpečuje hodnotenie relevancie, spracovanie dôkazov a zostavenie správy.
Flowise koordinuje volania nástrojov pomocou jazykového modelu.

Projekt vznikol ako bakalárska práca **AI for Advanced Information Research**.
Tag `v1.0-thesis` zachováva odovzdaný prototyp. Verzia **0.10.0** je portfóliový
míľnik po technickom audite, nie tvrdenie o pripravenosti na produkčné nasadenie.

[English README](README.md) · [Ukážka správy](docs/example-report.md) ·
[Technický audit](AUDIT.md) · [Overenie míľnika](docs/portfolio-milestone.md)

## Čo pribudlo po práci

- Deterministické rozhodovanie pri rovnakých skóre, regresné testy a negatívne kontroly.
- Pasívny záznam rozhodnutí o kandidátoch a pôvodu dotazu bez vplyvu na vyhľadávanie.
- Ochrana diagnostiky poskytovateľov pred kopírovaním prihlasovacích údajov.
- Syntetická offline infraštruktúra Goal 5C: import SQLite snapshotu iba na čítanie,
  explicitné zosúladenie identít, prázdne zaslepené hárky a deterministický výber dotazov.

Backend poskytuje sedem MCP nástrojov. Ich rozhrania kontrolujú testy; aktuálny
základ má **954 testov** a CI pre Python 3.11–3.13. Determinizmus sa vzťahuje na
rovnaké vstupy a konfiguráciu, nie na meniace sa výsledky externých služieb.

## Najprv offline overenie

Nie sú potrebné API kľúče, Docker ani účet jazykového modelu. Inštalácia závislostí
vyžaduje internet. V PowerShelli:

```powershell
git clone https://github.com/RobackaB/novelty-research-mcp.git
cd novelty-research-mcp
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
python -m eval.relevance_eval
python -m eval.goal5c --help
```

Na Linuxe/macOS aktivujte prostredie cez `source .venv/bin/activate`.
Príkazy `eval` spúšťajte z koreňa repozitára: nie sú súčasťou serverového wheel
balíka ani runtime Docker obrazu. Goal 5C používa syntetické vstupy; príklady sú
v [dokumentácii](docs/goal5c-offline-foundation.md) a testoch. Generované dáta
nepatria do Gitu.

## Meranie a hranice

Historický benchmark obsahuje **3 dotazy a 20 kandidátov**. Ide o regresné meranie
generického skórovania na pevnej množine kandidátov, nie o meranie celého workflow.
Makro priemery po dotazoch sú precision **0.611**, recall **0.833**, F1 **0.683**.
Taká malá, čiastočne autorom zostavená vzorka nepreukazuje všeobecnú účinnosť,
percentuálne zlepšenie ani globálnu úplnosť vyhľadávania.

Goal 5C fázy 1–3 sú implementované iba ako syntetická infraštruktúra. Skutočný pilot,
25–30 pôvodných informačných potrieb, ľudské označovanie/adjudikácia, metriky workflow
a prospektívne potvrdenie prahov zostávajú budúcim výskumom. Fáza 4 sa nezačala.

Systém pomáha s prieskumom; nedokazuje novosť ani patentovateľnosť. Chýbajúci výsledok
pri blokovaní poskytovateľa alebo zlyhaní sťahovania neznamená neexistenciu riešenia.

## Voliteľné lokálne demo

Použite Docker Desktop a Flowise **3.1.4** pripnutý v Compose. Spustenie kontajnerov
a interaktívny import sa na overovacom počítači nepodarilo overiť; podrobnosti sú
v [zázname míľnika](docs/portfolio-milestone.md#verification).
Pre živé volania modelu nastavte vlastný OpenAI credential vo Flowise.
Voliteľné kľúče poskytovateľov sú popísané v `.env.example`.

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Otvorte `http://localhost:3000`, vytvorte/otvorte **Agentflow V2** a cez nastavenia (Load Agents) importujte
`flowise_architecture/Flowise_agent.json`. Nastavte model a credential.
Pre služby v Compose nastavte URL Custom MCP na
`http://mcp-research-server:8000/mcp`; historický export používa `host.docker.internal`.
Podrobný postup a riešenie problémov sú v [anglickom README](README.md#optional-local-flowise-demo).

Ide o dôveryhodné lokálne demo. MCP server nemá autentifikáciu verejnej služby;
kontroly host/origin ju nenahrádzajú. Nevystavujte služby priamo internetu a
nepoužívajte citlivé vstupy. Dotazy a dôkazy sa ukladajú, niektoré URL sa logujú.

`docker compose down` ponechá dáta vo volumes `flowise_data` a `mcp_research_data`.
`docker compose down -v` ich odstráni. Stav a logy skontrolujete cez
`docker compose ps` a `docker compose logs`.
