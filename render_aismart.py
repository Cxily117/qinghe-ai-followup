import asyncio
from playwright.async_api import async_playwright

HTML = r"C:\Users\Crystal\WorkBuddy\医院随访\flowchart_aismart.html"
OUT = r"C:\Users\Crystal\WorkBuddy\医院随访\随访现状_AI数智化流程图.png"

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--no-sandbox"])
        pg = await b.new_page(device_scale_factor=2, viewport={"width": 960, "height": 1080})
        await pg.goto("file:///" + HTML)
        await pg.wait_for_timeout(400)
        el = await pg.query_selector("svg")
        await el.screenshot(path=OUT)
        await b.close()
    print("OK", OUT)

asyncio.run(main())
