"""Helpers for fetching web pages through Playwright."""

from __future__ import annotations
from typing import Callable

from .output_cleaner import USER_AGENT

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
except ImportError:  # Playwright is optional; the fallback class keeps the same interface.
    class PlaywrightTimeoutError(Exception):
        """Substitute exception used when Playwright is not installed."""


async def fetch_page_html_and_text(
    url: str,
    timeout_ms: int = 60000,
    wait_until: str = "networkidle",
    settle_ms: int = 0,
    *, allowed_url: Callable[[str], bool] | None = None,
) -> tuple[str, str]:
    """Load a page in a headless browser and return both its HTML and its text."""
    if allowed_url is not None and not allowed_url(url):
        raise ValueError("Unsafe document target rejected.")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed; Chromium-based fetching is unavailable. "
            "Install the 'playwright' package and run 'playwright install chromium'."
        ) from exc

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            **({"service_workers": "block"} if allowed_url else {}),
        )
        page = await context.new_page()

        async def handle_route(route) -> None:
            """Skip images, fonts and media so the page loads faster."""
            if (allowed_url is not None and not allowed_url(route.request.url)) or route.request.resource_type in {"image", "font", "media"}:
                await route.abort()
                return
            if allowed_url is not None:
                # Browser routing need not re-run for a redirect. Never let a
                # redirect bypass the patent host policy via continue_().
                response = await route.fetch(max_redirects=0, timeout=timeout_ms)
                if 300 <= response.status < 400:
                    await route.abort()
                else:
                    await route.fulfill(response=response)
                return
            await route.continue_()

        if allowed_url is not None:
            await context.route("**/*", handle_route)
        else:
            await page.route("**/*", handle_route)
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            if settle_ms > 0:
                await page.wait_for_timeout(settle_ms)
            return await page.content(), await page.locator("body").inner_text(timeout=5000)
        finally:
            await context.close()
            await browser.close()


async def fetch_page_html(url: str, timeout_ms: int = 60000) -> str:
    """Load a page and return only its HTML content."""
    html, _ = await fetch_page_html_and_text(url, timeout_ms=timeout_ms)
    return html


__all__ = ["PlaywrightTimeoutError", "fetch_page_html", "fetch_page_html_and_text"]
