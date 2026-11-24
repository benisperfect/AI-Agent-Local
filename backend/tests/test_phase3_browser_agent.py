"""Phase 3 Verification Suite: Authenticated Browser Agent (Brave CDP + MV3 Extension Bridge)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright
from websockets.asyncio.client import connect

from sirius.browser.agent import AuthenticatedBrowserAgent
from sirius.browser.profiles import discover_browser_profile
from sirius.ipc.server import IPCServer


def test_brave_and_cross_browser_profile_discovery() -> None:
    brave = discover_browser_profile("brave")
    assert brave.kind == "brave"
    assert brave.supports_cdp is True
    assert "Brave" in brave.display_name
    if brave.executable_path:
        args = brave.build_cdp_launch_args(9222)
        assert any("--remote-debugging-port=9222" in a for a in args)


async def test_playwright_live_dom_distiller_shadow_dom_forms_upload_and_tabs(
    tmp_path: Path,
) -> None:
    brave = discover_browser_profile("brave")
    chrome = discover_browser_profile("chrome")
    exe_path = brave.executable_path or chrome.executable_path
    assert exe_path is not None, "Brave or Chrome binary required for live Playwright test"

    ipc = IPCServer(host="127.0.0.1", port=0, auth_token="browser-tok")
    await ipc.start()
    agent = AuthenticatedBrowserAgent(
        ipc=ipc,
        primary_browser="brave",
        downloads_dir=tmp_path / "downloads",
    )

    sample_upload = tmp_path / "payouts_nov.csv"
    sample_upload.write_text("id,amount\np1,420.50\n", encoding="utf-8")

    html_app = """
    <!doctype html>
    <html>
      <head><title>Accounting Portal</title></head>
      <body>
        <label for="acctEmail">Accountant Email</label>
        <input id="acctEmail" type="email" value="" />
        <input id="secretPass" type="password" value="supersecret" />
        <select id="quarterSel" aria-label="Tax Quarter">
          <option value="Q3">Q3</option>
          <option value="Q4">Q4</option>
        </select>
        <input id="csvFile" type="file" aria-label="Upload CSV" />
        <button id="submitBtn" onclick="document.body.dataset.submitted = document.getElementById('acctEmail').value + ':' + document.getElementById('quarterSel').value">
          Submit Report
        </button>
        <div id="shadowHost"></div>
        <script>
          const host = document.getElementById("shadowHost");
          const root = host.attachShadow({ mode: "open" });
          const btn = document.createElement("button");
          btn.textContent = "Shadow Action";
          btn.onclick = () => { document.body.dataset.shadowClicked = "yes"; };
          root.appendChild(btn);
        </script>
      </body>
    </html>
    """

    canvas_html = """
    <!doctype html>
    <html>
      <head><title>Figma Canvas Board</title></head>
      <body>
        <canvas width="900" height="600" style="width:900px;height:600px;"></canvas>
      </body>
    </html>
    """

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                executable_path=exe_path,
                headless=True,
                args=["--no-sandbox"],
            )
            context = await browser.new_context()
            page = await context.new_page()
            await page.set_content(html_app)

            await agent.attach_playwright_context(context)
            assert agent.mode == "cdp"

            distilled = await agent.distill_page()
            assert distilled.title == "Accounting Portal"
            assert not distilled.needs_vision_fallback

            # Password values must ALWAYS be masked in distilled output
            pw_el = next(e for e in distilled.elements if e.input_type == "password")
            assert pw_el.value == "••••••••"

            email_el = next(e for e in distilled.elements if "Accountant Email" in e.label)
            select_el = next(e for e in distilled.elements if "Tax Quarter" in e.label)
            file_el = next(e for e in distilled.elements if e.input_type == "file")
            submit_el = next(e for e in distilled.elements if "Submit Report" in e.label)
            shadow_el = next(e for e in distilled.elements if "Shadow Action" in e.label)

            await agent.fill(email_el.agent_id, "cpa@firm.com")
            await agent.select_dropdown(select_el.agent_id, "Q4")
            await agent.upload_file(file_el.agent_id, str(sample_upload))
            await agent.click(submit_el.agent_id)
            await agent.click(shadow_el.agent_id)

            submitted_val = await page.evaluate("document.body.dataset.submitted")
            shadow_val = await page.evaluate("document.body.dataset.shadowClicked")
            assert submitted_val == "cpa@firm.com:Q4"
            assert shadow_val == "yes"

            # Open silent background tab (foreground=False) with pure <canvas> app
            bg_page = await context.new_page()
            await bg_page.set_content(canvas_html)
            tabs = await agent.list_tabs()
            assert len(tabs) == 2

            canvas_distilled = await agent.distill_page(tab_index=1)
            assert canvas_distilled.has_large_canvas is True
            assert canvas_distilled.needs_vision_fallback is True

            shot = await agent.capture_screenshot(tab_index=0)
            assert shot.startswith(b"\x89PNG")

            await browser.close()
    finally:
        await agent.close()
        await ipc.stop()


async def test_companion_mv3_extension_fallback_mode() -> None:
    ipc = IPCServer(host="127.0.0.1", port=0, auth_token="ext-secret")
    await ipc.start()
    agent = AuthenticatedBrowserAgent(ipc=ipc, primary_browser="brave", cdp_port=59999)
    uri = f"ws://127.0.0.1:{ipc.bound_port}"

    try:
        async with connect(uri) as ext_ws:
            await ext_ws.send(
                json.dumps(
                    {
                        "type": "auth",
                        "token": "ext-secret",
                        "client_role": "browser_extension",
                    }
                )
            )
            ack = json.loads(await ext_ws.recv())
            assert ack["type"] == "auth_ok"
            assert await agent.attach_if_available() == "extension"

            async def mock_extension_worker() -> None:
                for _ in range(2):
                    cmd = json.loads(await ext_ws.recv())
                    req_id = cmd["request_id"]
                    if cmd["action"] == "open_tab":
                        await ext_ws.send(
                            json.dumps(
                                {
                                    "type": "extension_result",
                                    "request_id": req_id,
                                    "ok": True,
                                    "data": {"tab_id": 42, "url": cmd["payload"]["url"]},
                                }
                            )
                        )
                    elif cmd["action"] == "distill_dom":
                        await ext_ws.send(
                            json.dumps(
                                {
                                    "type": "extension_result",
                                    "request_id": req_id,
                                    "ok": True,
                                    "data": {
                                        "url": "https://dashboard.stripe.com/payouts",
                                        "title": "Stripe Payouts",
                                        "elements": [
                                            {
                                                "agent_id": "s-1",
                                                "tag": "button",
                                                "role": "button",
                                                "input_type": "",
                                                "label": "Export CSV",
                                                "value": "",
                                                "href": "",
                                                "disabled": False,
                                                "bounds": {"x": 800, "y": 140, "width": 110, "height": 34},
                                            }
                                        ],
                                        "excerpt": "Payouts for November",
                                        "has_large_canvas": False,
                                        "needs_vision_fallback": False,
                                    },
                                }
                            )
                        )

            worker = asyncio.create_task(mock_extension_worker())
            opened = await agent.open_url("https://dashboard.stripe.com/payouts", foreground=False)
            assert opened["mode"] == "extension"
            assert opened["foreground"] is False

            page = await agent.distill_page()
            assert page.title == "Stripe Payouts"
            assert page.elements[0].label == "Export CSV"
            await worker
    finally:
        await agent.close()
        await ipc.stop()
