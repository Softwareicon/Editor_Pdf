"""Browser tests (Playwright) against a real server: sign-up, upload, viewer, search, download, delete.

Run:  pytest tests/frontend          (needs `playwright install chromium` once)
Set PDFMASTER_BROWSER_CHANNEL=chrome to use an installed Google Chrome instead.
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from pdf_factory import build_encrypted_pdf, build_pdf

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "correct horse battery"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ui")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "PDFMASTER_ENVIRONMENT": "test", "PDFMASTER_SECRET_KEY": "ui-test-secret-key-0123456789-abcdefghij",
           "PDFMASTER_DATABASE_URL": f"sqlite:///{(tmp / 'ui.db').as_posix()}", "PDFMASTER_STORAGE_DIR": str(tmp / "storage"),
           "PDFMASTER_SERVE_FRONTEND": "true", "PDFMASTER_AUTH_RATE_LIMIT_PER_MINUTE": "1000"}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT / "backend", env=env, check=True)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT / "backend", env=env)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{base}/api/health", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    yield base
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(channel=os.environ.get("PDFMASTER_BROWSER_CHANNEL") or None)
        yield b
        b.close()


@pytest.fixture
def page(browser, server):
    context = browser.new_context(base_url=server, viewport={"width": 1400, "height": 900})
    page = context.new_page()
    page.problems = []
    page.on("console", lambda m: page.problems.append(m.text)
            if m.type == "error" and "Failed to load resource" not in m.text else None)  # HTTP 4xx are asserted explicitly
    page.on("pageerror", lambda e: page.problems.append(str(e)))
    yield page
    context.close()


def sign_up(page, email):
    page.goto("/index.html")
    page.click("#tab-register")
    page.fill("#register-name", "Test User")
    page.fill("#register-email", email)
    page.fill("#register-password", PASSWORD)
    page.fill("#register-confirm", PASSWORD)
    page.click("#register-form button[type=submit]")
    page.wait_for_url("**/dashboard.html")


def painted(page, selector):
    """True if the canvas contains non-white pixels, i.e. PDF content was really drawn."""
    return page.eval_on_selector(selector, """c => {
        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
        for (let i = 0; i < d.length; i += 4) if (d[i] < 200 || d[i+1] < 200 || d[i+2] < 200) return true;
        return false; }""")


def wait_rendered(page, selector, timeout=10.0):
    """Poll until the canvas has been drawn (the site's CSP forbids string-eval based waiting)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if page.locator(selector).count() and page.eval_on_selector(selector, "c => c.width") > 0:
            return
        time.sleep(0.05)
    raise AssertionError(f"{selector} was not rendered")


def test_dashboard_requires_login(page):
    page.goto("/dashboard.html")
    page.wait_for_url("**/index.html")


def test_login_errors_and_logout(page):
    sign_up(page, "login@example.com")
    expect(page.locator("#user-name")).to_have_text("Test User")
    page.click("#logout")
    page.wait_for_url("**/index.html")
    page.fill("#login-email", "login@example.com")
    page.fill("#login-password", "definitely wrong")
    page.click("#login-form button[type=submit]")
    expect(page.locator("#form-error")).to_have_text("Incorrect email or password")
    page.fill("#login-password", PASSWORD)
    page.click("#login-form button[type=submit]")
    page.wait_for_url("**/dashboard.html")


def test_upload_view_search_zoom_download_delete(page, tmp_path):
    pdf = tmp_path / "Annual Report.pdf"
    texts = [f"Page {i} of the report" + (" with a zebra" if i in (2, 7) else "") for i in range(1, 9)]
    pdf.write_bytes(build_pdf(texts))
    not_pdf = tmp_path / "notes.pdf"
    not_pdf.write_text("just text")

    sign_up(page, "viewer@example.com")
    expect(page.locator("#empty")).to_be_visible()
    page.set_input_files("#file-input", str(not_pdf))
    expect(page.locator("#uploads li.failed .status")).to_have_text("Only PDF files can be uploaded")
    page.set_input_files("#file-input", str(pdf))
    row = page.locator("#file-rows tr")
    expect(row).to_have_count(1)
    expect(row.locator(".file-name")).to_have_text("Annual Report.pdf")
    expect(row.locator("td").nth(1)).to_have_text("8")

    with page.expect_download() as dl:
        row.get_by_role("button", name="Download").click()
    assert Path(dl.value.path()).read_bytes() == pdf.read_bytes()

    # --- viewer ---
    row.get_by_role("link", name="Open").click()
    page.wait_for_url("**/editor.html?id=*")
    expect(page.locator("#status")).to_have_text("Ready")
    expect(page.locator("#page-total")).to_have_text("8")
    expect(page.locator("#pages .page")).to_have_count(8)
    expect(page.locator("#thumbs .thumb")).to_have_count(8)
    wait_rendered(page, ".page[data-page='1'] canvas")
    assert painted(page, ".page[data-page='1'] canvas")
    wait_rendered(page, "#thumbs .thumb canvas")
    assert painted(page, "#thumbs .thumb canvas")
    # lazy rendering: the last page is laid out but not painted yet
    assert page.eval_on_selector(".page[data-page='8'] canvas", "c => c.width") == 0

    # fit width fills the viewer; zoom changes the page size
    fit_width = page.eval_on_selector(".page[data-page='1']", "e => e.offsetWidth")
    viewer_width = page.eval_on_selector("#viewer", "e => e.clientWidth")
    assert viewer_width - 60 < fit_width <= viewer_width
    page.click("#fit-page")
    assert page.eval_on_selector(".page[data-page='1']", "e => e.offsetHeight") <= page.eval_on_selector("#viewer", "e => e.clientHeight")
    before = page.eval_on_selector(".page[data-page='1']", "e => e.offsetWidth")
    page.click("#zoom-in")
    assert page.eval_on_selector(".page[data-page='1']", "e => e.offsetWidth") > before
    page.click("#zoom-out"); page.click("#zoom-out")
    assert page.eval_on_selector(".page[data-page='1']", "e => e.offsetWidth") < before

    # navigation by page number and thumbnail
    page.fill("#page-input", "8"); page.press("#page-input", "Enter")
    wait_rendered(page, ".page[data-page='8'] canvas")
    assert painted(page, ".page[data-page='8'] canvas")
    page.locator("#thumbs .thumb").nth(3).click()
    expect(page.locator("#page-input")).to_have_value("4")

    # search across pages
    page.fill("#search", "ZEBRA"); page.press("#search", "Enter")
    expect(page.locator("#search-count")).to_have_text("2 of 2")
    expect(page.locator("#page-input")).to_have_value("7")  # first match at/after the current page (4)
    expect(page.locator(".page[data-page='7'] .hl.current")).to_have_count(1)
    page.click("#search-next")  # wraps around to the first match
    expect(page.locator("#search-count")).to_have_text("1 of 2")
    expect(page.locator("#page-input")).to_have_value("2")
    page.fill("#search", "unicorn"); page.press("#search", "Enter")
    expect(page.locator("#search-count")).to_have_text("0 results")

    expect(page.locator("#outline")).to_be_hidden()
    page.click(".side-tabs button[data-panel=outline]")
    expect(page.locator("#outline")).to_have_text("This document has no outline.")

    with page.expect_download() as dl:
        page.click("#download")
    assert dl.value.suggested_filename == "Annual Report.pdf"
    assert Path(dl.value.path()).read_bytes() == pdf.read_bytes()

    if os.environ.get("PDFMASTER_SCREENSHOT_DIR"):
        page.screenshot(path=os.path.join(os.environ["PDFMASTER_SCREENSHOT_DIR"], "viewer.png"))

    # --- delete from the dashboard ---
    page.click("a.brand")
    page.wait_for_url("**/dashboard.html")
    page.locator("#file-rows tr").get_by_role("button", name="Delete").click()
    page.locator("#confirm-delete button[value=delete]").click()
    expect(page.locator("#empty")).to_be_visible()
    assert page.problems == [], page.problems  # no script errors or CSP violations anywhere in the flow


def test_password_protected_pdf_opens_only_with_its_password(page, tmp_path):
    pdf = tmp_path / "locked.pdf"
    pdf.write_bytes(build_encrypted_pdf("open-sesame"))
    sign_up(page, "locked@example.com")
    page.set_input_files("#file-input", str(pdf))
    expect(page.locator("#file-rows .badge")).to_have_text("Password protected")
    page.locator("#file-rows tr").get_by_role("link", name="Open").click()
    expect(page.locator("#password-dialog")).to_be_visible()
    page.fill("#pdf-password", "wrong"); page.click("#password-form button[value=ok]")
    expect(page.locator("#password-hint")).to_have_text("Incorrect password. Please try again.")
    page.fill("#pdf-password", "open-sesame"); page.click("#password-form button[value=ok]")
    expect(page.locator("#status")).to_have_text("Ready")
    wait_rendered(page, ".page[data-page='1'] canvas")
    assert painted(page, ".page[data-page='1'] canvas")


def test_one_user_cannot_open_another_users_document(page, browser, server, tmp_path):
    pdf = tmp_path / "private.pdf"
    pdf.write_bytes(build_pdf(["private"]))
    sign_up(page, "owner@example.com")
    page.set_input_files("#file-input", str(pdf))
    href = page.locator("#file-rows .file-name").get_attribute("href")
    other = browser.new_context(base_url=server).new_page()
    sign_up(other, "intruder@example.com")
    other.goto(href)
    expect(other.locator("#message")).to_have_text("This document does not exist or you do not have access to it.")


def downloaded_texts(page):
    import io
    from pypdf import PdfReader
    with page.expect_download() as dl:
        page.click("#download")
    return [p.extract_text().strip() for p in PdfReader(io.BytesIO(Path(dl.value.path()).read_bytes())).pages]


def test_page_tools_rotate_delete_duplicate_reorder_undo_redo(page, tmp_path):
    pdf = tmp_path / "pages.pdf"
    pdf.write_bytes(build_pdf(["P1", "P2", "P3", "P4"]))
    sign_up(page, "pages@example.com")
    page.set_input_files("#file-input", str(pdf))
    page.locator("#file-rows tr").get_by_role("link", name="Open").click()
    expect(page.locator("#status")).to_have_text("Ready")
    expect(page.locator("#undo")).to_be_disabled()
    thumbs = page.locator("#thumbs .thumb")
    saved = lambda: expect(page.locator("#status")).to_have_text("All changes saved")

    # rotate page 2: it becomes landscape in the viewer
    thumbs.nth(1).click()
    expect(page.locator("#selection")).to_contain_text("Page 2 selected")
    box = lambda n: page.eval_on_selector(f".page[data-page='{n}']", "e => [e.offsetWidth, e.offsetHeight]")
    assert box(2)[0] < box(2)[1]
    page.click("#rotate-right"); saved()
    assert box(2)[0] > box(2)[1] and box(1)[0] < box(1)[1]
    expect(page.locator("#page-input")).to_have_value("2")
    page.click("#undo")
    expect(page.locator("#status")).to_have_text("Ready")
    assert box(2)[0] < box(2)[1]
    expect(page.locator("#redo")).to_be_enabled()

    # delete pages 2 and 3 (multi-select), then undo and redo
    thumbs.nth(1).click()
    thumbs.nth(2).click(modifiers=["Control"])
    expect(page.locator("#selection")).to_have_text("2 pages selected")
    page.click("#delete-pages"); saved()
    expect(page.locator("#page-total")).to_have_text("2")
    expect(thumbs).to_have_count(2)
    assert downloaded_texts(page) == ["P1", "P4"]
    page.click("#undo")
    expect(page.locator("#page-total")).to_have_text("4")
    page.click("#redo"); saved()
    expect(page.locator("#page-total")).to_have_text("2")
    page.click("#undo")
    expect(page.locator("#page-total")).to_have_text("4")

    # duplicate page 1, add a blank page after it
    thumbs.nth(0).click()
    page.click("#duplicate"); saved()
    expect(page.locator("#page-total")).to_have_text("5")
    page.click("#add-blank"); saved()
    assert downloaded_texts(page) == ["P1", "", "P1", "P2", "P3", "P4"]
    page.click("#undo"); page.click("#undo")
    expect(page.locator("#page-total")).to_have_text("4")

    # drag the first thumbnail below the third
    a, b = thumbs.nth(0).bounding_box(), thumbs.nth(2).bounding_box()
    page.mouse.move(a["x"] + a["width"] / 2, a["y"] + a["height"] / 2)
    page.mouse.down()
    page.mouse.move(a["x"] + a["width"] / 2, a["y"] + a["height"] / 2 + 20, steps=4)
    page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] * 0.8, steps=12)
    page.mouse.up()
    saved()
    assert downloaded_texts(page) == ["P2", "P3", "P1", "P4"]

    page.click("#reverse"); saved()
    assert downloaded_texts(page) == ["P4", "P1", "P3", "P2"]

    # extract leaves this document alone and offers the new one
    thumbs.nth(0).click()
    page.click("#extract")
    expect(page.locator(".pm-toast-ok")).to_contain_text('Created "pages (extract).pdf".')
    expect(page.locator("#page-total")).to_have_text("4")
    page.locator(".pm-toast-ok a").click()
    expect(page.locator("#page-total")).to_have_text("1")
    assert downloaded_texts(page) == ["P4"]
    if os.environ.get("PDFMASTER_SCREENSHOT_DIR"):
        page.go_back(); saved()
        page.locator("#thumbs .thumb").nth(1).click()
        page.screenshot(path=os.path.join(os.environ["PDFMASTER_SCREENSHOT_DIR"], "pages.png"))
    assert page.problems == [], page.problems


def test_page_tools_are_disabled_for_password_protected_pdfs(page, tmp_path):
    pdf = tmp_path / "locked.pdf"
    pdf.write_bytes(build_encrypted_pdf("open-sesame"))
    sign_up(page, "locked2@example.com")
    page.set_input_files("#file-input", str(pdf))
    page.locator("#file-rows tr").get_by_role("link", name="Open").click()
    page.fill("#pdf-password", "open-sesame"); page.click("#password-form button[value=ok]")
    expect(page.locator("#status")).to_have_text("Ready")
    expect(page.locator("#selection")).to_have_text("Password-protected PDFs cannot be edited yet")
    for button in ("#rotate-left", "#delete-pages", "#duplicate", "#add-blank", "#reverse", "#extract", "#undo"):
        expect(page.locator(button)).to_be_disabled()
