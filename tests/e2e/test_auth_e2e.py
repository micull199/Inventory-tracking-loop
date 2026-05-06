"""End-to-end auth flow: anonymous landing → dev login → pending page."""

from __future__ import annotations

from playwright.sync_api import Page, expect


def test_anonymous_visitor_sees_sign_in(page: Page, app_server: str) -> None:
    page.goto(f"{app_server}/")
    expect(page.get_by_test_id("sign-in")).to_be_visible()


def test_dev_login_lands_pending_user_on_pending_page(
    page: Page, app_server: str
) -> None:
    """Use the test-only login backdoor; verify the pending holding page renders."""
    # Submit a tiny HTML form that posts to the dev-login endpoint, then redirects to /.
    page.set_content(
        f"""<form id="f" method="post" action="{app_server}/auth/_dev-login">
              <input name="email" value="newbie@example.com">
              <input name="name" value="New Bie">
              <input name="sub" value="g-e2e-newbie">
            </form>"""
    )
    page.evaluate("document.getElementById('f').submit()")
    page.wait_for_url(f"{app_server}/")

    expect(page.get_by_test_id("pending-heading")).to_be_visible()
    expect(page.get_by_test_id("user-status")).to_have_text("pending")
