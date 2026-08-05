# Flowise MCP Research Server

Tento balík obsahuje finálny prototyp MCP servera a exportovanú Flowise architektúru použitú v práci. Systém slúži na predbežný prieskum stavu techniky z patentových, publikačných a webových zdrojov.

*[English README](README.md)*

> **O tomto repozitári**
>
> Tag `v1.0-thesis` označuje kód presne v tom stave, v akom bol odovzdaný ako
> bakalárska práca — bez akýchkoľvek dodatočných úprav. Ďalšie commity sú
> vylepšenia, ktoré na tomto základe postupne pribúdajú (opravy chýb, testovacia
> sada, meranie kvality výstupu). Vývoj tak zostáva dohľadateľný od pôvodnej
> odovzdanej verzie.


## Čo je v balíku

- `server_http.py` - HTTP vstup pre MCP server používaný pri Docker spustení.
- `server.py` - registrácia MCP nástrojov.
- `terminal_ui.py` - terminálový štartovací banner pre HTTP server.
- `tools/` - implementácia vyhľadávacích, ukladacích, overovacích a hodnotiacich nástrojov.
- `docker-compose.yml` - spoločné spustenie Flowise a MCP servera.
- `Dockerfile` - obraz MCP servera.
- `.env.example` - vzor konfiguračného súboru bez tajných kľúčov.
- `flowise_architecture/Flowise_agent.json` - finálna Flowise architektúra.
- `flowise_baselines/` - jednoduchšie RAG architektúry použité iba na porovnanie.

Na beh finálneho systému je potrebný hlavne súbor `flowise_architecture/Flowise_agent.json`. Súbory v `flowise_baselines/` nie sú potrebné na spustenie finálnej verzie.

## Požiadavky

Pred spustením potrebujete:

- nainštalovaný a spustený Docker Desktop,
- vlastný OpenAI API kľúč nastavený vo Flowise po importe architektúry,
- vlastné API kľúče v súbore `.env`, ak chcete plnohodnotné vyhľadávanie.

Odporúčané API kľúče:

- `GOOGLE_CSE_API_KEY`
- `GOOGLE_CSE_ID`
- `TAVILY_API_KEY`
- `EXA_API_KEY`
- `SEMANTIC_SCHOLAR_API_KEY`

`GOOGLE_CSE_API_KEY` a `GOOGLE_CSE_ID` sú dôležité pre primárne webové vyhľadávanie. `TAVILY_API_KEY` a `EXA_API_KEY` zlepšujú webové a patentové fallback vyhľadávanie. `SEMANTIC_SCHOLAR_API_KEY` zlepšuje limity pri vyhľadávaní odborných publikácií.

## Rýchle spustenie

1. Otvorte terminál v priečinku projektu.

2. Vytvorte lokálny konfiguračný súbor:

```powershell
Copy-Item .env.example .env
```

3. Otvorte `.env` a doplňte vlastné API kľúče.

4. Spustite služby:

```powershell
docker compose up --build
```

Po úspešnom spustení budú dostupné:

- Flowise: `http://localhost:3000`
- MCP server: `http://localhost:8000/mcp`

Pri štarte MCP server vypíše do terminálu aj adresu pre Flowise Custom MCP konfiguráciu.

## Import Flowise architektúry

1. Otvorte Flowise na adrese:

```text
http://localhost:3000
```

2. Importujte súbor:

```text
flowise_architecture/Flowise_agent.json
```

3. Ak Flowise po importe vyžaduje credentials, nastavte vlastný OpenAI API kľúč pre použitý jazykový model.

4. Skontrolujte Custom MCP konfiguráciu. Exportovaná architektúra používa adresu:

```text
http://host.docker.internal:8000/mcp
```

Táto adresa je vhodná pri spustení cez Docker Desktop, pretože Flowise kontajner sa cez ňu pripája na MCP server vystavený na porte `8000`.

## Testovací dotaz

Po importe architektúry zadajte vlastný opis riešenia. Dotaz môže byť po slovensky alebo po anglicky.

```text
Over, či už existuje [stručný opis riešenia, jeho účelu, technických prvkov, spôsobu fungovania a výsledku, ktorý má dosiahnuť].
```

Pri správnom nastavení by mal workflow postupne volať najmä tieto MCP nástroje:

- `research_session_start`
- `research_session_understand_query`
- `patent_evidence_to_session`
- `publication_evidence_to_session`
- `web_evidence_to_session`
- `research_session_checklist`
- `research_session_user_answer`

## Overenie spustenia

Ak Flowise nevracia odpoveď alebo workflow hlási chybu:

- skontrolujte, že bežia oba kontajnery,
- skontrolujte, že MCP server je dostupný na `http://localhost:8000/mcp`,
- skontrolujte, že Custom MCP konfigurácia vo Flowise používa `http://host.docker.internal:8000/mcp`,
- skontrolujte, že v `.env` sú doplnené API kľúče,
- skontrolujte, že vo Flowise je nastavený OpenAI credential.

Stav kontajnerov zobrazíte príkazom:

```powershell
docker compose ps
```

Logy zobrazíte príkazom:

```powershell
docker compose logs -f
```

## Zastavenie

Kontajnery zastavíte príkazom:

```powershell
docker compose down
```

Ak chcete vymazať aj uložené dáta Flowise a SQLite databázu z testovania:

```powershell
docker compose down -v
```

## Poznámky k dátam

Flowise dáta sú uložené v Docker volume `flowise_data`. SQLite databáza prieskumov je uložená v Docker volume `mcp_research_data` na ceste `/app/data/research_sessions.sqlite3`.

Príkaz `docker compose down` dáta ponechá. Príkaz `docker compose down -v` tieto volumes odstráni.