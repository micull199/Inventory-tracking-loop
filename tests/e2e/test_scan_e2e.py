"""End-to-end walk for the camera-scan leg of DoD #3 (SC2c).

SC2a laid down the camera scaffolding. SC2b loaded ``html5-qrcode@2.3.8`` from
jsDelivr (with SRI) and wired the inline glue: on toggle-open, instantiate
``Html5Qrcode("scan-camera-viewfinder")``, ``.start({facingMode:"environment"}…)``;
on a successful decode, write the value into the keyboard input and submit
the resolve form. SC2c is the *runtime* verification — a Playwright walk
that drives a real Chromium against a real uvicorn server and pins:

1. The toggle button becomes visible on a JS-enabled page (the IIFE's
   ``navigator.mediaDevices.getUserMedia`` feature-detect succeeds — Chromium
   on ``http://127.0.0.1:port`` is a secure context, so the API is exposed).
2. Clicking the toggle starts the scanner.
3. A successful decode writes the payload into the keyboard input and
   submits the resolve form.
4. The server resolves the QR code and 303-redirects to ``/scan/item/{id}``.
5. The action picker renders on the resolved-item page.

Once this walk passes, **DoD #3 ticks** — both USB (SC1*) and camera (SC2*)
scanning legs are pinned by tests.

Camera-side strategy (Q1 in the SC2c brainstorm): we **stub** ``Html5Qrcode``
via ``context.add_init_script`` so its ``.start()`` immediately calls
``onScanSuccess(known_payload)``. We also ``context.route`` the jsDelivr CDN
URL to abort with an empty 200 — without that, the real lib script would
overwrite our stub when its ``<script>`` tag executes. The stub bypasses
the camera entirely; that's intentional. The IIFE's wiring (input write +
form submit + form action URL) is what we're verifying here. Library
integration is implicitly verified by SC2b's static-content tests (the
glue source contains the ``new Html5Qrcode(...)`` and ``.start(...)``
calls; if those didn't fire, the IIFE's ``if (typeof Html5Qrcode ===
"undefined")`` branch would run instead and surface a different error
path). A future slice could bring in a real ``--use-file-for-fake-video-
capture=*.y4m`` flag to verify the lib + camera combo end-to-end; deferred
because Y4M generation tooling adds repo weight for marginal extra value.
"""

from __future__ import annotations

from playwright.sync_api import BrowserContext, Page, expect

_E2E_QR_PAYLOAD = "E2E-CAM-PAYLOAD"

# Stub injected before any other script runs on every new document in the
# workshop's BrowserContext. Defines ``window.Html5Qrcode`` matching the
# subset of the API the inline glue uses (constructor + ``.start`` +
# ``.stop``). ``.start`` resolves and then fires ``onScanSuccess`` with our
# known payload after a microtask — the same "decode succeeded" code-path
# the real library would take. Also polyfills
# ``navigator.mediaDevices.getUserMedia`` to a no-op so the IIFE's
# feature-detect succeeds even in environments where Chromium might not
# expose it (defensive — ``http://127.0.0.1:port`` is a secure context, so
# native exposure should suffice, but the polyfill is cheap).
_HTML5QRCODE_STUB_JS = """
(function () {
    if (!navigator.mediaDevices) {
        Object.defineProperty(navigator, 'mediaDevices', {
            value: {},
            configurable: true,
        });
    }
    if (!navigator.mediaDevices.getUserMedia) {
        navigator.mediaDevices.getUserMedia = function () {
            return Promise.resolve({});
        };
    }
    function StubHtml5Qrcode(elementId) {
        this.elementId = elementId;
    }
    StubHtml5Qrcode.prototype.start = function (
        constraints, config, onScanSuccess, onScanError
    ) {
        var self = this;
        setTimeout(function () {
            if (typeof onScanSuccess === 'function') {
                onScanSuccess(window.__SC2C_PAYLOAD || 'E2E-CAM-PAYLOAD');
            }
        }, 30);
        return Promise.resolve();
    };
    StubHtml5Qrcode.prototype.stop = function () {
        return Promise.resolve();
    };
    Object.defineProperty(window, 'Html5Qrcode', {
        value: StubHtml5Qrcode,
        writable: true,
        configurable: true,
    });
    window.__SC2C_PAYLOAD = 'E2E-CAM-PAYLOAD';
})();
"""


def _dev_login(
    page: Page, base_url: str, email: str, sub: str, name: str = "Test User"
) -> None:
    page.set_content(
        f"""<form id="f" method="post" action="{base_url}/auth/_dev-login">
              <input name="email" value="{email}">
              <input name="name" value="{name}">
              <input name="sub" value="{sub}">
            </form>"""
    )
    page.evaluate("document.getElementById('f').submit()")
    page.wait_for_url(f"{base_url}/")


def _admin_promote(
    base_url: str,
    context: BrowserContext,
    *,
    email: str,
    role: str,
) -> None:
    """Sign in as the bootstrap admin and promote ``email`` → ``role`` + active."""
    admin_context = (
        context.browser.new_context() if context.browser else context
    )
    admin_page = admin_context.new_page()
    _dev_login(
        admin_page,
        base_url,
        email="admin@uc.test",
        sub="g-e2e-admin",
        name="Seed Admin",
    )
    admin_page.goto(f"{base_url}/admin/users")
    row = admin_page.locator('[data-testid="user-row"]', has_text=email)
    row.locator('[data-testid="role-select"]').select_option(role)
    row.locator('[data-testid="role-submit"]').click()
    admin_page.wait_for_url(f"{base_url}/admin/users")
    promoted = admin_page.locator('[data-testid="user-row"]', has_text=email)
    promoted.locator('[data-testid="status-select"]').select_option("active")
    promoted.locator('[data-testid="status-submit"]').click()
    admin_page.wait_for_url(f"{base_url}/admin/users")
    admin_page.close()
    if admin_context is not context:
        admin_context.close()


def test_workshop_scans_via_camera_on_mobile_viewport(
    context: BrowserContext, app_server: str
) -> None:
    # Step 1: Workshop + Manager pending sign-ups.
    for email, sub in (
        ("scan-ws@uc.test", "g-e2e-scan-ws"),
        ("scan-mgr@uc.test", "g-e2e-scan-mgr"),
    ):
        page = context.new_page()
        _dev_login(page, app_server, email=email, sub=sub)
        expect(page.get_by_test_id("pending-heading")).to_be_visible()
        page.close()

    # Step 2: Admin promotes both.
    _admin_promote(app_server, context, email="scan-ws@uc.test", role="workshop")
    _admin_promote(app_server, context, email="scan-mgr@uc.test", role="manager")

    # Step 3: Manager creates a category + a qty-tracked item with the known
    # qr_code that our stub will deliver as the decoded payload.
    mgr_context = context.browser.new_context() if context.browser else context
    mgr_page = mgr_context.new_page()
    _dev_login(
        mgr_page,
        app_server,
        email="scan-mgr@uc.test",
        sub="g-e2e-scan-mgr",
        name="Scan Manager",
    )
    expect(mgr_page.get_by_test_id("welcome")).to_be_visible()

    mgr_page.goto(f"{app_server}/admin/taxonomy")
    mgr_page.get_by_test_id("new-taxonomy").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy/new")
    mgr_page.get_by_test_id("taxonomy-name-input").fill("Camera E2E Cat")
    mgr_page.get_by_test_id("taxonomy-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")

    mgr_page.goto(f"{app_server}/admin/items/new")
    mgr_page.get_by_test_id("item-sku-input").fill("SC2C-CAM-001")
    mgr_page.get_by_test_id("item-name-input").fill("Camera test item")
    mgr_page.get_by_test_id("item-category-input").select_option(
        label="Camera E2E Cat"
    )
    mgr_page.get_by_test_id("item-unit-input").fill("ea")
    mgr_page.get_by_test_id("item-qr-input").fill(_E2E_QR_PAYLOAD)
    mgr_page.get_by_test_id("item-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")

    item_row = mgr_page.locator(
        '[data-testid="item-row"]', has_text="SC2C-CAM-001"
    )
    item_id = item_row.get_attribute("data-item-id")
    assert item_id is not None
    mgr_page.close()

    # Step 4: Workshop signs in inside its own context, with the camera stub
    # installed via add_init_script + the CDN script blocked via route.
    if not context.browser:
        raise RuntimeError("expected a browser-backed context for SC2c")
    ws_context = context.browser.new_context()
    ws_context.add_init_script(_HTML5QRCODE_STUB_JS)
    # Block the real html5-qrcode CDN load so it doesn't overwrite our stub.
    ws_context.route(
        "**/html5-qrcode*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/javascript",
            body="/* SC2c stubbed — html5-qrcode CDN load blocked */",
        ),
    )
    ws_page = ws_context.new_page()
    # Mobile viewport: iPhone 12/13/14 logical resolution. Workshop's
    # hot-path is "tablet/phone scanning a QR on a workbench".
    ws_page.set_viewport_size({"width": 390, "height": 844})

    _dev_login(
        ws_page,
        app_server,
        email="scan-ws@uc.test",
        sub="g-e2e-scan-ws",
        name="Scan Workshop",
    )
    expect(ws_page.get_by_test_id("welcome")).to_be_visible()

    # Step 5: Workshop navigates to /scan via the role-aware nav link.
    ws_page.get_by_test_id("nav-scan").click()
    ws_page.wait_for_url(f"{app_server}/scan")
    expect(ws_page.get_by_test_id("scan-heading")).to_be_visible()

    # Step 6: The camera-toggle button becomes visible (SC2a's IIFE
    # feature-detected getUserMedia and unhid the button).
    toggle = ws_page.get_by_test_id("scan-camera-toggle")
    expect(toggle).to_be_visible()
    expect(toggle).to_have_attribute("aria-expanded", "false")

    # Step 7: Click the toggle. Our stubbed Html5Qrcode.start fires
    # onScanSuccess(payload) after a microtask, and the IIFE writes
    # payload → input + form.submit() → POST /scan/resolve → 303 to
    # /scan/item/{id}.
    toggle.click()
    ws_page.wait_for_url(f"{app_server}/scan/item/{item_id}")

    # Step 8: The action picker renders on the resolved-item page (SC1b).
    expect(ws_page.get_by_test_id("scan-resolved-item")).to_be_visible()
    resolved = ws_page.locator('[data-testid="scan-resolved-item"]')
    assert resolved.get_attribute("data-item-id") == item_id
    expect(ws_page.get_by_test_id("scan-out-form")).to_be_visible()
    expect(ws_page.get_by_test_id("scan-in-form")).to_be_visible()
    expect(ws_page.get_by_test_id("scan-adjust-form")).to_be_visible()

    ws_page.close()
    ws_context.close()

    # Step 9: Cleanup. Manager archives the item + category so downstream
    # walks see empty active lists.
    cleanup_context = (
        context.browser.new_context() if context.browser else context
    )
    cleanup_page = cleanup_context.new_page()
    _dev_login(
        cleanup_page,
        app_server,
        email="scan-mgr@uc.test",
        sub="g-e2e-scan-mgr",
        name="Scan Manager",
    )
    cleanup_page.goto(f"{app_server}/admin/items")
    cleanup_row = cleanup_page.locator(
        '[data-testid="item-row"]', has_text="SC2C-CAM-001"
    )
    cleanup_row.get_by_test_id("archive-item").click()
    cleanup_page.wait_for_url(f"{app_server}/admin/items")

    cleanup_page.goto(f"{app_server}/admin/taxonomy")
    cat_row = cleanup_page.locator(
        '[data-testid="taxonomy-row"]', has_text="Camera E2E Cat"
    )
    cat_row.get_by_test_id("archive-taxonomy").click()
    cleanup_page.wait_for_url(f"{app_server}/admin/taxonomy")
    cleanup_page.close()
    if cleanup_context is not context:
        cleanup_context.close()
