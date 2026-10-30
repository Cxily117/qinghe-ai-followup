import asyncio
from playwright.async_api import async_playwright

HTML = r"C:\Users\Crystal\WorkBuddy\医院随访\arch.html"
OUT = r"C:\Users\Crystal\WorkBuddy\医院随访\随访系统架构图.png"

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--no-sandbox"])
        pg = await b.new_page(device_scale_factor=2, viewport={"width": 1000, "height": 720})
        await pg.goto("file:///" + HTML)
        await pg.wait_for_timeout(400)
        el = await pg.query_selector("svg")
        await el.screenshot(path=OUT)
        await b.close()
    print("OK", OUT)

asyncio.run(main())
