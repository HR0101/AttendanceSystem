"""Playwright のライフサイクル。"""

from contextlib import contextmanager


@contextmanager
def open_page(config: dict):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config["browser"]["headless"])
        try:
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(config["browser"]["timeout_ms"])
            yield page
        finally:
            browser.close()
