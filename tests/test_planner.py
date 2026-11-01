"""Testy plánovače a parseru e-mailů (spouští se bez Home Assistantu)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "darovani"


def _load(name: str):
    """Načte modul přímo ze souboru – bez importu celého balíčku (HA)."""
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


planner = _load("planner")
mail_parser = _load("mail_parser")

TZ = ZoneInfo("Europe/Prague")
RULES = planner.Rules()


def dt(y, m, d, hh=7, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


def don(kind, y, m, d, reserved=False, hh=7):
    return planner.Donation(kind=kind, start=dt(y, m, d, hh), reserved=reserved)


# --- Pravidla -------------------------------------------------------------


def test_add_months_clamps_end_of_month():
    assert planner.add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    assert planner.add_months(date(2027, 1, 31), 1) == date(2027, 2, 28)


def test_after_blood_comes_plasma_month_later():
    kind, earliest = planner.next_after("krev", date(2026, 11, 30), date(2026, 11, 30), RULES)
    assert kind == "plazma"
    assert earliest == date(2026, 12, 30)


def test_full_cycle_three_plasmas_then_blood():
    """Krev 0. den -> plazma 30, 51, 72 -> krev ~93 (jako Jakubův rytmus)."""
    b = date(2026, 1, 1)
    steps = planner.build_plan([don("krev", 2026, 1, 1)], date(2026, 1, 2), RULES, horizon_days=120)
    kinds = [(s.kind, (s.earliest - b).days) for s in steps]
    assert kinds[:4] == [("plazma", 30), ("plazma", 51), ("plazma", 72), ("krev", 93)]


def test_current_situation_plan():
    """Skutečný stav k 1. 11. 2026: plazma 2. 11. a krev 30. 11. rezervované."""
    history = [
        don("krev", 2026, 8, 31),
        don("plazma", 2026, 10, 12),
        don("plazma", 2026, 11, 2, reserved=True),
        don("krev", 2026, 11, 30, reserved=True),
    ]
    steps = planner.build_plan(history, date(2026, 11, 1), RULES, horizon_days=150)
    assert [s.state for s in steps[:3]] == ["reserved", "reserved", "bookable"]
    assert not any(s.conflict for s in steps)
    nxt = steps[2]
    assert (nxt.kind, nxt.earliest) == ("plazma", date(2026, 12, 30))
    assert [(s.kind, s.earliest) for s in steps[3:6]] == [
        ("plazma", date(2027, 1, 20)),
        ("plazma", date(2027, 2, 10)),
        ("krev", date(2027, 3, 3)),
    ]
    assert all(s.state == "planned" for s in steps[3:])


def test_plasma_too_soon_after_blood_is_conflict():
    """Plazma 17. 12. po krvi 30. 11. je příliš brzy."""
    history = [don("krev", 2026, 11, 30, reserved=True), don("plazma", 2026, 12, 17, reserved=True)]
    steps = planner.build_plan(history, date(2026, 11, 1), RULES)
    assert steps[1].conflict is True


def test_empty_history_starts_with_blood_today():
    steps = planner.build_plan([], date(2026, 11, 1), RULES)
    assert steps[0].kind == "krev"
    assert steps[0].earliest == date(2026, 11, 1)
    assert steps[0].state == "bookable"


def test_blocked_until_moves_next_step():
    history = [don("krev", 2026, 8, 31), don("plazma", 2026, 10, 12)]
    steps = planner.build_plan(history, date(2026, 11, 1), RULES, blocked_until=date(2026, 11, 15))
    assert steps[0].earliest == date(2026, 11, 15)


# --- Volné termíny --------------------------------------------------------


def test_suggest_prefers_early_slots_one_per_day():
    slots = [dt(2027, 1, 5, 9, 45), dt(2027, 1, 5, 10, 0), dt(2027, 1, 6, 11, 0), dt(2027, 1, 7, 9, 0), dt(2027, 1, 8, 8, 0)]
    picked = planner.suggest_slots(slots, date(2027, 1, 5), time(10, 0), count=3)
    assert picked == [dt(2027, 1, 5, 9, 45), dt(2027, 1, 7, 9, 0), dt(2027, 1, 8, 8, 0)]


def test_suggest_falls_back_to_late_slots():
    slots = [dt(2027, 1, 5, 12, 0), dt(2027, 1, 6, 13, 0)]
    picked = planner.suggest_slots(slots, date(2027, 1, 1), time(10, 0), count=3)
    assert picked == slots


# --- Odklad ---------------------------------------------------------------


def test_postpone_two_weeks_moves_and_cascades():
    """Opar 1. 11. -> 14 dní: plazma 2. 11. se musí posunout, krev 30. 11. ne."""
    history = [
        don("krev", 2026, 8, 31),
        don("plazma", 2026, 10, 12),
        don("plazma", 2026, 11, 2, reserved=True, hh=9),
        don("krev", 2026, 11, 30, reserved=True, hh=6),
    ]
    slots = {
        "plazma": [dt(2026, 11, 16, 11, 0), dt(2026, 11, 17, 9, 45)],
        "krev": [dt(2026, 12, 8, 6, 15)],
    }
    res = planner.simulate_postpone(history, date(2026, 11, 1), 14, RULES, slots, time(10, 0))
    actions = [(c.reservation.day, c.action) for c in res.changes]
    assert actions[0] == (date(2026, 11, 2), "move")
    assert res.changes[0].replacement == dt(2026, 11, 17, 9, 45)  # ranní má přednost
    # Krev 30. 11. je 13 dní po nové plazmě 17. 11. -> musí se posunout taky
    assert actions[1] == (date(2026, 11, 30), "move")
    assert res.changes[1].replacement == dt(2026, 12, 8, 6, 15)


def test_postpone_without_free_slot_cancels():
    history = [don("plazma", 2026, 11, 2, reserved=True)]
    res = planner.simulate_postpone(history, date(2026, 11, 1), 14, RULES, {}, time(10, 0))
    assert res.changes[0].action == "cancel"
    assert res.changes[0].new_earliest == date(2026, 11, 15)


# --- E-maily --------------------------------------------------------------

NEW_BODY = """Toto je automaticky generovaný e-mail. Na tento e-mail prosím neodpovídejte!

Byl pro Vás rezervován nový termín.

Termín: pondělí 30.11.2026 6:15.
Typ systému: Nemocniční Informační Systém (NIS).
Název kalendáře: Dárci krve.
Klinika: Transfuzní oddělení (TO).
Pracoviště: TO - Ambulance.

Rezervaci můžete upravit, smazat nebo přesunout na jiný termín použitím následujícího odkazu: https://rezervace.fnusa.cz/Administration?ReservationKey=TESTKEY0000000000000000000000000

Výše uvedený odkaz je určený pouze Vám."""

CANCEL_BODY = """Vaše rezervace byla zrušena.

Termín: úterý 20.10.2026 8:45.
Typ systému: Nemocniční Informační Systém (NIS).
Název kalendáře: Plazmaferézy - Objednávky.
Klinika: Transfuzní oddělení (TO)."""


def test_parse_confirmation():
    ev = mail_parser.parse_mail("Rezervace nového termínu v systému NIS: Dárci krve (TO)", NEW_BODY)
    assert ev.type == "new" and ev.kind == "krev"
    assert ev.start == dt(2026, 11, 30, 6, 15)
    assert ev.key == "TESTKEY0000000000000000000000000"
    assert ev.admin_url.endswith("ReservationKey=TESTKEY0000000000000000000000000")


def test_parse_cancel():
    ev = mail_parser.parse_mail("Storno rezervace v systému NIS: Plazmaferézy - Objednávky (TO)", CANCEL_BODY)
    assert ev.type == "cancel" and ev.kind == "plazma"
    assert ev.start == dt(2026, 10, 20, 8, 45)
    assert ev.key is None


def test_parse_other_calendar_ignored():
    body = NEW_BODY.replace("Dárci krve", "Holter")
    assert mail_parser.parse_mail("Rezervace nového termínu v systému NIS: Holter (I.IKAK)", body) is None
