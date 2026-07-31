#!/usr/bin/env python3
"""Dump homepage controls so the mode/model selectors can be driven by DOM (US-4/US-6).

    python spike/probe_dom.py
"""

import sys

from playwright.sync_api import sync_playwright

from capture import attach, launch_chrome  # noqa: E402


def main() -> None:
    launch_chrome()
    with sync_playwright() as p:
        ctx, page = attach(p)
        page.goto("https://www.perplexity.ai/")
        page.wait_for_timeout(4000)
        if "Just a moment" in page.title():
            sys.exit("challenged")
        controls = page.evaluate("""() =>
            [...document.querySelectorAll('button,[role=button],[role=combobox],[role=tab]')]
              .map(e => ({
                 tag: e.tagName,
                 role: e.getAttribute('role'),
                 aria: e.getAttribute('aria-label'),
                 testid: e.getAttribute('data-testid'),
                 text: (e.innerText || '').trim().slice(0, 40),
              }))
              .filter(c => c.aria || c.testid || c.text)
        """)
        for c in controls:
            print(c)
        print(f"\n{len(controls)} controls")

        # Open the model picker -- "Deep research" (pplx_alpha) is expected inside it.
        page.get_by_role("button", name="Model", exact=True).first.click()
        page.wait_for_timeout(1500)
        menu = page.evaluate("""() =>
            [...document.querySelectorAll('[role=menuitem],[role=option],[role=menuitemradio]')]
              .map(e => (e.innerText || '').trim().replace(/\\n/g, ' | ').slice(0, 70))
        """)
        print("\n--- model picker ---")
        for m in menu:
            print(" ", m)
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)

        for label in ("Search", "Add files or tools"):
            try:
                page.get_by_role("button", name=label, exact=True).first.click()
            except Exception as e:
                print(f"\n--- {label}: {type(e).__name__} ---")
                continue
            page.wait_for_timeout(1500)
            items = page.evaluate("""() =>
                [...document.querySelectorAll('[role=menuitem],[role=option],[role=menuitemradio],[role=switch]')]
                  .map(e => (e.innerText || '').trim().replace(/\\n/g, ' | ').slice(0, 70))
            """)
            print(f"\n--- {label} menu ---")
            for m in items:
                print(" ", m)
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)


if __name__ == "__main__":
    main()
