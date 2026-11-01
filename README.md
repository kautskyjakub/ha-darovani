# Darování krve a plazmy – FN u sv. Anny v Brně

Integrace pro Home Assistant, která hlídá volné termíny na transfuzním oddělení **FN u sv. Anny (FNUSA)**, plánuje další odběry podle tvých pravidel a rezervuje, ruší i přesouvá termíny. Všechno se potvrzuje kódem z SMS přímo v notifikaci na mobilu.

## Co umí

- **Plán na 3 měsíce dopředu.** Krev → za 30 dní plazma → plazma každé 3 týdny → krev nejdřív za 3 měsíce a zároveň aspoň 3 týdny po poslední plazmě. Všechny odstupy se dají změnit.
- **Notifikace s nabídkou termínů.** Jakmile nemocnice vypíše termíny pro další krok, přijde jedna notifikace se třemi návrhy (ranní časy mají přednost), odkazem na všechny termíny a volbou „Připomenout zítra“. Další notifikace přijde jen tehdy, když se uvolní dřívější termín.
- **Rezervace kódem z SMS.** Ťukneš na termín, přijde SMS, kód napíšeš do notifikace a je hotovo. Během platnosti kódu (pár minut) jde rovnou zamluvit i další krok.
- **Rušení bez otevírání mailu.** Odkaz pro správu se vyčte z potvrzovacího e-mailu a uloží se k rezervaci i do popisu události v kalendáři. Rušení se pak potvrzuje kódem z SMS.
- **Odklad (např. opar).** Zvolíš počet dní a karta ukáže, co se posune a na jaký termín. Po potvrzení modul zruší termíny v době odkladu a zamluví náhrady.
- **Google kalendář.** Události mají stejný formát jako dosud: *🩸 Darování krve / plazmy*, místo FN u sv. Anny, připomínka den předem a v čase odběru. Integrace je drží v souladu s rezervacemi, včetně storen provedených mimo Home Assistant.
- **Karta na dashboard** `custom:darovani-card` s dalším odběrem, výběrem termínu, zadáním kódu, plánem, odkladem a historií.
- **Entity:** kalendář `calendar.darovani` (včetně výhledu plánu) a senzory *Další odběr*, *Stav*, *Další krok*, *Poslední krev*, *Poslední plazma* a *Počet odběrů*.

## Instalace

1. HACS → ⋮ → **Vlastní repozitáře** → `https://github.com/kautskyjakub/ha-darovani`, typ *Integration*.
2. Stáhni **Darování krve a plazmy (FNUSA)** a restartuj Home Assistant.

## Propojení s Googlem (Kalendář + Gmail)

Integrace potřebuje vlastního OAuth klienta Google. Nastavuje se jednou:

1. [Google Cloud Console](https://console.cloud.google.com/) → nový projekt (např. *Home Assistant*).
2. **APIs & Services → Library**: zapni **Google Calendar API** a **Gmail API**.
3. **OAuth consent screen**: typ *External*, vyplň název a svůj e-mail. Pak dej **Publish app**. Bez publikování Google přihlášení po 7 dnech zneplatní. Neověřená aplikace pro vlastní účet stačí, při přihlášení jen projdeš varováním.
4. **Credentials → Create credentials → OAuth client ID**: typ *Web application*, autorizované přesměrování `https://my.home-assistant.io/redirect/oauth`.
5. V HA: Nastavení → Zařízení a služby → ⋮ → **Přihlašovací údaje aplikace** → *Darování krve a plazmy* → vlož Client ID a Client secret.
6. Přidej integraci **Darování krve a plazmy**, přihlas se Googlem a vyplň údaje dárce (stejné jako ve formuláři FNUSA).
7. V **Možnostech** integrace vyber, na který telefon chodí notifikace.

Rozsahy oprávnění: `calendar.events` (zápis událostí odběru) a `gmail.readonly` (čte se jen pošta od `nis-noreply@fnusa.cz`).

## Karta

```yaml
type: custom:darovani-card
```

## Soukromí a přístup

**Modul je jen pro jednoho člověka.** Kartu, služby i akce z notifikací smí použít jen povolení uživatelé HA. Výchozí je vlastník telefonu, na který chodí notifikace; seznam se dá upravit v Možnostech. Ostatní uživatelé (i administrátoři) na kartě uvidí jen zámek. Služby od nich nebo od automatizací skončí chybou *Unauthorized* a akce z cizího telefonu se ignorují.

Gmail a Google kalendář čte jen integrace pod tvým Google účtem. Obsah e-mailů se nikam v HA nezobrazuje a odkazy pro správu rezervace jsou jen na kartě a v tvém Google kalendáři, ne ve stavech entit. Entity mají jen data a stav, jsou skryté z automatických přehledů a stavy entit jsou v HA technicky vidět všem uživatelům.

Údaje dárce zůstávají v Home Assistantu a odesílají se jen do rezervačního systému FNUSA při rezervaci. Odkazy pro správu rezervace jsou osobní (kdo je má, může s rezervací manipulovat) a ukládají se jen do tvého HA a tvého kalendáře.

## Jak funguje rezervační systém FNUSA

| Krok | Volání |
|---|---|
| Volné termíny | `GET /api/AvailableTerms/NIS/162/2` (plazma), `…/162/3` (krev) |
| SMS kód pro rezervaci | `POST /api/PreReservationOtp/RequestNIS` → `VerifyNIS` |
| Rezervace | `POST /api/ReserveTerm/NIS/{idPrac}/{kalnum}` + `POST /api/UploadFiles/NIS` |
| Přihlášení ke správě | `POST /api/OtpRequest/NIS` → `POST /api/Authenticate/NIS/Guest` (token ~20 min) |
| Zrušení | `GET /api/ReservedTerms/NIS` → `POST /api/DeleteReservation/NIS/{key}` |

Nemocnice ověřuje telefon SMS kódem u každé rezervace i každého zrušení. Integrace to neobchází a kód vždy zadáváš ty.
