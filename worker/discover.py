"""Discovery with LunaRoute djev: code parses the page HTML into candidates (elements, option groups,
repeating cards, cart-line parts) with verified CSS/role selectors; djev answers typed questions about
which candidate is which. No screenshots, no generated text — every answer is one of our candidates."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin

from playwright.sync_api import ElementHandle, Locator, Page
from playwright.sync_api import Error as PlaywrightError

from worker.browser import ElementInfo, by_rc_id, detect_blocker, safe_click, snapshot
from worker.llm import LLM, SystemOneReply, choice, noul
from worker.prompts import MAX_STATE_CHARS, batch_questions, choice_q, noul_q, page_state
from worker.recipe import Blockers
from worker.safety import is_allowed_url

log = logging.getLogger("worker.discover")

MAX_OPTIONS = 250


# ---------------------------------------------------------------------------------------------
# Selector candidates
# ---------------------------------------------------------------------------------------------

_CSS_CANDIDATES_JS = r"""
e => {
  const esc = s => CSS.escape(s);
  const tag = e.tagName.toLowerCase();
  const out = [];
  if (e.id && !/\d{3,}|:|^_|_r_|_R_/.test(e.id)) out.push('#' + esc(e.id));
  for (const a of e.attributes) {
    if (a.name.startsWith('data-') && !a.name.startsWith('data-rc') && a.value && a.value.length <= 40
        && !/\d{4,}/.test(a.value)) out.push(`${tag}[${a.name}="${a.value.replace(/"/g, '\\"')}"]`);
  }
  if (e.getAttribute('aria-label')) out.push(`${tag}[aria-label="${e.getAttribute('aria-label').replace(/"/g, '\\"')}"]`);
  const classes = (typeof e.className === 'string' ? e.className : '').split(/\s+/).filter(Boolean);
  // CSS-module classes like "cart_cart-item__wfmzZ": the prefix before "__" is the stable part.
  for (const c of classes) {
    const m = c.match(/^(.+?)__[A-Za-z0-9_-]{4,}$/);
    if (m && m[1].includes('_')) out.push(`${tag}[class*="${m[1]}__"]`);
  }
  const plain = classes.filter(c => !/__[A-Za-z0-9_-]{4,}$|\d{3,}|[=+/]/.test(c)).slice(0, 2);
  if (plain.length) { out.push(tag + plain.map(c => '.' + esc(c)).join('')); if (plain.length > 1) out.push(tag + '.' + esc(plain[0])); }
  return out;
}
"""


def _css_candidates(scope: Page | Locator, handle: ElementHandle) -> list[dict]:
    """CSS selectors (relative to `scope`) that match `handle`, each with its match count in scope."""
    out: list[dict] = []
    for css in handle.evaluate(_CSS_CANDIDATES_JS):
        try:
            loc = scope.locator(css)
            n = loc.count()
            if n >= 1 and loc.evaluate_all("(els, t) => els.includes(t)", handle):
                entry = {"css": css, "matches": n}
                if entry not in out:
                    out.append(entry)
        except PlaywrightError:
            continue
    return out


def candidate_locators(page: Page, element_id: str, elements: list[ElementInfo] | None = None) -> list[dict]:
    """Role/name locator first (if it matches), then CSS candidates; unique matches sort first."""
    target = by_rc_id(page, element_id)
    if target.count() == 0:
        return []
    el = next((e for e in (elements or snapshot_cache(page)) if e.id == element_id), None)
    out: list[dict] = []
    if el and el.role and el.name:
        loc = page.get_by_role(el.role, name=el.name, exact=True)
        try:
            n = loc.count()
            if n >= 1 and loc.evaluate_all(f"els => els.some(e => e.getAttribute('data-rc-id') === '{element_id}')"):
                out.append({"role": el.role, "name": el.name, "exact": True, "matches": n})
        except PlaywrightError:
            pass
    out += _css_candidates(page, target.first.element_handle())
    out.sort(key=lambda c: (c["matches"] != 1, "css" in c))
    return out


_SNAP: dict[int, list[ElementInfo]] = {}


def snapshot_cache(page: Page) -> list[ElementInfo]:
    return _SNAP.get(id(page)) or snapshot(page, 400)


# ---------------------------------------------------------------------------------------------
# Structural parsing: option groups, text leaves, containers
# ---------------------------------------------------------------------------------------------

_GROUPS_JS = r"""
() => {
  document.querySelectorAll('[data-rc-gid]').forEach(e => e.removeAttribute('data-rc-gid'));
  document.querySelectorAll('[data-rc-oid]').forEach(e => e.removeAttribute('data-rc-oid'));
  const vis = e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const choicesIn = c => {
    if (c.tagName === 'SELECT') return [...c.options].map(o => ({el: o, label: clean(o.textContent), disabled: o.disabled, selected: o.selected}));
    const radios = [...c.querySelectorAll('input[type=radio], input[type=checkbox]')];
    if (radios.length >= 2) return radios.map(r => {
      const lab = r.closest('label') || (r.id && c.querySelector(`label[for="${CSS.escape(r.id)}"]`));
      return {el: lab || r, label: clean(lab ? lab.innerText : r.value), disabled: r.disabled, selected: r.checked};
    });
    const roles = [...c.querySelectorAll('[role=radio], [role=option]')];
    if (roles.length >= 2) return roles.map(r => ({el: r, label: clean(r.getAttribute('aria-label') || r.innerText),
      disabled: r.getAttribute('aria-disabled') === 'true', selected: r.getAttribute('aria-checked') === 'true' || r.getAttribute('aria-selected') === 'true'}));
    const buttons = [...c.querySelectorAll('button')];
    if (buttons.length >= 2 && buttons.length <= 40) return buttons.map(b => ({el: b, label: clean(b.getAttribute('aria-label') || b.innerText),
      disabled: b.disabled || b.getAttribute('aria-disabled') === 'true',
      selected: ['aria-pressed', 'aria-checked', 'aria-selected'].some(a => b.getAttribute(a) === 'true')}));
    return [];
  };
  const kindOf = (c, ch) => {
    if (c.tagName === 'SELECT') return 'select';
    const t = ch[0].el;
    if (t.tagName === 'LABEL' || (t.tagName === 'INPUT') || t.getAttribute('role') === 'radio') return 'radio';
    return 'buttons';
  };
  const nameOf = c => {
    if (c.getAttribute('aria-label')) return c.getAttribute('aria-label');
    const lg = c.querySelector('legend'); if (lg) return clean(lg.innerText);
    if (c.id) { const l = document.querySelector(`label[for="${CSS.escape(c.id)}"]`); if (l) return clean(l.innerText); }
    const first = [...c.querySelectorAll('span, p, div, h3, h4')].find(n => n.children.length === 0 && clean(n.innerText));
    return first ? clean(first.innerText) : '';
  };
  const sel = 'select, fieldset, [role=radiogroup], [role=group], [role=listbox]';
  const all = [...document.querySelectorAll(sel)].filter(vis);
  const out = [];
  let g = 0, o = 0;
  for (const c of all) {
    if (all.some(d => d !== c && c.contains(d) && choicesIn(d).length >= 2)) continue;  // innermost only
    const ch = choicesIn(c);
    if (ch.length < 2 || ch.length > 60) continue;
    if (c.closest('form') && c.closest('form').querySelector('input[type=email], textarea')) continue;  // contact forms
    const gid = 'g' + (g++);
    c.setAttribute('data-rc-gid', gid);
    const kind = kindOf(c, ch);
    const first = ch[0].el;
    let selected_css, unavailable_css;
    if (kind === 'select') { selected_css = ':checked'; unavailable_css = ':disabled'; }
    else if (first.tagName === 'LABEL') { selected_css = ':has(input:checked)'; unavailable_css = ':has(input:disabled)'; }
    else if (first.getAttribute('role') === 'radio' || first.hasAttribute('aria-checked')) { selected_css = '[aria-checked=true]'; unavailable_css = '[aria-disabled=true]'; }
    else if (first.hasAttribute('aria-pressed')) { selected_css = '[aria-pressed=true]'; unavailable_css = '[disabled], [aria-disabled=true]'; }
    else { selected_css = '[aria-selected=true], [aria-pressed=true], .selected, .is-selected, .active'; unavailable_css = '[disabled], [aria-disabled=true]'; }
    const ctx = [];
    for (let a = c.parentElement; a && a !== document.body && ctx.length < 5; a = a.parentElement) {
      const cls = (typeof a.className === 'string' ? a.className : '').split(/\s+/).filter(Boolean)
                    .map(x => x.replace(/__[A-Za-z0-9_-]{4,}$/, '')).slice(0, 2).join('.');
      if (cls || a.getAttribute('aria-label')) ctx.push(a.tagName.toLowerCase() + (cls ? '.' + cls : '') + (a.getAttribute('aria-label') ? `[${a.getAttribute('aria-label')}]` : ''));
    }
    out.push({gid, kind, name: nameOf(c), selected_css, unavailable_css, context: ctx.join(' < '),
              options: ch.map(x => { x.el.setAttribute('data-rc-oid', 'o' + (o++)); return {oid: x.el.getAttribute('data-rc-oid'), label: x.label, disabled: !!x.disabled, selected: !!x.selected}; })});
  }
  return out;
}
"""


def find_option_groups(page: Page) -> list[dict]:
    """Option pickers parsed from the DOM, with values, availability and selector candidates."""
    groups = page.evaluate(_GROUPS_JS)
    for g in groups:
        container = page.locator(f'[data-rc-gid="{g["gid"]}"]')
        g["group_candidates"] = _css_candidates(page, container.element_handle())
        first = page.locator(f'[data-rc-oid="{g["options"][0]["oid"]}"]').element_handle()
        g["option_candidates"] = _css_candidates(container, first)
        g["values"] = [o["label"] for o in g["options"]]
    return groups


_LEAVES_JS = r"""
([rootSel, prefer]) => {
  document.querySelectorAll('[data-rc-t]').forEach(e => e.removeAttribute('data-rc-t'));
  const root = rootSel ? document.querySelector(rootSel) : document.body;
  if (!root) return [];
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const own = e => [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ');
  const out = [];
  let n = 0;
  for (const e of root.querySelectorAll('*')) {
    if (['SCRIPT', 'STYLE', 'SVG', 'PATH', 'IMG', 'NOSCRIPT'].includes(e.tagName) || !vis(e)) continue;
    const text = clean(own(e));
    const control = ['INPUT', 'SELECT', 'BUTTON'].includes(e.tagName) || e.getAttribute('role') === 'button'
                    || (e.children.length <= 2 && e.querySelector('svg') && /(increase|decrease|plus|minus|add|remove|delete|qty|quantity)/i.test(e.className));
    if (!text && !control) continue;
    if (text.length > 160) continue;
    const id = 't' + (n++);
    e.setAttribute('data-rc-t', id);
    const cls = (typeof e.className === 'string' ? e.className : '').slice(0, 80);
    const label = e.getAttribute('aria-label') || '';
    const value = e.tagName === 'INPUT' && ['number', 'text'].includes(e.type) && /qty|quant/i.test(e.className + label + e.name) ? e.value : '';
    out.push({id, tag: e.tagName.toLowerCase(), text, label, cls, value,
              preferred: prefer ? !!e.closest(prefer) : false});
  }
  return out;
}
"""


def text_leaves(page: Page, root_css: str | None = None, prefer_css: str | None = None) -> list[dict]:
    return page.evaluate(_LEAVES_JS, [root_css, prefer_css])


def _leaf_desc(leaf: dict) -> str:
    bits = [f"<{leaf['tag']}>"]
    if leaf["text"]:
        bits.append(repr(leaf["text"][:80]))
    if leaf["label"]:
        bits.append(f"aria-label={leaf['label']!r}")
    if leaf["value"]:
        bits.append(f"value={leaf['value']!r}")
    if leaf["cls"]:
        bits.append(f"class={leaf['cls'][:60]!r}")
    return " ".join(bits)


_CONTAINER_JS = r"""
([attr, id, mode]) => {
  let e = document.querySelector(`[${attr}="${id}"]`);
  if (!e) return null;
  const sig = n => n.tagName + '|' + ((typeof n.className === 'string' ? n.className : '').split(/\s+/)[0] || '');
  for (let depth = 0; e && e !== document.body && depth < 10; depth++, e = e.parentElement) {
    if (depth === 0) continue;
    const t = e.tagName;
    const isItem = t === 'LI' || t === 'ARTICLE' || e.getAttribute('role') === 'listitem'
                   || /(card|item|line|product-tile|result)/i.test(typeof e.className === 'string' ? e.className : '');
    if (mode === 'repeating') {
      const parent = e.parentElement;
      const peers = parent ? [...parent.children].filter(s => sig(s) === sig(e)).length : 0;
      const pageWide = [...document.querySelectorAll(e.tagName)].filter(s => sig(s) === sig(e)).length;
      if (isItem && (peers >= 2 || pageWide >= 2)) { e.setAttribute('data-rc-c', 'c0'); return true; }
    } else if (isItem && /[$€£]\s?\d/.test(e.innerText)) {  // a cart line also shows its price
      e.setAttribute('data-rc-c', 'c0'); return true;
    }
  }
  return null;
}
"""


def _container_for(page: Page, attr: str, element_id: str, mode: str) -> Locator | None:
    page.evaluate("() => document.querySelectorAll('[data-rc-c]').forEach(e => e.removeAttribute('data-rc-c'))")
    if not page.evaluate(_CONTAINER_JS, [attr, element_id, mode]):
        return None
    return page.locator('[data-rc-c="c0"]')


# ---------------------------------------------------------------------------------------------
# Page-kind question builders
# ---------------------------------------------------------------------------------------------

def _options(elements: list[ElementInfo], keep) -> dict[str, str]:
    opts = {e.id: e.brief()[:160] for e in elements if keep(e)}
    opts = dict(list(opts.items())[:MAX_OPTIONS])
    opts["none"] = "none of these"
    return opts


def _is_button(e: ElementInfo) -> bool:
    return e.tag == "button" or e.role == "button" or (e.tag == "input" and e.attrs.get("type") in ("submit", "button"))


def _is_heading(e: ElementInfo) -> bool:
    return e.tag in ("h1", "h2", "h3") or e.role == "heading"


def _has_price(e: ElementInfo) -> bool:
    return bool(re.search(r"[$€£]\s?\d|\d[.,]\d{2}", e.name or e.text)) and len(e.name or e.text) < 40


def _is_input(e: ElementInfo) -> bool:
    return e.tag in ("input", "select", "textarea") or e.role in ("textbox", "searchbox", "spinbutton", "combobox")


def _ref(page: Page, pick: tuple[str | None, float], elements: list[ElementInfo]) -> dict | None:
    eid, conf = pick
    if not eid:
        return None
    el = next((e for e in elements if e.id == eid), None)
    if el is None:
        return {"element_id": eid, "label": "unknown element", "confidence": conf, "candidates": []}
    return {"element_id": eid, "label": el.name or el.text, "confidence": conf,
            "candidates": candidate_locators(page, eid, elements)}


def _leaf_ref(scope: Page | Locator, page: Page, pick: tuple[str | None, float], leaves: list[dict]) -> dict | None:
    lid, conf = pick
    leaf = next((x for x in leaves if x["id"] == lid), None)
    if not leaf:
        return None
    handle = page.locator(f'[data-rc-t="{lid}"]').element_handle()
    return {"element_id": lid, "label": leaf["label"] or leaf["text"] or leaf["cls"][:40], "confidence": conf,
            "candidates": _css_candidates(scope, handle)}


def _ask(llm: LLM, purpose: str, state: str, questions: dict) -> SystemOneReply:
    merged: SystemOneReply | None = None
    for batch in batch_questions(state, questions):
        r = llm.ask(purpose=purpose, state=state, questions=batch)
        if merged is None:
            merged = r
        else:
            merged.answers.update(r.answers)
            merged.tokens_in = (merged.tokens_in or 0) + (r.tokens_in or 0)
    return merged


def _discover_landing(page, llm, elements, state) -> dict:
    qs = {
        "search_input": choice_q("Which element is the store's product search text box?", _options(elements, _is_input)),
        "search_open_button": choice_q("Which element opens the product search (often a magnifier icon)?",
                                       _options(elements, lambda e: _is_button(e) or e.tag == "a")),
        "cart_link": choice_q("Which element opens the shopping cart?", _options(elements, lambda e: _is_button(e) or e.tag == "a")),
        "cookie_accept": choice_q("Which element accepts or closes the cookie-consent banner?", _options(elements, _is_button)),
        "login_wall": noul_q("The page blocks shopping until the visitor signs in (a login wall), "
                             "as opposed to merely showing a 'sign in' link."),
    }
    r = _ask(llm, "discover:landing", state, qs)
    out = {k: _ref(page, choice(r, k), elements) for k in qs if k != "login_wall"}
    out["login_wall_probability"] = noul(r, "login_wall")
    return out, r


def _discover_product(page, llm, elements, state) -> dict:
    groups = find_option_groups(page)
    qs = {
        "title": choice_q("Which element is the main product's title?", _options(elements, lambda e: _is_heading(e) or e.tag == "h1")),
        "price": choice_q("Which element shows the main product's price?", _options(elements, _has_price)),
        "add_to_cart": choice_q("Which element is the main product's add-to-cart (or add-to-bag) button? A 'sign in' "
                                "link in the header does not count.", _options(elements, _is_button)),
        "quantity_input": choice_q("Which element sets the quantity of the main product?", _options(elements, _is_input)),
        "cart_link": choice_q("Which element opens the shopping cart?", _options(elements, lambda e: _is_button(e) or e.tag == "a")),
        "added_signal": choice_q("Which element is an 'added to cart' confirmation message?",
                                 _options(elements, lambda e: "added" in (e.name + e.text).lower())),
    }
    for g in groups[:20]:
        qs[f"group_{g['gid']}"] = noul_q(
            f"The option picker {g['name']!r} with choices {', '.join(g['values'][:12])}, located inside "
            f"{g['context'] or 'the page body'!r}, is a choice the shopper makes for the MAIN product on this page "
            "(not part of a recommended-products carousel, a product card, or a form).")
    r = _ask(llm, "discover:product", state, qs)
    out = {k: _ref(page, choice(r, k), elements) for k in
           ("title", "price", "add_to_cart", "quantity_input", "cart_link", "added_signal")}
    out["option_groups"] = []
    for g in groups[:20]:
        g["is_main_product"] = noul(r, f"group_{g['gid']}")
        seen = {x["name"].casefold() for x in out["option_groups"]}
        if g["is_main_product"] >= 0.5 and g["name"].casefold() not in seen:  # first in DOM = main product
            out["option_groups"].append(g)
    out["rejected_groups"] = [{"name": g["name"], "values": g["values"][:8], "is_main_product": g.get("is_main_product")}
                              for g in groups[:20] if g.get("is_main_product", 0) < 0.5]
    return out, r


def _discover_search(page, llm, elements, state) -> dict:
    links = lambda e: e.tag == "a" and bool(e.attrs.get("href"))  # noqa: E731
    r1 = _ask(llm, "discover:search", state, {"first_result_link": choice_q(
        "Which link opens the FIRST product in the search results (not navigation, not a category)?",
        _options(elements, links))})
    out: dict = {"result_item": None, "result_title": None, "result_link": None}
    link = _ref(page, choice(r1, "first_result_link"), elements)
    if not link:
        return out, r1
    out["result_link_first"] = link
    card = _container_for(page, "data-rc-id", link["element_id"], "repeating")
    if card is None:
        return out, r1
    out["result_item"] = {"label": card.inner_text()[:80], "candidates": _css_candidates(page, card.element_handle())}
    out["result_link"] = {"label": link["label"], "candidates": _css_candidates(card, by_rc_id(page, link["element_id"]).element_handle())}
    leaves = text_leaves(page, '[data-rc-c="c0"]')
    r2 = _ask(llm, "discover:search:card", page_state(page.url, elements),
              {"result_title": choice_q("Inside the first search result card, which text is the product's name?",
                                        {**{x["id"]: _leaf_desc(x) for x in leaves[:MAX_OPTIONS]}, "none": "none"})})
    out["result_title"] = _leaf_ref(card, page, choice(r2, "result_title"), leaves)
    r1.answers.update(r2.answers)
    return out, r1


_CART_HINT = '[class*="cart" i], [id*="cart" i], [role=dialog], aside'


def _discover_cart(page, llm, elements, state) -> dict:
    leaves = text_leaves(page, None, _CART_HINT)
    ranked = sorted(leaves, key=lambda x: not x["preferred"])[:MAX_OPTIONS]
    leaf_state = (f"URL: {page.url}\nText elements of the page (cart area first):\n" + "\n".join(
        f"[{x['id']}] {_leaf_desc(x)}" + (" (inside cart area)" if x["preferred"] else "") for x in ranked)
    )[:MAX_STATE_CHARS]
    r1 = _ask(llm, "discover:cart", leaf_state, {"line_title": choice_q(
        "Which text is the product name of the FIRST item line in the shopping cart (not a recommendation)?",
        {**{x["id"]: _leaf_desc(x) for x in ranked}, "none": "none"})})
    out: dict = {k: None for k in ("line_item", "line_title", "line_options", "line_quantity", "line_price",
                                   "quantity_control", "quantity_increase")}
    tid, conf = choice(r1, "line_title")
    if not tid:
        return out, r1
    line = _container_for(page, "data-rc-t", tid, "item")
    if line is None:
        return out, r1
    out["line_item"] = {"label": line.inner_text()[:80], "confidence": conf,
                        "candidates": _css_candidates(page, line.element_handle())}
    out["line_title"] = _leaf_ref(line, page, (tid, conf), leaves)
    parts = text_leaves(page, '[data-rc-c="c0"]')
    opts = {**{x["id"]: _leaf_desc(x) for x in parts[:MAX_OPTIONS]}, "none": "none of these"}
    qs = {
        "line_price": choice_q("In this cart line, which element shows the price?", opts),
        "line_quantity": choice_q("In this cart line, which element shows the quantity (a number or a quantity box)?", opts),
        "line_options": choice_q("In this cart line, which element shows the chosen options/variant (size, color...)?", opts),
        "quantity_increase": choice_q("In this cart line, which control increases the quantity by one (a plus button)?", opts),
        "quantity_control": choice_q("In this cart line, which element is a text/number box to type the quantity into?", opts),
    }
    r2 = _ask(llm, "discover:cart:line", leaf_state, qs)
    for k in qs:
        out[k] = _leaf_ref(line, page, choice(r2, k), parts)
    r1.answers.update(r2.answers)
    return out, r1


_KINDS = {"landing": _discover_landing, "product": _discover_product, "search": _discover_search, "cart": _discover_cart}


def discover_page(page: Page, llm: LLM, kind: str) -> dict:
    elements = snapshot(page, 400)
    _SNAP[id(page)] = elements
    try:
        data, reply = _KINDS[kind](page, llm, elements, page_state(page.url, elements))
    finally:
        _SNAP.pop(id(page), None)
    data["page_url"] = page.url
    data["page_kind"] = kind
    data["djev_answers"] = reply.answers
    data["tokens_in"] = reply.tokens_in
    return data


# ---------------------------------------------------------------------------------------------
# CLI driver
# ---------------------------------------------------------------------------------------------

def sanitize_html(html: str) -> str:
    html = re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style\b.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r"<svg\b.*?</svg>", "<svg/>", html, flags=re.S | re.I)
    html = re.sub(r"""\svalue\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", "", html, flags=re.I)
    return re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[redacted]", html)


def riot_policy(origin: str, prefix: str):
    return SimpleNamespace(allowed_origins=[origin], allowed_path_prefixes=[prefix], blockers=Blockers())


def save(page: Page, proposal: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "proposal.json").write_text(json.dumps(proposal, indent=2), encoding="utf-8")
    (out / "page.html").write_text(sanitize_html(page.content()), encoding="utf-8")


def _settle(page: Page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except PlaywrightError:
        pass
    page.wait_for_timeout(1_000)


def run_discovery(page: Page, llm: LLM, policy, url: str, kind: str, out: Path,
                  search_term: str | None = None, explore_cart: bool = False) -> dict:
    page.goto(url, wait_until="domcontentloaded")
    _settle(page)
    summary: dict = {"url": page.url, "kind": kind, "blocker": None, "outputs": []}
    blocker = detect_blocker(page, policy)
    if blocker:
        summary["blocker"] = str(blocker)
    proposal = discover_page(page, llm, kind)
    save(page, proposal, out)
    summary["outputs"].append(str(out / "proposal.json"))

    if kind == "landing" and search_term:
        box = proposal.get("search_input")
        if not box and proposal.get("search_open_button"):
            safe_click(by_rc_id(page, proposal["search_open_button"]["element_id"]))
            page.wait_for_timeout(800)
            snap = snapshot(page, 400)
            box = next(({"element_id": e.id} for e in snap if e.role == "searchbox" or e.attrs.get("type") == "search"), None)
        if not box:
            summary["error"] = "no search input found"
            return summary
        by_rc_id(page, box["element_id"]).fill(search_term)
        page.keyboard.press("Enter")
        _settle(page)
        summary["search_url"] = page.url
        save(page, discover_page(page, llm, "search"), out / "search")
        summary["outputs"].append(str(out / "search" / "proposal.json"))

    if kind == "product" and explore_cart:
        for g in proposal.get("option_groups") or []:
            first = next((o for o in g["options"] if not o["disabled"]), None)
            if first is None:
                continue
            if g["kind"] == "select":
                page.locator(f'[data-rc-gid="{g["gid"]}"]' if "gid" in g else "select").first.select_option(label=first["label"])
            else:
                opt = page.locator(f'[data-rc-oid="{first["oid"]}"]')
                if opt.count():
                    safe_click(opt)
        if proposal.get("add_to_cart"):
            safe_click(by_rc_id(page, proposal["add_to_cart"]["element_id"]))
            page.wait_for_timeout(3_000)
        cart = proposal.get("cart_link")
        href = by_rc_id(page, cart["element_id"]).first.get_attribute("href") if cart else None
        cart_url = urljoin(page.url, href) if href else None
        if cart_url and is_allowed_url(cart_url, policy):
            page.goto(cart_url, wait_until="domcontentloaded")
        elif cart:  # a cart drawer; Riot doesn't open it on add
            safe_click(by_rc_id(page, cart["element_id"]))
        _settle(page)
        summary["cart_url"] = page.url
        save(page, discover_page(page, llm, "cart"), out / "cart")
        summary["outputs"].append(str(out / "cart" / "proposal.json"))
    return summary
