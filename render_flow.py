import pathlib, sys
from playwright.sync_api import sync_playwright

html_path = pathlib.Path(r"C:\Users\Crystal\WorkBuddy\医院随访\随访现状流程图.html").as_uri()
out_png = r"C:\Users\Crystal\WorkBuddy\医院随访\随访现状流程图.png"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1000, "height": 1200}, device_scale_factor=2)
    page.goto(html_path, wait_until="networkidle")
    page.wait_for_timeout(600)
    page.screenshot(path=out_png, full_page=True)
    browser.close()
print("RENDER_DONE", out_png)
