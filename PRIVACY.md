# Zásady ochrany soukromí – Home Assistant: Darování

Tato aplikace je soukromá integrace pro Home Assistant, kterou používá jen její autor pro správu vlastních rezervací darování krve a plazmy ve FN u sv. Anny v Brně.

- **Google Kalendář** (`calendar.events`): aplikace vytváří, upravuje a maže pouze události darování, které sama spravuje.
- **Gmail** (`gmail.modify`): aplikace čte pouze e-maily od `nis-noreply@fnusa.cz` (potvrzení a storna rezervací), ukládá z nich termín a odkaz pro správu rezervace a zpracované e-maily přesouvá do zvoleného štítku.
- **Gmail – odesílání** (`gmail.send`): aplikace odesílá pouze pozvánky do pracovního kalendáře na adresu, kterou uživatel sám nastaví.
- Data zůstávají v instanci Home Assistant uživatele. Nikam jinam se neodesílají, neprodávají ani nesdílejí. Výjimkou jsou údaje dárce, které se při rezervaci posílají rezervačnímu systému FNUSA.
- Přístup lze kdykoliv odvolat na https://myaccount.google.com/permissions.

Kontakt: jakub.kautsky1@gmail.com
