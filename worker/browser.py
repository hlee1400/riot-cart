"""Guarded Playwright layer: launch, navigation guard, page snapshot, locator resolution, safe clicks."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from playwright.sync_api import Browser, BrowserContext, Locator, Page, Playwright, sync_playwright

from worker.models import Status
from worker.recipe import CssLocator, Recipe, RoleLocator
from worker.safety import is_allowed_url, is_forbidden_label

log = logging.getLogger("worker.browser")

VIEWPORT = {"width": 1366, "height": 900}
STEP_TIMEOUT_MS = 15_000


class UnsafeAction(Exception):
    pass


def launch(headed: bool) -> tuple[Playwright, Browser]:
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=not headed)
    return pw, browser


def new_context(browser: Browser) -> BrowserContext:
    major = browser.version.split(".")[0]
    ua = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          f"Chrome/{major}.0.0.0 Safari/537.36")
    ctx = browser.new_context(user_agent=ua, viewport=VIEWPORT, locale="en-US")
    ctx.set_default_timeout(STEP_TIMEOUT_MS)
    return ctx


def install_guard(context: BrowserContext, recipe: Recipe) -> list[str]:
    """Abort main-frame navigations outside the recipe's allowlist. Register after any test routes."""
    blocked: list[str] = []

    def handler(route):
        req = route.request
        if req.is_navigation_request() and req.frame.parent_frame is None and not is_allowed_url(req.url, recipe):
            blocked.append(req.url)
            log.warning("blocked navigation to %s", req.url)
            return route.abort("blockedbyclient")
        return route.fallback()

    context.route("**/*", handler)
    return blocked


_POLICY: Recipe | None = None


def set_policy(recipe: Recipe) -> None:
    global _POLICY
    _POLICY = recipe


@dataclass
class ElementInfo:
    id: str
    tag: str
    role: str | None
    name: str
    text: str
    attrs: dict[str, str] = field(default_factory=dict)
    disabled: bool = False
    selected: bool = False

    def brief(self) -> str:
        bits = [f"[{self.id}] <{self.tag}>", f"role={self.role}" if self.role else "", repr(self.name)]
        if self.text and self.text != self.name:
            bits.append(f"text={self.text!r}")
        if self.attrs:
            bits.append(" ".join(f"{k}={v!r}" for k, v in self.attrs.items()))
        if self.disabled:
            bits.append("DISABLED")
        if self.selected:
            bits.append("SELECTED")
        return " ".join(b for b in bits if b)


_SNAPSHOT_JS = r"""
(limit) => {
  document.querySelectorAll('[data-rc-id]').forEach(e => e.removeAttribute('data-rc-id'));
  const sel = 'a,button,input,select,option,textarea,label,[role],[aria-pressed],[aria-checked],h1,[class*=price]';
  const scrub = s => (s || '').replace(/[\w.+-]+@[\w-]+\.[\w.]+/g, '[redacted]').replace(/\d{7,}/g, '[redacted]')
                               .replace(/\s+/g, ' ').trim();
  const visible = e => {
    if (e.tagName === 'OPTION') { const s = e.closest('select'); return !!s && s.getClientRects().length > 0; }
    const st = getComputedStyle(e);
    return e.getClientRects().length > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const implicit = e => {
    const t = e.tagName.toLowerCase(), ty = (e.getAttribute('type') || '').toLowerCase();
    if (t === 'a' && e.hasAttribute('href')) return 'link';
    if (t === 'button') return 'button';
    if (t === 'select') return 'combobox';
    if (t === 'option') return 'option';
    if (t === 'textarea') return 'textbox';
    if (t === 'h1') return 'heading';
    if (t === 'input') return ({number: 'spinbutton', checkbox: 'checkbox', radio: 'radio', search: 'searchbox',
                               submit: 'button', button: 'button'})[ty] || 'textbox';
    return null;
  };
  const nameOf = e => {
    if (e.getAttribute('aria-label')) return e.getAttribute('aria-label');
    const lb = e.getAttribute('aria-labelledby');
    if (lb) { const n = document.getElementById(lb); if (n) return n.innerText; }
    if (e.id) { const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`); if (l) return l.innerText; }
    if (['INPUT', 'TEXTAREA'].includes(e.tagName)) {
      const ty = (e.getAttribute('type') || '').toLowerCase();
      if (['submit', 'button'].includes(ty)) return e.value;
      return e.getAttribute('placeholder') || e.getAttribute('name') || '';
    }
    return e.innerText || e.getAttribute('title') || e.getAttribute('alt') || '';
  };
  const out = [];
  let n = 0;
  for (const e of document.querySelectorAll(sel)) {
    if (out.length >= limit) break;
    if (!visible(e)) continue;
    if (['presentation', 'none'].includes(e.getAttribute('role'))) continue;
    const id = 'e' + (n++);
    e.setAttribute('data-rc-id', id);
    const attrs = {};
    for (const a of ['id', 'name', 'type', 'aria-label', 'aria-pressed', 'aria-checked', 'aria-selected',
                     'aria-expanded', 'aria-disabled']) {
      if (e.hasAttribute(a)) attrs[a] = scrub(e.getAttribute(a)).slice(0, 60);
    }
    if (e.hasAttribute('href')) { try { attrs.href = new URL(e.href).origin + new URL(e.href).pathname; } catch (_) {} }
    if (e.className && typeof e.className === 'string') attrs.class = e.className.slice(0, 60);
    let k = 0;
    for (const a of e.attributes) {
      if (a.name.startsWith('data-') && a.name !== 'data-rc-id' && k < 3) { attrs[a.name] = scrub(a.value).slice(0, 40); k++; }
    }
    out.push({
      id, tag: e.tagName.toLowerCase(), role: e.getAttribute('role') || implicit(e),
      name: scrub(nameOf(e)).slice(0, 80), text: scrub(e.innerText).slice(0, 80), attrs,
      disabled: !!(e.disabled || e.getAttribute('aria-disabled') === 'true'),
      selected: !!(e.selected || e.checked || e.getAttribute('aria-pressed') === 'true'
                   || e.getAttribute('aria-checked') === 'true' || e.getAttribute('aria-selected') === 'true'),
    });
  }
  return out;
}
"""


def snapshot(page: Page, limit: int = 150) -> list[ElementInfo]:
    """Visible interactive elements, tagged with data-rc-id so they can be targeted later. No input values."""
    return [ElementInfo(**d) for d in page.evaluate(_SNAPSHOT_JS, limit)]


def by_rc_id(page: Page, element_id: str) -> Locator:
    return page.locator(f'[data-rc-id="{element_id}"]')


def resolve(scope: Page | Locator, target) -> Locator | None:
    for loc in target:
        if isinstance(loc, RoleLocator):
            candidate = scope.get_by_role(loc.role, name=loc.name, exact=loc.exact)
        elif isinstance(loc, CssLocator):
            candidate = scope.locator(loc.css)
        else:
            continue
        try:
            if candidate.count() >= 1:
                return candidate
        except Exception:  # invalid selector from a recipe: try the next alternative
            continue
    return None


_CLICK_INFO_JS = """
e => ({
  label: [e.getAttribute('aria-label'), e.innerText, e.value, e.getAttribute('title')].filter(Boolean).join(' '),
  href: e.closest('a[href]') ? e.closest('a[href]').href : null,
  sensitiveForm: !!(e.closest('form') && e.closest('form').querySelector(
      'input[type=password], input[autocomplete^="cc-"]')),
  hiddenChoice: e.tagName === 'INPUT' && ['radio', 'checkbox'].includes((e.type || '').toLowerCase()),
  id: e.id || null,
})
"""


def safe_click(locator: Locator, timeout: int = STEP_TIMEOUT_MS) -> None:
    el = locator.first
    info = el.evaluate(_CLICK_INFO_JS)
    if is_forbidden_label(info["label"]):
        raise UnsafeAction(f"refused to click {info['label'][:60]!r}: forbidden action")
    if info["sensitiveForm"]:
        raise UnsafeAction("refused to click inside a login/payment form")
    if info["href"] and _POLICY is not None and not is_allowed_url(info["href"], _POLICY):
        raise UnsafeAction(f"refused to follow off-store link {info['href']}")
    if info["hiddenChoice"] and not el.is_visible():
        # Styled option pickers hide the real radio/checkbox; click its label instead.
        label = el.page.locator(f'label[for="{info["id"]}"]') if info["id"] else el.locator("xpath=ancestor::label[1]")
        if label.count():
            label.first.click(timeout=timeout)
            return
    el.click(timeout=timeout)


_BLOCKER_JS = """
([loginCss, challengeCss]) => {
  const any = list => list.some(s => { try { return !!document.querySelector(s); } catch (_) { return false; } });
  // Many stores embed an invisible, passive captcha widget; only a visible challenge frame counts.
  const shown = f => {
    const r = f.getBoundingClientRect(), st = getComputedStyle(f);
    return r.width > 60 && r.height > 60 && st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
  };
  const frames = [...document.querySelectorAll('iframe')].filter(shown).map(f => f.src || '');
  const challenge = any(challengeCss) ||
    frames.some(s => /captcha|challenges\\.cloudflare|hcaptcha|recaptcha|arkoselabs/i.test(s));
  const pw = [...document.querySelectorAll('input[type=password]')].some(e => e.getClientRects().length > 0);
  return {challenge, login: any(loginCss) || pw};
}
"""


def detect_blocker(page: Page, recipe: Recipe) -> Status | None:
    r = page.evaluate(_BLOCKER_JS, [recipe.blockers.login_css, recipe.blockers.challenge_css])
    if r["challenge"]:
        return Status.VERIFICATION_REQUIRED
    if r["login"]:
        return Status.AUTH_REQUIRED
    return None
