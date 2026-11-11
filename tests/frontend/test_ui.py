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


def _pos(cm, tm):
    """Where a text run starts on the page: its text matrix placed by the current transformation matrix."""
    return tm[4] * cm[0] + tm[5] * cm[2] + cm[4], tm[4] * cm[1] + tm[5] * cm[3] + cm[5]


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
    created = page.locator(".pm-toast-ok", has_text="Created")   # an earlier toast may still be on screen
    expect(created).to_contain_text('Created "pages (extract).pdf".')
    expect(page.locator("#page-total")).to_have_text("4")
    created.locator("a").click()
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
    for button in ("#rotate-left", "#delete-pages", "#duplicate", "#add-blank", "#reverse", "#extract", "#undo",
                   "#edit-tools button[data-tool=text]", "#edit-image", "#edit-sign", "#save-edits"):
        expect(page.locator(button)).to_be_disabled()


def test_content_editing_text_shapes_image_signature_save_and_undo(page, tmp_path):
    import io
    from PIL import Image
    from pypdf import PdfReader

    pdf = tmp_path / "edit.pdf"
    pdf.write_bytes(build_pdf(["Original one", "Original two"]))
    picture = tmp_path / "logo.png"
    Image.new("RGB", (120, 60), (20, 120, 220)).save(picture)
    sign_up(page, "edit@example.com")
    page.set_input_files("#file-input", str(pdf))
    page.locator("#file-rows tr").get_by_role("link", name="Open").click()
    expect(page.locator("#status")).to_have_text("Ready")
    expect(page.locator("#save-edits")).to_be_disabled()
    count = page.locator("#edit-count")
    tool = lambda name: page.click(f"#edit-tools button[data-tool={name}]")
    page.click("#fit-page")                     # the whole page must be on screen for the mouse to reach it
    box = page.locator(".page[data-page='1']").bounding_box()
    k = box["width"] / 612                      # CSS pixels per PDF point
    at = lambda x, y: (box["x"] + x * k, box["y"] + y * k)

    def drag(x1, y1, x2, y2):
        page.mouse.move(*at(x1, y1)); page.mouse.down()
        page.mouse.move(*at((x1 + x2) / 2, (y1 + y2) / 2 + 3), steps=3); page.mouse.move(*at(x2, y2), steps=3)
        page.mouse.up()

    # text: click, type two lines, finish with Ctrl+Enter; the tool returns to Select with the text selected
    tool("text")
    page.mouse.click(*at(100, 200))
    page.keyboard.type("Hello World")
    page.keyboard.press("Enter")
    page.keyboard.type("Line two")
    page.keyboard.press("Control+Enter")
    expect(count).to_have_text("1 unsaved edit")
    expect(page.locator("#edit-tools button[data-tool=select]")).to_have_class("active")
    expect(page.locator(".edit-layer text")).to_have_count(1)
    expect(page.locator("#undo")).to_be_enabled()

    # whiteout over the original heading, a rectangle, an arrow, a pen stroke and a highlight
    tool("whiteout"); drag(60, 80, 320, 115)
    tool("rect"); drag(350, 300, 450, 360)
    tool("arrow"); drag(100, 400, 250, 450)
    tool("pen"); drag(100, 500, 200, 540)
    tool("highlight"); drag(90, 190, 260, 215)
    expect(count).to_have_text("6 unsaved edits")

    # a tiny drag creates nothing; local undo removes the newest object (the highlight)
    drag(300, 600, 301, 601)
    expect(count).to_have_text("6 unsaved edits")
    page.click("#undo")
    expect(count).to_have_text("5 unsaved edits")

    # text the standard PDF fonts cannot encode is saved as a picture of the text
    tool("text")
    page.mouse.click(*at(100, 620))
    page.keyboard.insert_text("\u0928\u092e\u0938\u094d\u0924\u0947")
    page.keyboard.press("Control+Enter")

    # picture from a file, then a drawn signature
    page.set_input_files("#edit-image-input", str(picture))
    expect(page.locator(".edit-layer image")).to_have_count(1)
    page.click("#edit-sign")
    pad = page.locator("#sign-canvas").bounding_box()
    page.mouse.move(pad["x"] + 40, pad["y"] + 60); page.mouse.down()
    page.mouse.move(pad["x"] + 140, pad["y"] + 30, steps=5); page.mouse.move(pad["x"] + 220, pad["y"] + 90, steps=5)
    page.mouse.up()
    page.click("#sign-dialog button[value=ok]")
    expect(page.locator(".edit-layer image")).to_have_count(2)
    expect(count).to_have_text("8 unsaved edits")

    # move the first text with the Select tool, and delete the rectangle with the keyboard
    tool("select")
    page.mouse.click(*at(360, 300))             # on the rectangle's outline
    expect(page.locator("#edit-delete")).to_be_enabled()
    page.keyboard.press("Delete")
    expect(count).to_have_text("7 unsaved edits")
    drag(110, 205, 160, 255)                    # text moves by (50, 50)
    expect(page.locator(".edit-selection")).to_have_count(1)

    if os.environ.get("PDFMASTER_SCREENSHOT_DIR"):
        page.screenshot(path=os.path.join(os.environ["PDFMASTER_SCREENSHOT_DIR"], "edit.png"))

    # save: one new version containing everything
    page.click("#save-edits")
    expect(page.locator("#status")).to_have_text("All changes saved")
    expect(count).to_have_text("")
    expect(page.locator(".edit-layer .edit-item")).to_have_count(0)
    with page.expect_download() as dl:
        page.click("#download")
    first, second = PdfReader(io.BytesIO(Path(dl.value.path()).read_bytes())).pages
    text = first.extract_text()
    assert "Hello World" in text and "Line two" in text and "Original one" in text
    assert second.extract_text().strip() == "Original two"
    assert len(first.images) == 3              # picture, signature and the non-Latin text
    found = {}
    first.extract_text(visitor_text=lambda t, cm, tm, f, s: found.setdefault(t.strip(), _pos(cm, tm)) if t.strip() else None)
    x, y = found["Hello World"]
    assert abs(x - 150) < 3 and abs(792 - y - (250 - 0.6 * 14 + 0.9 * 14)) < 3, (x, y)

    if os.environ.get("PDFMASTER_SCREENSHOT_DIR"):
        page.screenshot(path=os.path.join(os.environ["PDFMASTER_SCREENSHOT_DIR"], "edit-saved.png"))

    # Undo now takes the whole save back on the server
    page.click("#undo")
    expect(page.locator("#status")).to_have_text("Ready")
    with page.expect_download() as dl:
        page.click("#download")
    assert "Hello World" not in PdfReader(io.BytesIO(Path(dl.value.path()).read_bytes())).pages[0].extract_text()
    assert page.problems == [], page.problems


def test_edit_existing_text_replace_marks_typed_signature_and_blank_document(page, tmp_path):
    import io
    from pypdf import PdfReader

    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(build_pdf(["Invoice total 100", "Total again"]))
    sign_up(page, "replace@example.com")

    # a blank document can be started from the dashboard and opens in the editor
    page.click("#new-blank")
    page.wait_for_url("**/editor.html?id=*")
    expect(page.locator("#status")).to_have_text("Ready")
    expect(page.locator("#doc-name")).to_have_text("Untitled.pdf")
    expect(page.locator("#page-total")).to_have_text("1")
    page.goto("/dashboard.html")

    page.set_input_files("#file-input", str(pdf))
    page.locator("#file-rows tr", has_text="invoice.pdf").get_by_role("link", name="Open").click()
    expect(page.locator("#status")).to_have_text("Ready")
    count = page.locator("#edit-count")
    tool = lambda name: page.click(f"#edit-tools button[data-tool={name}]")
    page.click("#fit-page")
    wait_rendered(page, ".page[data-page='1'] canvas")
    box = page.locator(".page[data-page='1']").bounding_box()
    k = box["width"] / 612
    at = lambda x, y: (box["x"] + x * k, box["y"] + y * k)

    # find and replace covers each matching line and retypes it (case-insensitive, all pages)
    expect(page.locator("#replace-open")).to_be_enabled()
    page.click("#replace-open")
    page.fill("#replace-find", "total")
    page.fill("#replace-with", "Sum")
    page.click("#replace-dialog button[value=ok]")
    expect(page.locator(".pm-toast-ok")).to_contain_text("Replaced 2 occurrences in 2 places")
    expect(count).to_have_text("4 unsaved edits")       # a cover and a text object per place
    page.click("#save-edits")
    expect(page.locator("#status")).to_have_text("All changes saved")

    # "Edit text": click the (new) line, retype it; the old line is covered in the page colour
    wait_rendered(page, ".page[data-page='1'] canvas")
    tool("edittext")
    page.mouse.click(*at(100, 92))
    editor = page.locator(".edit-textbox")
    expect(editor).to_have_value("Invoice Sum 100")
    page.keyboard.press("Control+a")
    page.keyboard.type("Invoice Sum 250 paid")
    page.keyboard.press("Control+Enter")
    expect(count).to_have_text("2 unsaved edits")
    expect(page.locator(".edit-layer rect.whiteout")).to_have_attribute("fill", "#ffffff")
    tool("edittext")
    page.mouse.click(*at(400, 600))                     # empty area: nothing to edit there
    expect(page.locator(".pm-toast").last).to_contain_text("Click on a line of text")

    # check mark, cross, strike-through, underline, a filled rectangle and a typed signature
    tool("tick"); page.mouse.click(*at(100, 300))
    tool("cross"); page.mouse.click(*at(140, 300))
    tool("strike")
    page.mouse.move(*at(72, 180)); page.mouse.down(); page.mouse.move(*at(150, 190), steps=3); page.mouse.move(*at(300, 204), steps=3); page.mouse.up()
    tool("underline")
    page.mouse.move(*at(72, 220)); page.mouse.down(); page.mouse.move(*at(150, 230), steps=3); page.mouse.move(*at(300, 244), steps=3); page.mouse.up()
    expect(page.locator(".edit-layer line[stroke-linecap]")).to_have_count(2)
    page.click("#edit-fill-on")
    tool("rect")
    page.mouse.move(*at(350, 300)); page.mouse.down(); page.mouse.move(*at(400, 330), steps=3); page.mouse.move(*at(450, 360), steps=3); page.mouse.up()
    expect(page.locator(".edit-layer rect[fill='#fff2a8']")).to_have_count(1)
    page.click("#edit-sign")
    page.fill("#sign-name", "Asha Rao")
    page.click("#sign-dialog button[value=ok]")
    expect(page.locator(".edit-layer image")).to_have_count(1)
    expect(count).to_have_text("8 unsaved edits")

    if os.environ.get("PDFMASTER_SCREENSHOT_DIR"):
        page.screenshot(path=os.path.join(os.environ["PDFMASTER_SCREENSHOT_DIR"], "edit2.png"))
    page.click("#save-edits")
    expect(page.locator("#status")).to_have_text("All changes saved")
    with page.expect_download() as dl:
        page.click("#download")
    first, second = PdfReader(io.BytesIO(Path(dl.value.path()).read_bytes())).pages
    assert "Invoice Sum 250 paid" in first.extract_text() and "Sum again" in second.extract_text()
    assert len(first.images) == 1
    found = {}
    first.extract_text(visitor_text=lambda t, cm, tm, f, s: found.setdefault(t.strip(), (*_pos(cm, tm), s)) if t.strip() else None)
    x, y, size = found["Invoice Sum 250 paid"]
    assert abs(x - 72) < 1 and abs(y - 692) < 1.5 and abs(size - 24) < 0.6, (x, y, size)   # same place and size as the original line
    assert page.problems == [], page.problems


def test_tools_page_links_and_form_filling(page, tmp_path):
    import io
    from PIL import Image
    from pypdf import PdfReader
    from reportlab.pdfgen import canvas

    (tmp_path / "alpha.pdf").write_bytes(build_pdf(["Alpha one", "Alpha two"]))
    (tmp_path / "beta.pdf").write_bytes(build_pdf(["Beta one"]))
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 750, "Application form")
    c.acroForm.textfield(name="full_name", x=72, y=700, width=200, height=20, value="")
    c.acroForm.checkbox(name="agree", x=72, y=660, size=14, checked=False)
    c.save()
    (tmp_path / "form.pdf").write_bytes(buf.getvalue())
    Image.new("RGB", (300, 200), (10, 160, 90)).save(tmp_path / "photo.png")

    sign_up(page, "tools@example.com")
    page.set_input_files("#file-input", [str(tmp_path / n) for n in ("alpha.pdf", "beta.pdf", "form.pdf")])
    expect(page.locator("#file-rows tr")).to_have_count(3)
    page.click("#tools-link")
    page.wait_for_url("**/tools.html")
    expect(page.locator(".tool-card")).to_have_count(23)
    if os.environ.get("PDFMASTER_SCREENSHOT_DIR"):
        page.screenshot(path=os.path.join(os.environ["PDFMASTER_SCREENSHOT_DIR"], "tools.png"))
    result = page.locator("#tool-result")
    card = lambda name: page.click(f".tool-card[data-tool={name}]")
    run = lambda: page.click("#tool-run")
    close = lambda: page.click("#tool-close")

    # merge in the order the documents are ticked
    card("merge")
    page.locator(".doc-row", has_text="beta.pdf").locator("input").check()
    page.locator(".doc-row", has_text="alpha.pdf").locator("input").check()
    expect(page.locator(".doc-row", has_text="alpha.pdf").locator(".doc-order")).to_have_text("2")
    page.fill("#f-name", "Joined")
    run()
    expect(result).to_contain_text("Merged into a new document.")
    expect(result.locator("a")).to_have_text("Open Joined.pdf")
    close()

    # watermark and page numbers change the document itself; errors are shown in the dialog
    card("watermark")
    page.select_option("#f-document_id", label="Joined.pdf (3 pages)")
    run()
    expect(result).to_contain_text("Watermark added.")
    close()
    card("numbers")
    page.select_option("#f-document_id", label="Joined.pdf (3 pages)")
    page.fill("#f-pages", "2-9")
    run()
    expect(result).to_have_class("tool-result failed")
    expect(result).to_contain_text("only 3 pages")
    page.fill("#f-pages", "all")
    run()
    expect(result).to_contain_text("Page numbers added.")
    close()

    # downloads and pictures
    card("to-text")
    page.select_option("#f-document_id", label="Joined.pdf (3 pages)")
    with page.expect_download() as dl:
        run()
    text = Path(dl.value.path()).read_text()
    assert dl.value.suggested_filename == "Joined.txt" and "Beta one" in text and "DRAFT" in text and "Page 3 of 3" in text
    close()
    card("images-to-pdf")
    page.set_input_files("#f-files", str(tmp_path / "photo.png"))
    run()
    expect(result.locator("a")).to_have_text("Open Images.pdf")
    close()
    card("bates")
    page.select_option("#f-document_id", label="Joined.pdf (3 pages)")
    run()
    expect(result).to_contain_text("Bates numbers added.")
    close()
    card("nup")
    page.select_option("#f-document_id", label="Joined.pdf (3 pages)")
    run()
    expect(result.locator("a")).to_have_text("Open Joined (2-up).pdf")
    close()
    card("mix")
    page.locator(".doc-row", has_text="alpha.pdf").locator("input").check()
    run()
    expect(page.locator(".pm-toast", has_text="Tick exactly 2 documents")).to_be_visible()
    page.locator(".doc-row", has_text="beta.pdf").locator("input").check()
    run()
    expect(result.locator("a")).to_have_text("Open Mixed.pdf")
    close()
    card("repair")
    page.set_input_files("#f-file", str(tmp_path / "beta.pdf"))
    run()
    expect(result.locator("a")).to_have_text("Open beta (repaired).pdf")
    close()
    card("split")
    page.select_option("#f-document_id", label="Joined.pdf (3 pages)")
    page.fill("#f-ranges", "1, 2-3")
    run()
    expect(result).to_contain_text("Created 2 documents.")
    expect(result.locator("a")).to_have_count(2)

    # editor: a link box, then the PDF's own form fields
    result.locator("a", has_text="pages 2-3").click()
    page.wait_for_url("**/editor.html?id=*")
    expect(page.locator("#status")).to_have_text("Ready")
    page.click("#fit-page")
    box = page.locator(".page[data-page='1']").bounding_box()
    k = box["width"] / 612
    page.click("#edit-tools button[data-tool=link]")
    page.mouse.move(box["x"] + 72 * k, box["y"] + 80 * k); page.mouse.down()
    page.mouse.move(box["x"] + 120 * k, box["y"] + 95 * k, steps=3); page.mouse.move(box["x"] + 172 * k, box["y"] + 110 * k, steps=3)
    page.mouse.up()
    page.fill("#link-url", "example.com/docs")
    page.click("#link-dialog button[value=ok]")
    expect(page.locator("#edit-count")).to_have_text("1 unsaved edit")
    # a new fillable field: a small square suggests a check box, a wide box a text field
    page.click("#edit-tools button[data-tool=field]")
    page.mouse.move(box["x"] + 72 * k, box["y"] + 300 * k); page.mouse.down()
    page.mouse.move(box["x"] + 150 * k, box["y"] + 310 * k, steps=3); page.mouse.move(box["x"] + 272 * k, box["y"] + 322 * k, steps=3)
    page.mouse.up()
    expect(page.locator("#field-type")).to_have_value("text")
    page.fill("#field-name", "Customer name")
    page.click("#field-dialog button[value=ok]")
    expect(page.locator(".edit-layer .edit-field")).to_have_count(1)
    expect(page.locator("#edit-count")).to_have_text("2 unsaved edits")
    page.click("#edit-form")                      # saves the pending objects first, then offers the new field
    expect(page.locator("#form-fields .form-row")).to_have_count(1)
    page.locator("#form-fields input").fill("Meera")
    page.click("#form-dialog button[value=ok]")
    expect(page.locator(".pm-toast-ok", has_text="Filled 1 field")).to_be_visible()
    expect(page.locator("#status")).to_have_text("All changes saved")
    with page.expect_download() as dl:
        page.click("#download")
    saved = PdfReader(io.BytesIO(Path(dl.value.path()).read_bytes()))
    assert saved.get_fields()["Customer name"]["/V"] == "Meera"
    link = next(a.get_object() for a in saved.pages[0]["/Annots"] if a.get_object()["/Subtype"] == "/Link")
    assert link["/A"]["/URI"] == "https://example.com/docs"
    assert all(abs(float(a) - b) < 2 for a, b in zip(link["/Rect"], (72, 682, 172, 712)))

    page.goto("/dashboard.html")
    page.locator("#file-rows tr", has_text="beta.pdf").get_by_role("link", name="Open").click()
    expect(page.locator("#status")).to_have_text("Ready")
    page.click("#edit-form")
    expect(page.locator(".pm-toast", has_text="no fillable form fields")).to_be_visible()
    page.goto("/dashboard.html")
    page.locator("#file-rows tr", has_text="form.pdf").get_by_role("link", name="Open").click()
    expect(page.locator("#status")).to_have_text("Ready")
    page.click("#edit-form")
    fields = page.locator("#form-fields .form-row")
    expect(fields).to_have_count(2)
    fields.filter(has_text="full_name").locator("input").fill("Asha Rao")
    fields.filter(has_text="agree").locator("input").check()
    page.click("#form-dialog button[value=ok]")
    expect(page.locator("#status")).to_have_text("All changes saved")
    with page.expect_download() as dl:
        page.click("#download")
    values = PdfReader(io.BytesIO(Path(dl.value.path()).read_bytes())).get_fields()
    assert values["full_name"]["/V"] == "Asha Rao" and values["agree"]["/V"] != "/Off"
    page.click("#more-tools")
    page.wait_for_url("**/tools.html?doc=*")
    assert page.problems == [], page.problems
