/*
 * Darování krve a plazmy – karta pro Home Assistant
 *
 * Jedna karta, ve které je všechno podstatné a nic navíc:
 *   1. rozpracovaný krok (zadání kódu z SMS) – když nějaký běží,
 *   2. další odběr se správou a zrušením,
 *   3. výběr termínu pro další krok (návrhy + všechny volné časy),
 *   4. plán na 3 měsíce, odklad (např. opar) s náhledem změn,
 *   5. historie (sbalená).
 *
 * Data chodí živě přes websocket "darovani/subscribe", akce jsou služby
 * domény "darovani". Barvy a písmo se berou z aktivního motivu HA.
 */

const KIND = {
  krev: { label: "Krev", color: "var(--darovani-blood, #c62828)", icon: "mdi:water" },
  plazma: { label: "Plazma", color: "var(--darovani-plasma, #d99a00)", icon: "mdi:water-outline" },
};
const TZ = "Europe/Prague";
const fmt = (opts) => new Intl.DateTimeFormat("cs-CZ", { timeZone: TZ, ...opts });
const F_WD = fmt({ weekday: "short" });
const F_WDL = fmt({ weekday: "long" });
const F_DM = fmt({ day: "numeric", month: "numeric" });
const F_T = fmt({ hour: "numeric", minute: "2-digit" });

const d = (iso) => (iso && iso.length === 10 ? new Date(iso + "T12:00:00") : new Date(iso));
const day = (iso) => `${F_WD.format(d(iso))} ${F_DM.format(d(iso))}`;
const dayLong = (iso) => `${F_WDL.format(d(iso))} ${F_DM.format(d(iso))}`;
const time = (iso) => F_T.format(d(iso));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function relative(iso) {
  const start = new Date(d(iso).toLocaleDateString("en-CA", { timeZone: TZ }) + "T00:00:00");
  const today = new Date(new Date().toLocaleDateString("en-CA", { timeZone: TZ }) + "T00:00:00");
  const diff = Math.round((start - today) / 86400000);
  if (diff === 0) return "dnes";
  if (diff === 1) return "zítra";
  if (diff < 5) return `za ${diff} dny`;
  if (diff < 7) return `za ${diff} dní`;
  const w = Math.round(diff / 7);
  if (diff < 35) return w === 1 ? "za týden" : w < 5 ? `za ${w} týdny` : `za ${w} týdnů`;
  return `za ${diff} dní`;
}

class DarovaniCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.s = null; // snapshot ze serveru
    this.ui = {
      day: null,         // vybraný den ve výběru termínu
      slot: null,        // vybraný čas
      allDays: false,    // rozbalený seznam všech dnů
      confirmCancel: null,
      postpone: false,
      postponeDays: 14,
      postponeReason: "Opar",
      preview: null,
      history: false,
      code: "",
      busy: false,
      error: null,
    };
  }

  setConfig(config) {
    this.config = config || {};
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._subscribe();
  }

  connectedCallback() {
    if (this._hass && !this._unsub) this._subscribe();
  }

  disconnectedCallback() {
    if (this._unsub) {
      this._unsub.then((u) => u && u()).catch(() => {});
      this._unsub = null;
    }
  }

  _subscribe() {
    if (!this._hass || this._unsub) return;
    this._unsub = this._hass.connection
      .subscribeMessage((msg) => {
        this.s = msg;
        this._render();
      }, { type: "darovani/subscribe" })
      .catch((err) => {
        this.ui.error = err.message || String(err);
        this._render();
      });
    this._render();
  }

  getCardSize() {
    return 8;
  }

  static getStubConfig() {
    return {};
  }

  // ---------------------------------------------------------------- akce

  async _call(service, data = {}) {
    this.ui.busy = true;
    this.ui.error = null;
    this._render();
    try {
      await this._hass.callService("darovani", service, data);
    } catch (err) {
      this.ui.error = err.message || String(err);
    }
    this.ui.busy = false;
    this._render();
  }

  async _loadPreview() {
    this.ui.preview = null;
    this._render();
    try {
      this.ui.preview = await this._hass.callWS({ type: "darovani/postpone_preview", days: Number(this.ui.postponeDays) });
    } catch (err) {
      this.ui.error = err.message || String(err);
    }
    this._render();
  }

  _onClick(ev) {
    const el = ev.target.closest("[data-a]");
    if (!el) return;
    const a = el.dataset.a;
    const v = el.dataset.v;
    const ui = this.ui;
    switch (a) {
      case "refresh": return this._call("refresh");
      case "pick-slot": ui.slot = v; ui.day = v.slice(0, 10); break;
      case "pick-day": ui.day = v; ui.slot = null; break;
      case "all-days": ui.allDays = !ui.allDays; break;
      case "book": {
        const kind = this.s.offer.kind;
        const start = ui.slot;
        ui.slot = null; ui.code = "";
        return this._call("select_slot", { kind, start });
      }
      case "code-submit": {
        const code = ui.code.trim();
        if (!code) return;
        ui.code = "";
        return this._call("submit_code", { code });
      }
      case "code-resend": return this._call("resend_code");
      case "abort": return this._call("abort");
      case "cancel-ask": ui.confirmCancel = v; break;
      case "cancel-no": ui.confirmCancel = null; break;
      case "cancel-yes": ui.confirmCancel = null; return this._call("cancel_reservation", { reservation_id: v });
      case "postpone": ui.postpone = !ui.postpone; if (ui.postpone) return this._loadPreview(); break;
      case "pp-days": ui.postponeDays = Number(v); return this._loadPreview();
      case "pp-reason": ui.postponeReason = v; break;
      case "pp-apply":
        ui.postpone = false;
        return this._call("postpone_apply", { days: Number(ui.postponeDays), reason: ui.postponeReason });
      case "clear-deferral": return this._call("clear_deferral");
      case "history": ui.history = !ui.history; break;
      case "not-done": return this._call("mark_not_done", { reservation_id: v });
      case "dismiss-error": ui.error = null; break;
      default: return;
    }
    this._render();
  }

  // ------------------------------------------------------------- vykreslení

  _render() {
    const root = this.shadowRoot;
    const focused = root.activeElement && root.activeElement.id;
    const caret = focused === "code" ? root.activeElement.selectionStart : null;

    root.innerHTML = `<style>${STYLE}</style><ha-card>${this._body()}</ha-card>`;
    root.querySelector("ha-card").addEventListener("click", (e) => this._onClick(e));
    const code = root.getElementById("code");
    if (code) {
      code.addEventListener("input", (e) => (this.ui.code = e.target.value));
      code.addEventListener("keydown", (e) => {
        if (e.key === "Enter") this._onClick({ target: root.querySelector("[data-a=code-submit]") });
      });
      if (focused === "code" || this.s?.flow?.awaiting) {
        code.focus();
        if (caret !== null) code.setSelectionRange(caret, caret);
      }
    }
    const ppInput = root.getElementById("pp-custom");
    if (ppInput) {
      ppInput.addEventListener("change", (e) => {
        const n = Math.max(1, Math.min(120, Number(e.target.value) || 14));
        this.ui.postponeDays = n;
        this._loadPreview();
      });
    }
  }

  _body() {
    const s = this.s;
    if (!s) return `<div class="empty">Načítám…</div>${this._errorBar()}`;
    if (s.setup === false) {
      return `<div class="empty">Integrace <b>Darování</b> zatím není nastavená.<br><span class="muted">Nastavení → Zařízení a služby → Přidat integraci.</span></div>`;
    }
    return [
      this._header(),
      this._errorBar(),
      this._flow(),
      this._deferral(),
      this._next(),
      this._offer(),
      this._plan(),
      this._postpone(),
      this._history(),
    ].join("");
  }

  _header() {
    const s = this.s;
    const pill = {
      to_book: ["K rezervaci", "warn"],
      waiting_code: ["Čeká na kód", "accent"],
      booking: ["Probíhá", "accent"],
      error: ["Chyba", "err"],
    }[s.status];
    return `
      <div class="head">
        <div class="title">Darování</div>
        ${pill ? `<span class="pill ${pill[1]}">${pill[0]}</span>` : ""}
        <span class="grow"></span>
        <button class="icon" data-a="refresh" title="Obnovit"${this.ui.busy ? " disabled" : ""}>
          <ha-icon icon="mdi:refresh"></ha-icon></button>
      </div>`;
  }

  _errorBar() {
    const msg = this.ui.error || (this.s && this.s.flow && this.s.flow.error) || null;
    if (!msg) return "";
    return `<div class="alert" data-a="dismiss-error">${esc(msg)}</div>`;
  }

  _flow() {
    const f = this.s.flow;
    if (!f) return "";
    if (f.awaiting) {
      const what = f.awaiting === "booking_code" ? "rezervaci" : "správu rezervace";
      return `
        <section class="flow">
          <div class="flow-title">${esc(f.title)}</div>
          <div class="muted">FN USA ti poslala SMS s kódem pro ${what}.</div>
          <div class="code-row">
            <input id="code" inputmode="numeric" autocomplete="one-time-code" placeholder="Kód z SMS"
                   value="${esc(this.ui.code)}" ${this.ui.busy ? "disabled" : ""}>
            <button class="primary" data-a="code-submit" ${this.ui.busy ? "disabled" : ""}>Potvrdit</button>
          </div>
          <div class="row-links">
            <button class="link" data-a="code-resend">Poslat znovu</button>
            <button class="link" data-a="abort">Přerušit</button>
          </div>
        </section>`;
    }
    return `
      <section class="flow">
        <div class="flow-title">${esc(f.title)}</div>
        <div class="muted"><span class="spinner"></span>Pracuji${f.remaining > 1 ? ` · zbývá ${f.remaining} kroků` : ""}…</div>
        <div class="row-links"><button class="link" data-a="abort">Přerušit</button></div>
      </section>`;
  }

  _deferral() {
    const s = this.s;
    if (!s.blocked_until) return "";
    const until = d(s.blocked_until);
    if (until < new Date(new Date().toDateString())) return "";
    return `
      <div class="banner">
        <ha-icon icon="mdi:pause-circle-outline"></ha-icon>
        <span>Odklad do <b>${day(s.blocked_until)}</b>${s.block_reason ? ` · ${esc(s.block_reason)}` : ""}</span>
        <span class="grow"></span>
        <button class="link" data-a="clear-deferral">Zrušit odklad</button>
      </div>`;
  }

  _kindDot(kind, hollow = false) {
    const c = KIND[kind].color;
    return `<span class="dot${hollow ? " hollow" : ""}" style="--c:${c}"></span>`;
  }

  _next() {
    const s = this.s;
    const n = s.next;
    if (!n) {
      return `<section><div class="label">Další odběr</div><div class="muted">Nic rezervováno.</div></section>`;
    }
    const k = KIND[n.kind];
    const confirming = this.ui.confirmCancel === n.id;
    const others = s.upcoming.slice(1);
    return `
      <section>
        <div class="label">Další odběr</div>
        <div class="hero" style="--c:${k.color}">
          <div class="hero-icon"><ha-icon icon="${k.icon}"></ha-icon></div>
          <div class="hero-main">
            <div class="hero-kind">${k.label} · ${relative(n.start)}</div>
            <div class="hero-date">${dayLong(n.start)} <span class="hero-time">${time(n.start)}</span></div>
            <div class="muted">FN u sv. Anny · budova B1, 1. PP</div>
          </div>
        </div>
        <div class="actions">
          ${confirming
            ? `<span class="muted">Zrušit u nemocnice? Přijde SMS kód.</span>
               <span class="grow"></span>
               <button class="ghost" data-a="cancel-no">Ne</button>
               <button class="danger" data-a="cancel-yes" data-v="${n.id}">Zrušit rezervaci</button>`
            : `${n.admin_url ? `<a class="ghost" href="${esc(n.admin_url)}" target="_blank" rel="noreferrer">Správa u FN USA</a>` : `<span class="muted small">Odkaz pro správu dorazí e-mailem…</span>`}
               <span class="grow"></span>
               <button class="ghost" data-a="cancel-ask" data-v="${n.id}" ${n.has_key ? "" : "disabled"}>Zrušit</button>`}
        </div>
        ${others.length ? `<div class="list">${others.map((r) => this._resRow(r)).join("")}</div>` : ""}
      </section>`;
  }

  _resRow(r) {
    const confirming = this.ui.confirmCancel === r.id;
    return `
      <div class="li">
        ${this._kindDot(r.kind)}
        <span class="li-main"><b>${KIND[r.kind].label}</b> · ${day(r.start)} · ${time(r.start)}</span>
        ${confirming
          ? `<button class="ghost sm" data-a="cancel-no">Ne</button><button class="danger sm" data-a="cancel-yes" data-v="${r.id}">Zrušit</button>`
          : `${r.admin_url ? `<a class="icon sm" href="${esc(r.admin_url)}" target="_blank" rel="noreferrer" title="Správa u FN USA"><ha-icon icon="mdi:open-in-new"></ha-icon></a>` : ""}
             <button class="icon sm" data-a="cancel-ask" data-v="${r.id}" title="Zrušit" ${r.has_key ? "" : "disabled"}><ha-icon icon="mdi:close"></ha-icon></button>`}
      </div>`;
  }

  _offer() {
    const o = this.s.offer;
    if (!o || this.s.flow) return "";
    const k = KIND[o.kind];
    const ui = this.ui;
    const head = `
      <div class="label">Další k rezervaci</div>
      <div class="offer-head">${this._kindDot(o.kind)}<b>${k.label}</b><span class="muted">&nbsp;· možné od ${day(o.earliest)}</span></div>`;
    if (!o.days.length) {
      return `<section>${head}<div class="muted">Nemocnice zatím vypsala termíny jen${o.max_date ? ` do ${day(o.max_date)}` : ""}. Jakmile se objeví, pošlu notifikaci.</div></section>`;
    }
    const chip = (iso, extra = "") =>
      `<button class="chip${ui.slot === iso ? " on" : ""}${this._isLate(iso) ? " late" : ""} ${extra}" data-a="pick-slot" data-v="${iso}">
         <span class="chip-d">${day(iso)}</span><span class="chip-t">${time(iso)}</span></button>`;
    const suggested = `<div class="chips">${o.suggested.map((iso) => chip(iso)).join("")}</div>`;

    let all = "";
    if (ui.allDays) {
      const sel = ui.day || o.days[0].date;
      const dayObj = o.days.find((x) => x.date === sel) || o.days[0];
      all = `
        <div class="days">${o.days.map((x) => `
          <button class="daypill${x.date === dayObj.date ? " on" : ""}" data-a="pick-day" data-v="${x.date}">
            <span>${F_WD.format(d(x.date))}</span><b>${F_DM.format(d(x.date))}</b><small>${x.slots.length}×</small>
          </button>`).join("")}
        </div>
        <div class="times">${dayObj.slots.map((iso) => `
          <button class="tchip${ui.slot === iso ? " on" : ""}${this._isLate(iso) ? " late" : ""}" data-a="pick-slot" data-v="${iso}">${time(iso)}</button>`).join("")}
        </div>`;
    }
    const confirm = ui.slot
      ? `<div class="confirm">
           <span>${k.label} · <b>${dayLong(ui.slot)} v ${time(ui.slot)}</b></span>
           <span class="grow"></span>
           <button class="primary" data-a="book" ${this.ui.busy || this.s.flow ? "disabled" : ""}>Rezervovat</button>
         </div>`
      : "";
    return `
      <section>
        ${head}
        ${suggested}
        <button class="link" data-a="all-days">${ui.allDays ? "Skrýt ostatní termíny" : `Všechny volné termíny (${o.days.length} dní)`}</button>
        ${all}
        ${confirm}
      </section>`;
  }

  _isLate(iso) {
    const [h, m] = this.s.preferred_latest.split(":").map(Number);
    const t = d(iso);
    const parts = F_T.formatToParts(t);
    const hh = Number(parts.find((p) => p.type === "hour").value);
    const mm = Number(parts.find((p) => p.type === "minute").value);
    return hh * 60 + mm > h * 60 + m;
  }

  _plan() {
    const steps = this.s.plan.filter((p) => p.state !== "reserved" || p.reservation);
    if (!steps.length) return "";
    const rows = steps.map((p) => {
      const k = KIND[p.kind];
      if (p.reservation) {
        return `<div class="tl">${this._kindDot(p.kind)}<span><b>${k.label}</b> · ${day(p.reservation.start)} · ${time(p.reservation.start)}</span>
          ${p.conflict ? `<span class="warn-text">příliš brzy</span>` : `<span class="muted small">rezervováno</span>`}</div>`;
      }
      return `<div class="tl planned">${this._kindDot(p.kind, true)}<span>${k.label} · od ${day(p.earliest)}</span>
        <span class="muted small">${p.state === "bookable" ? "k rezervaci" : "výhled"}</span></div>`;
    });
    return `<section><div class="label">Plán</div><div class="timeline">${rows.join("")}</div></section>`;
  }

  _postpone() {
    const ui = this.ui;
    if (!ui.postpone) {
      return `<section class="slim"><button class="ghost wide" data-a="postpone"><ha-icon icon="mdi:calendar-clock"></ha-icon>Odložit darování…</button></section>`;
    }
    const presets = [7, 14, 21];
    const reasons = ["Opar", "Nemoc", "Cesta", "Jiné"];
    let body = `<div class="muted">Načítám náhled…</div>`;
    const p = ui.preview;
    if (p) {
      const changes = p.changes.filter((c) => c.action !== "keep" || true);
      const affected = p.changes.filter((c) => c.action !== "keep");
      body = changes.length
        ? `<div class="list">${changes.map((c) => {
            const k = KIND[c.kind];
            const from = `${day(c.start)} ${time(c.start)}`;
            let to = `<span class="muted small">beze změny</span>`;
            if (c.action === "move") to = `→ <b>${day(c.replacement)} ${time(c.replacement)}</b>`;
            if (c.action === "cancel") to = `<span class="warn-text">zrušit · nový termín od ${day(c.new_earliest)} zatím není</span>`;
            return `<div class="li">${this._kindDot(c.kind)}<span class="li-main">${k.label} · ${from}</span>${to}</div>`;
          }).join("")}</div>`
        : `<div class="muted">Žádná rezervace se nemění. Další krok se naplánuje od ${day(p.blocked_until)}.</div>`;
      if (affected.length) {
        body += `<div class="muted small">Zrušení i nové rezervace potvrdíš kódy z SMS (FN USA je posílá zvlášť pro každý krok).</div>`;
      }
    }
    return `
      <section class="panel">
        <div class="label">Odložit darování</div>
        <div class="chips">
          ${presets.map((n) => `<button class="chip sm${ui.postponeDays === n ? " on" : ""}" data-a="pp-days" data-v="${n}">${n} dní</button>`).join("")}
          <input id="pp-custom" class="num" type="number" min="1" max="120" value="${ui.postponeDays}" aria-label="Počet dní">
        </div>
        <div class="chips">
          ${reasons.map((r) => `<button class="chip sm${ui.postponeReason === r ? " on" : ""}" data-a="pp-reason" data-v="${r}">${r}</button>`).join("")}
        </div>
        ${p ? `<div class="muted small">Darovat nejdřív od <b>${day(p.blocked_until)}</b></div>` : ""}
        ${body}
        <div class="actions">
          <button class="ghost" data-a="postpone">Zavřít</button>
          <span class="grow"></span>
          <button class="primary" data-a="pp-apply" ${p ? "" : "disabled"}>Potvrdit odklad</button>
        </div>
      </section>`;
  }

  _history() {
    const h = this.s.history;
    if (!h.length) return "";
    const ui = this.ui;
    const list = ui.history
      ? `<div class="list">${h.map((r) => `
          <div class="li${r.status === "skipped" ? " skipped" : ""}">${this._kindDot(r.kind)}
            <span class="li-main">${KIND[r.kind].label} · ${day(r.start)} ${d(r.start).getFullYear()}</span>
            ${r.status === "skipped"
              ? `<span class="muted small">neproběhlo</span>`
              : `<button class="link small" data-a="not-done" data-v="${r.id}">neproběhlo?</button>`}
          </div>`).join("")}</div>`
      : "";
    return `<section class="slim"><button class="link" data-a="history">${ui.history ? "Skrýt historii" : `Historie (${h.length})`}</button>${list}</section>`;
  }
}

const STYLE = `
  :host { --gap: 12px; }
  ha-card { padding: 16px 16px 8px; display: block; }
  section { padding: 12px 0; border-top: 1px solid var(--divider-color, rgba(127,127,127,.2)); }
  section.slim { padding: 8px 0; }
  .head { display: flex; align-items: center; gap: 8px; padding-bottom: 8px; }
  .title { font-size: 1.15rem; font-weight: 600; letter-spacing: .01em; }
  .grow { flex: 1; }
  .label { font-size: .72rem; text-transform: uppercase; letter-spacing: .08em; color: var(--secondary-text-color); margin-bottom: 8px; }
  .muted { color: var(--secondary-text-color); font-size: .9rem; }
  .small { font-size: .8rem; }
  .empty { padding: 24px 8px; text-align: center; color: var(--primary-text-color); }
  .pill { font-size: .72rem; padding: 2px 8px; border-radius: 999px; font-weight: 600; }
  .pill.warn { background: color-mix(in srgb, var(--warning-color, #ffa600) 18%, transparent); color: var(--warning-color, #b37400); }
  .pill.accent { background: color-mix(in srgb, var(--primary-color) 18%, transparent); color: var(--primary-color); }
  .pill.err { background: color-mix(in srgb, var(--error-color, #db4437) 18%, transparent); color: var(--error-color, #db4437); }
  .alert { margin: 4px 0 8px; padding: 8px 12px; border-radius: 10px; font-size: .88rem; cursor: pointer;
           background: color-mix(in srgb, var(--error-color, #db4437) 12%, transparent); color: var(--primary-text-color); }
  .banner { display: flex; align-items: center; gap: 8px; padding: 8px 12px; margin: 0 0 4px; border-radius: 10px;
            background: color-mix(in srgb, var(--warning-color, #ffa600) 12%, transparent); font-size: .9rem; }
  .banner ha-icon { --mdc-icon-size: 18px; color: var(--warning-color, #b37400); }

  .hero { display: flex; gap: 14px; align-items: center; }
  .hero-icon { width: 44px; height: 44px; border-radius: 14px; display: grid; place-items: center; flex: none;
               background: color-mix(in srgb, var(--c) 16%, transparent); color: var(--c); }
  .hero-kind { font-size: .82rem; color: var(--c); font-weight: 600; }
  .hero-date { font-size: 1.25rem; font-weight: 600; line-height: 1.3; }
  .hero-time { font-weight: 400; color: var(--secondary-text-color); margin-left: 4px; }

  .actions { display: flex; align-items: center; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
  :where(button, a) { font: inherit; cursor: pointer; border: none; background: none; color: inherit; text-decoration: none; }
  button:disabled { opacity: .45; cursor: default; }
  .primary { background: var(--primary-color); color: var(--text-primary-color, #fff); padding: 8px 16px; border-radius: 10px; font-weight: 600; }
  .ghost { padding: 7px 14px; border-radius: 10px; border: 1px solid var(--divider-color, rgba(127,127,127,.3)); font-size: .9rem; display: inline-flex; align-items: center; gap: 6px; }
  .ghost.wide { width: 100%; justify-content: center; color: var(--secondary-text-color); }
  .ghost ha-icon { --mdc-icon-size: 18px; }
  .danger { background: var(--error-color, #db4437); color: #fff; padding: 7px 14px; border-radius: 10px; font-weight: 600; }
  .sm { padding: 4px 10px; font-size: .82rem; }
  .icon { width: 36px; height: 36px; border-radius: 50%; display: inline-grid; place-items: center; color: var(--secondary-text-color); }
  .icon.sm { width: 30px; height: 30px; padding: 0; }
  .icon ha-icon { --mdc-icon-size: 20px; }
  .icon:hover, .ghost:hover { background: color-mix(in srgb, var(--primary-text-color) 6%, transparent); }
  .link { color: var(--primary-color); padding: 6px 0; font-size: .9rem; }
  .row-links { display: flex; gap: 16px; }

  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--c); flex: none; display: inline-block; }
  .dot.hollow { background: transparent; box-shadow: inset 0 0 0 2px var(--c); }
  .offer-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }

  .chips { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; align-items: center; }
  .chip { display: inline-flex; flex-direction: column; align-items: flex-start; padding: 8px 12px; border-radius: 12px;
          border: 1px solid var(--divider-color, rgba(127,127,127,.3)); min-width: 84px; }
  .chip.sm { flex-direction: row; min-width: 0; padding: 6px 12px; }
  .chip-d { font-size: .78rem; color: var(--secondary-text-color); }
  .chip-t { font-size: 1.05rem; font-weight: 600; }
  .chip.on, .daypill.on, .tchip.on { border-color: var(--primary-color); background: color-mix(in srgb, var(--primary-color) 14%, transparent); }
  .chip.on .chip-d { color: var(--primary-color); }

  .days { display: flex; gap: 6px; overflow-x: auto; padding: 4px 0 8px; scrollbar-width: thin; }
  .daypill { flex: none; display: flex; flex-direction: column; align-items: center; padding: 6px 10px; border-radius: 12px;
             border: 1px solid var(--divider-color, rgba(127,127,127,.3)); min-width: 52px; font-size: .78rem; }
  .daypill b { font-size: .95rem; }
  .daypill small { color: var(--secondary-text-color); }
  .times { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
  .tchip { padding: 6px 12px; border-radius: 10px; border: 1px solid var(--divider-color, rgba(127,127,127,.3)); font-weight: 600; }
  .tchip.late, .chip.late .chip-t { font-weight: 400; color: var(--secondary-text-color); }

  .confirm { display: flex; align-items: center; gap: 8px; margin-top: 8px; padding: 10px 12px; border-radius: 12px;
             background: color-mix(in srgb, var(--primary-color) 8%, transparent); flex-wrap: wrap; }

  .flow { padding: 14px; margin: 4px 0 8px; border: none; border-radius: 14px; background: color-mix(in srgb, var(--primary-color) 9%, transparent); }
  .flow-title { font-weight: 600; margin-bottom: 4px; }
  .code-row { display: flex; gap: 8px; margin: 10px 0 4px; }
  .code-row input, .num { flex: 1; font: inherit; font-size: 1.15rem; letter-spacing: .12em; padding: 8px 12px; border-radius: 10px;
                          border: 1px solid var(--divider-color, rgba(127,127,127,.4)); background: var(--card-background-color, transparent); color: var(--primary-text-color); min-width: 0; }
  .num { flex: none; width: 72px; font-size: .9rem; letter-spacing: 0; padding: 6px 8px; }
  .spinner { display: inline-block; width: 12px; height: 12px; margin-right: 8px; border-radius: 50%; vertical-align: -1px;
             border: 2px solid var(--secondary-text-color); border-top-color: transparent; animation: spin .8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .timeline { display: flex; flex-direction: column; gap: 10px; }
  .tl { display: flex; align-items: center; gap: 10px; font-size: .92rem; }
  .tl > span:nth-child(2) { flex: 1; }
  .tl.planned { color: var(--secondary-text-color); }
  .warn-text { color: var(--warning-color, #b37400); font-size: .8rem; }

  .list { display: flex; flex-direction: column; margin-top: 8px; }
  .li { display: flex; align-items: center; gap: 10px; padding: 6px 0; font-size: .92rem; }
  .li-main { flex: 1; }
  .li.skipped { opacity: .55; }
  .panel { }
`;

customElements.define("darovani-card", DarovaniCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "darovani-card",
  name: "Darování krve a plazmy",
  description: "Další odběr, výběr termínu se SMS kódem, plán a odklad.",
});
