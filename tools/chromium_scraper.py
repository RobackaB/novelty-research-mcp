"""Pomocné funkcie na načítanie webových stránok cez Playwright."""

from __future__ import annotations

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from .output_cleaner import USER_AGENT


async def fetch_page_html_and_text(
    url: str,
    timeout_ms: int = 60000,
    wait_until: str = "networkidle",
    settle_ms: int = 0,
) -> tuple[str, str]:
    """Načíta stránku v prehliadači bez grafického rozhrania a vráti jej HTML aj text."""
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
        )
        page = await context.new_page()

        async def handle_route(route) -> None:
            """Preskočí obrázky, font a médiá, aby sa stránka načítala rýchlejšie."""
            if route.request.resource_type in {"image", "font", "media"}:
                await route.abort()
                return
            await route.continue_()

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
    """Načíta stránku a vráti iba jej HTML obsah."""
    html, _ = await fetch_page_html_and_text(url, timeout_ms=timeout_ms)
    return html


__all__ = ["PlaywrightTimeoutError", "fetch_page_html", "fetch_page_html_and_text"]
