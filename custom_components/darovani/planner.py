"""Plánovač odběrů – čistá logika bez závislosti na Home Assistantu.

Pravidla (výchozí hodnoty, dají se změnit ve volbách integrace):
  * krev -> krev:      nejdříve za 3 měsíce
  * krev -> plazma:    nejdříve za 30 dní
  * plazma -> plazma:  nejdříve za 21 dní
  * plazma -> krev:    nejdříve za 21 dní

Cyklus tedy vypadá: krev, ~měsíc pauza, plazma každé 3 týdny, a jakmile by
další plazma posunula krev o víc než `slack_days` za její termín, přijde
na řadu krev.

Zjednodušeně: plánovač se podívá na poslední odběry (proběhlé i rezervované),
z pravidel spočítá, co a odkdy je možné jako další, a takhle pokračuje
až do konce plánovacího horizontu (výchozí 3 měsíce).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable

KIND_BLOOD = "krev"
KIND_PLASMA = "plazma"

# Stavy kroku plánu
STEP_RESERVED = "reserved"      # termín je zarezervovaný
STEP_BOOKABLE = "bookable"      # další na řadě, dá se rezervovat
STEP_PLANNED = "planned"        # výhled – rezervovat až po předchozím kroku


@dataclass(frozen=True)
class Rules:
    """Minimální rozestupy mezi odběry."""

    blood_interval_months: int = 3
    blood_to_plasma_days: int = 30
    plasma_interval_days: int = 21
    plasma_to_blood_days: int = 21
    slack_days: int = 7


@dataclass(frozen=True)
class Donation:
    """Proběhlý nebo rezervovaný odběr, ze kterého plánovač vychází."""

    kind: str
    start: datetime
    reserved: bool = False          # True = budoucí rezervace, False = proběhlý
    ref: str | None = None          # id rezervace v úložišti

    @property
    def day(self) -> date:
        return self.start.date()


@dataclass
class Step:
    """Jeden krok plánu (konkrétní odběr v budoucnu)."""

    kind: str
    earliest: date                  # nejdřívější možné datum podle pravidel
    state: str
    reservation: Donation | None = None
    conflict: bool = False          # rezervace porušuje pravidla (moc brzy)

    @property
    def day(self) -> date:
        """Skutečné (rezervované) nebo předpokládané datum kroku."""
        return self.reservation.day if self.reservation else self.earliest


def add_months(d: date, months: int) -> date:
    """Přičte kalendářní měsíce, 31. 1. + 1 měsíc = 28./29. 2."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last_day))


def next_after(
    last_kind: str | None,
    last_day: date | None,
    last_blood_day: date | None,
    rules: Rules,
) -> tuple[str, date | None]:
    """Vrátí (druh, nejdřívější datum) dalšího odběru po posledním odběru.

    Datum None znamená "kdykoliv" (zatím žádná historie).
    """
    if last_kind is None or last_day is None:
        return KIND_BLOOD, None

    if last_kind == KIND_BLOOD:
        return KIND_PLASMA, last_day + timedelta(days=rules.blood_to_plasma_days)

    # Poslední byla plazma
    plasma_next = last_day + timedelta(days=rules.plasma_interval_days)
    blood_after_plasma = last_day + timedelta(days=rules.plasma_to_blood_days)
    if last_blood_day is None:
        # Krev ještě nikdy – přednost má krev, jakmile to jde
        return KIND_BLOOD, blood_after_plasma

    blood_due = add_months(last_blood_day, rules.blood_interval_months)
    # Vejde se ještě jedna plazma, aniž by krev ujela o víc než slack?
    if plasma_next + timedelta(days=rules.plasma_to_blood_days) <= blood_due + timedelta(
        days=rules.slack_days
    ):
        return KIND_PLASMA, plasma_next
    return KIND_BLOOD, max(blood_due, blood_after_plasma)


def build_plan(
    donations: Iterable[Donation],
    today: date,
    rules: Rules,
    horizon_days: int = 90,
    blocked_until: date | None = None,
) -> list[Step]:
    """Sestaví plán budoucích odběrů.

    `donations` = proběhlé odběry + budoucí rezervace (zrušené sem nepatří).
    `blocked_until` = odklad (např. opar) – nic nesmí být dřív než tohle datum.
    """
    ordered = sorted(donations, key=lambda d: d.start)
    horizon_end = today + timedelta(days=horizon_days)

    last_kind: str | None = None
    last_day: date | None = None
    last_blood: date | None = None
    steps: list[Step] = []

    def floor(d: date | None) -> date:
        """Nejdřívější datum nesmí být v minulosti ani v době odkladu."""
        candidates = [today]
        if d is not None:
            candidates.append(d)
        if blocked_until is not None:
            candidates.append(blocked_until)
        return max(candidates)

    for don in ordered:
        if don.reserved and don.day >= today:
            kind_rule, earliest_rule = next_after(last_kind, last_day, last_blood, rules)
            conflict = earliest_rule is not None and don.day < earliest_rule
            if blocked_until is not None and don.day < blocked_until:
                conflict = True
            steps.append(
                Step(
                    kind=don.kind,
                    earliest=earliest_rule or don.day,
                    state=STEP_RESERVED,
                    reservation=don,
                    conflict=conflict,
                )
            )
        last_kind, last_day = don.kind, don.day
        if don.kind == KIND_BLOOD:
            last_blood = don.day

    # Doplnění neobsazených kroků až do horizontu
    first_free = True
    for _ in range(24):  # pojistka proti nekonečné smyčce
        kind, earliest = next_after(last_kind, last_day, last_blood, rules)
        earliest = floor(earliest)
        if earliest > horizon_end:
            break
        steps.append(
            Step(
                kind=kind,
                earliest=earliest,
                state=STEP_BOOKABLE if first_free else STEP_PLANNED,
            )
        )
        first_free = False
        last_kind, last_day = kind, earliest
        if kind == KIND_BLOOD:
            last_blood = earliest
    return steps


# --- Volné termíny -------------------------------------------------------


def slots_from(slots: Iterable[datetime], earliest: date) -> list[datetime]:
    """Volné časy od daného data (včetně), seřazené."""
    return sorted(s for s in slots if s.date() >= earliest)


def group_by_day(slots: Iterable[datetime]) -> dict[date, list[datetime]]:
    """Seskupí časy podle dne (dny i časy seřazené)."""
    out: dict[date, list[datetime]] = {}
    for s in sorted(slots):
        out.setdefault(s.date(), []).append(s)
    return out


def suggest_slots(
    slots: Iterable[datetime],
    earliest: date,
    preferred_latest: time,
    count: int = 3,
) -> list[datetime]:
    """Návrh `count` termínů: z každého dne ten nejranější.

    Nejdřív se berou dny, kde nejranější čas splňuje preferenci
    (začátek nejpozději v `preferred_latest`). Když jich je málo,
    doplní se i pozdější časy, aby bylo vždy z čeho vybírat.
    Výsledek je seřazený podle data.
    """
    days = group_by_day(slots_from(slots, earliest))
    firsts = [times[0] for times in days.values()]
    preferred = [s for s in firsts if s.time() <= preferred_latest]
    picked = preferred[:count]
    if len(picked) < count:
        rest = [s for s in firsts if s not in picked]
        picked.extend(rest[: count - len(picked)])
    return sorted(picked)


# --- Odklad (např. opar) -------------------------------------------------


@dataclass
class PostponeChange:
    """Co se stane s jednou budoucí rezervací při odkladu."""

    reservation: Donation
    action: str                      # "keep" | "move" | "cancel"
    new_earliest: date | None = None
    replacement: datetime | None = None


@dataclass
class PostponeResult:
    blocked_until: date
    changes: list[PostponeChange] = field(default_factory=list)

    @property
    def affected(self) -> list[PostponeChange]:
        return [c for c in self.changes if c.action != "keep"]


def simulate_postpone(
    donations: Iterable[Donation],
    today: date,
    days: int,
    rules: Rules,
    slots_by_kind: dict[str, list[datetime]],
    preferred_latest: time,
) -> PostponeResult:
    """Spočítá, co se musí posunout, když teď nemůžu `days` dní darovat.

    Rezervace se procházejí v čase. Každá, která by byla dřív, než dovolí
    odklad nebo pravidla (vůči případně posunutému předchozímu odběru),
    se přesune na první volný termín – s preferencí ranních časů. Když
    volný termín zatím není (nemocnice ještě nevypsala), rezervace se zruší
    a krok se později nabídne k rezervaci znovu.
    """
    blocked_until = today + timedelta(days=days)
    ordered = sorted(donations, key=lambda d: d.start)

    last_kind: str | None = None
    last_day: date | None = None
    last_blood: date | None = None
    result = PostponeResult(blocked_until=blocked_until)
    taken: set[datetime] = set()

    for don in ordered:
        if not (don.reserved and don.day >= today):
            last_kind, last_day = don.kind, don.day
            if don.kind == KIND_BLOOD:
                last_blood = don.day
            continue

        _, rule_earliest = next_after(last_kind, last_day, last_blood, rules)
        earliest = max(d for d in (rule_earliest, blocked_until) if d is not None)

        if don.day >= earliest:
            result.changes.append(PostponeChange(don, "keep"))
            new_day = don.day
        else:
            free = [s for s in slots_by_kind.get(don.kind, []) if s not in taken]
            # Preferuj stejný čas začátku jako původní rezervace, jinak ranní
            proposal = suggest_slots(free, earliest, preferred_latest, count=1)
            if proposal:
                taken.add(proposal[0])
                result.changes.append(
                    PostponeChange(don, "move", new_earliest=earliest, replacement=proposal[0])
                )
                new_day = proposal[0].date()
            else:
                result.changes.append(PostponeChange(don, "cancel", new_earliest=earliest))
                new_day = earliest
        last_kind, last_day = don.kind, new_day
        if don.kind == KIND_BLOOD:
            last_blood = new_day
    return result
