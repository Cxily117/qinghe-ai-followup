import pathlib
from playwright.sync_api import sync_playwright

html_path = pathlib.Path(r"C:\Users\Crystal\WorkBuddy\医院随访\flowchart_classic.html").as_uri()
out_png = r"C:\Users\Crystal\WorkBuddy\医院随访\随访现状流程图_判断框.png"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 980, "height": 1480}, device_scale_factor=2)
    page.goto(html_path, wait_until="networkidle")
    page.wait_for_timeout(500)
    page.screenshot(path=out_png, full_page=True)
    browser.close()
print("RENDER_DONE", out_png)
