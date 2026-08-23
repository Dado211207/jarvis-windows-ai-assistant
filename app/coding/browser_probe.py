"""The JavaScript that runs inside the page under test.

Separated from `browser_qa.py` for one reason: a reviewer asking "what
code does JARVIS inject into the user's page" should be able to read all
of it in one file, and a reviewer asking "can this reach a foreign
origin" should not have to scroll past four hundred lines of JavaScript
to find out.

**Everything here reads; nothing writes.** No expression mutates the
document, sets a cookie, calls `fetch`, or touches storage. The one
apparent exception — `checkOverflow` — changes only the *emulated*
viewport, which CDP applies from outside the page.

**Every scan is bounded.** A page with a hundred thousand nodes must not
make a QA check take a minute, so each traversal stops at
`MAX_ELEMENTS_SCANNED` and says that it did. A truncated scan reports
`scanned_all: false`; it never reports a clean result it did not earn.

**The accessibility check is a named, fixed rule set, not an audit.**
It is nine deterministic structural rules — the kind a linter catches —
and `ACCESSIBILITY_RULES` is what the UI displays so the page cannot
claim more than the code checks. It is deliberately *not* described as
an axe-core audit: axe-core is a Node package the packaged Windows build
does not carry, and reporting "0 accessibility violations" from a
nine-rule check while implying a full audit would be the same species of
dishonesty as reporting zero console errors for a page nothing opened.
"""

from __future__ import annotations

MAX_ELEMENTS_SCANNED = 4000
MAX_FINDINGS_PER_RULE = 10

#: What the accessibility check actually looks at, in the order it reports.
#: The UI renders this list; it is the contract for what "no findings"
#: means.
ACCESSIBILITY_RULES = (
    ("html-has-lang", "The page declares a language"),
    ("document-has-title", "The page has a non-empty title"),
    ("one-h1", "The page has exactly one top-level heading"),
    ("heading-order", "Heading levels increase by one at a time"),
    ("image-alt", "Every image has an alt attribute"),
    ("control-has-name", "Every form control has an accessible name"),
    ("button-has-name", "Every button and link has accessible text"),
    ("iframe-has-title", "Every frame has a title"),
    ("duplicate-id", "No id is used twice"),
)

# Shared preamble. `J` collects elements with a bound, and reports whether
# it reached the end — the honesty rule above, in three lines.
_PREAMBLE = f"""
const LIMIT = {MAX_ELEMENTS_SCANNED};
const CAP = {MAX_FINDINGS_PER_RULE};
const all = Array.prototype.slice.call(document.querySelectorAll('*'), 0, LIMIT);
const scannedAll = document.querySelectorAll('*').length <= LIMIT;
const label = (el) => {{
  const tag = el.tagName ? el.tagName.toLowerCase() : '?';
  const id = el.id ? '#' + el.id : '';
  const cls = (typeof el.className === 'string' && el.className.trim())
    ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.') : '';
  return (tag + id + cls).slice(0, 80);
}};
"""

#: Title, language, heading structure and image health.
PAGE_FACTS_JS = "(() => {" + _PREAMBLE + r"""
  const images = Array.prototype.slice.call(
    document.querySelectorAll('img'), 0, LIMIT);
  const broken = [];
  // Counted separately from the examples: `broken` is capped at CAP so a
  // page with fifty broken images does not produce a fifty-line report,
  // and reporting the capped length as the count would under-report the
  // defect the check exists to find.
  let brokenCount = 0;
  for (const img of images) {
    const noSource = !img.getAttribute('src') && !img.getAttribute('srcset');
    // `complete` is true for a finished load *and* for a failed one; a
    // natural width of zero is what separates them. An image that has not
    // finished yet is neither, and is not reported either way.
    const failed = img.complete && img.naturalWidth === 0;
    if (noSource || failed) {
      brokenCount += 1;
      if (broken.length < CAP) {
        broken.push(label(img) + ' ' + String(img.currentSrc || img.getAttribute('src') || '(no src)').slice(0, 120));
      }
    }
  }
  const headings = Array.prototype.slice.call(
    document.querySelectorAll('h1,h2,h3,h4,h5,h6'), 0, LIMIT)
    .map((h) => Number(h.tagName.substring(1)));
  return {
    title: String(document.title || '').slice(0, 200),
    lang: String(document.documentElement.getAttribute('lang') || ''),
    h1_count: document.querySelectorAll('h1').length,
    heading_levels: headings.slice(0, 60),
    image_count: images.length,
    broken_images: brokenCount,
    broken_image_labels: broken,
    element_count: document.querySelectorAll('*').length,
    scanned_all: scannedAll,
    has_meta_refresh: !!document.querySelector('meta[http-equiv="refresh" i]'),
  };
})()"""

#: Does anything stick out sideways at the current emulated width?
OVERFLOW_JS = "(() => {" + _PREAMBLE + r"""
  const doc = document.documentElement;
  const scroll = doc.scrollWidth;
  const client = doc.clientWidth;
  // One pixel of slack: sub-pixel layout rounding otherwise reports
  // overflow on pages that have none.
  const overflows = scroll > client + 1;
  const culprits = [];
  if (overflows) {
    for (const el of all) {
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) continue;
      if (rect.right > client + 1 || rect.left < -1) {
        if (culprits.length < CAP) {
          culprits.push(label(el) + ' → ' + Math.round(rect.right) + 'px');
        }
      }
    }
  }
  return {
    overflows: overflows,
    scroll_width: scroll,
    client_width: client,
    culprits: culprits,
    scanned_all: scannedAll,
  };
})()"""

#: The nine rules named in `ACCESSIBILITY_RULES`, and nothing else.
ACCESSIBILITY_JS = "(() => {" + _PREAMBLE + r"""
  const findings = [];
  const add = (rule, detail) => {
    if (findings.length < CAP * 3) findings.push({rule: rule, detail: String(detail).slice(0, 160)});
  };
  const nameOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    const ref = el.getAttribute('aria-labelledby');
    if (ref) {
      const target = document.getElementById(ref.split(/\s+/)[0]);
      if (target && target.textContent.trim()) return target.textContent.trim();
    }
    const title = el.getAttribute('title');
    if (title && title.trim()) return title.trim();
    if (el.id) {
      const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lbl && lbl.textContent.trim()) return lbl.textContent.trim();
    }
    if (el.closest && el.closest('label')) {
      const wrap = el.closest('label');
      if (wrap.textContent.trim()) return wrap.textContent.trim();
    }
    if (el.textContent && el.textContent.trim()) return el.textContent.trim();
    const img = el.querySelector ? el.querySelector('img[alt]') : null;
    if (img && img.getAttribute('alt').trim()) return img.getAttribute('alt').trim();
    return '';
  };

  if (!document.documentElement.getAttribute('lang')) {
    add('html-has-lang', '<html> has no lang attribute');
  }
  if (!String(document.title || '').trim()) {
    add('document-has-title', 'the document has no title');
  }

  const h1s = document.querySelectorAll('h1').length;
  if (h1s !== 1) add('one-h1', h1s + ' <h1> element(s); expected exactly 1');

  let previous = 0;
  for (const h of Array.prototype.slice.call(
      document.querySelectorAll('h1,h2,h3,h4,h5,h6'), 0, LIMIT)) {
    const level = Number(h.tagName.substring(1));
    if (previous && level > previous + 1) {
      add('heading-order', 'h' + previous + ' is followed by h' + level);
    }
    previous = level;
  }

  for (const img of Array.prototype.slice.call(document.querySelectorAll('img'), 0, LIMIT)) {
    if (img.getAttribute('alt') === null) add('image-alt', label(img) + ' has no alt attribute');
  }

  for (const el of Array.prototype.slice.call(
      document.querySelectorAll('input,select,textarea'), 0, LIMIT)) {
    const type = String(el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden' || type === 'submit' || type === 'button' || type === 'reset') continue;
    if (!nameOf(el)) add('control-has-name', label(el) + ' has no accessible name');
  }

  for (const el of Array.prototype.slice.call(
      document.querySelectorAll('button,a[href],[role="button"]'), 0, LIMIT)) {
    if (!nameOf(el)) add('button-has-name', label(el) + ' has no accessible text');
  }

  for (const el of Array.prototype.slice.call(document.querySelectorAll('iframe'), 0, LIMIT)) {
    if (!String(el.getAttribute('title') || '').trim()) {
      add('iframe-has-title', label(el) + ' has no title');
    }
  }

  const seen = Object.create(null);
  for (const el of all) {
    if (!el.id) continue;
    if (seen[el.id]) { add('duplicate-id', 'id "' + String(el.id).slice(0, 60) + '" is used more than once'); }
    seen[el.id] = true;
  }

  return {findings: findings, scanned_all: scannedAll};
})()"""

#: With `prefers-reduced-motion: reduce` emulated, is anything still moving?
REDUCED_MOTION_JS = "(() => {" + _PREAMBLE + r"""
  const emulated = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const moving = [];
  let movingCount = 0;   // counted separately; `moving` holds examples only
  const seconds = (value) => String(value || '')
    .split(',')
    .map((v) => {
      const t = v.trim();
      if (t.endsWith('ms')) return parseFloat(t) / 1000;
      if (t.endsWith('s')) return parseFloat(t);
      return 0;
    })
    .reduce((a, b) => Math.max(a, isNaN(b) ? 0 : b), 0);

  for (const el of all) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    const animated = style.animationName !== 'none' && seconds(style.animationDuration) > 0.01;
    const transitioned = seconds(style.transitionDuration) > 0.01;
    if (animated || transitioned) {
      movingCount += 1;
      if (moving.length < CAP) {
        moving.push(label(el) + (animated ? ' (animation)' : ' (transition)'));
      }
    }
  }
  return {
    emulated: emulated,
    still_animating: movingCount,
    examples: moving,
    respects_reduced_motion: movingCount === 0,
    scanned_all: scannedAll,
  };
})()"""
