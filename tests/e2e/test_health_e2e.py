from playwright.sync_api import Page


def test_health_endpoint_via_browser(page: Page, app_server: str) -> None:
    response = page.goto(f"{app_server}/health")
    assert response is not None
    assert response.status == 200
    assert response.json() == {"status": "ok"}
