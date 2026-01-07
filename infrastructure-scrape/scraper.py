# This script was written to scrape the ejalshakti website. WIP.

import asyncio # For doing multiple processes at once (useful for opening many scheme links at once)
import random # Will be used for defining our human sleep function
from playwright.async_api import async_playwright # Our main engine!

# Define our website
URL = "https://ejalshakti.gov.in/JJM/JJMReports/profiles/rpt_VillageProfile.aspx"

# Define our CSS Selectors so it has self-explanatory names
SELECTORS = {
    "state": "#CPHPage_ddState",
    "district": "#ddDistrict",
    "block": "#ddblock",
    "panchayat": "#ddPanchayat",
    "village": "#ddVillage",
    "show_btn": "#CPHPage_btnShow",
    "reset_btn": "#CPHPage_btnReset",
    "pop_label": "#CPHPage_lblToptalPop", 
    "scheme_links": "a[id*='CPHPage_rptscheme_hyscheme']"
}

# --- HELPER FUNCTION --- 

# Pause Function (we add variability to our clicks to "hopefully" avoid being detected as a bot)
async def human_sleep(min_s=0.2, max_s=0.6):
    delay = random.uniform(min_s, max_s)
    await asyncio.sleep(delay)

# Selector Function (this function is used to navigate the website)
async def select_and_wait(page, selector_id, option_label):
    # We wait for a complete POST response
    try:
        async with page.expect_response(lambda response: response.url == URL and response.request.method == "POST", timeout=60000):
         await page.select_option(selector_id, label=option_label)
    except Exception as e:
        print("❌ POST never completed, continuing anyway:", e)
    try:
    # We wait for 120 seconds until the spinning gif on the page disappears
        await page.wait_for_load_state("domcontentloaded", timeout=120000)
    except Exception as e:
        print("❌ The page is still loading, but it took longer than 10 seconds so we must proceed:", e)
    # Add small variability to speed
    await human_sleep()

# Options Scraper Function (scrapes the state names, district names, panchayat names, etc)
async def get_options(page, selector_id):
    # Tries to get the dropdown menu
        try:
            await page.wait_for_selector(selector_id, state="attached", timeout=10000)
            try:
                await page.wait_for_function(f"document.querySelector('{selector_id}').options.length > 1", timeout=10000)
            except:
                pass
        
            # Scrapes and cleans them
            options = await page.locator(f"{selector_id} option").all_text_contents()
            cleaned_options = [o.strip() for o in options if "Select" not in o and o.strip() != ""]
            return cleaned_options
        except Exception as e:
            print("❌ get_options error:", e)
            return []

# Parallel Scheme Extractor Function (this function handles one tab and we will run many of these at once)
async def scrape_single_scheme(context, url, semaphore):
    async with semaphore:
        page = await context.new_page()
        cost = "N/A"
        try:
            await page.goto(url, timeout=60000) # Give it 60s to load
            el = page.locator("#CPHPage_lblEstimatedCost") # Currently this function only scrapes the scheme cost, but we could add more!
            if await el.count() > 0:
                cost = await el.inner_text()
                # Use this to test if it's correctly scraping the scheme cost
                print(f"             Scheme cost: {cost}")
        except:
            cost = "Error"
            print("             ❌ Couldn't calculate cost!")
        
        await page.close()
        return cost

# Village Processor Function (this is the main workhorse as this function processes the villages)
async def process_village(context, page, village_name):

    # 1. We click the show button
    try:
        async with page.expect_response(lambda response: response.url == URL and response.request.method == "POST", timeout=60000):
            await page.click(SELECTORS["show_btn"])
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=120000) # We wait until the content is loaded because the loading here refreshes the page (not a spinner) 
        except Exception as e:
            print("             ❌ DOM load wait failed, continuing anyway:", e)
    except Exception as e:
        print(f"             ❌ Show click failed for {village_name}:", e)
        return
    
    # 2. Scrape Basic Village Info
    pop = "N/A"
    if await page.locator(SELECTORS["pop_label"]).count() > 0:
        pop = await page.locator(SELECTORS["pop_label"]).inner_text()
        # Use this to test if it's correctly scraping the basic info
        print(f"             Population: {pop}")
        print("             ✅ Basic info scraped!")

    # 3. Scrape Schemes
    try:
        await page.locator(SELECTORS["scheme_links"]).first.wait_for(state="attached", timeout=5000)
    except:
        print("             No scheme links appeared after 5 seconds")
        await page.click(SELECTORS["reset_btn"])
        await page.wait_for_selector(SELECTORS["show_btn"], state="attached")
        return

    links = await page.locator(SELECTORS["scheme_links"]).all()
    print(f"             Found {len(links)} scheme(s) to check!")

    # Here we execute our asynchronous (parallel extraction) feature
    semaphore = asyncio.Semaphore(10)
    tasks = []

    for link in links:
        # Get the sub url and combine it to produce the full url
        href = await link.get_attribute("href")
        if href != "" and "../../" in href:
            clean_suffix = href.replace("../../", "")
            full_url = "https://ejalshakti.gov.in/JJM/" + clean_suffix
            tasks.append(scrape_single_scheme(context, full_url, semaphore))
    
    if tasks:
        results = await asyncio.gather(*tasks)
        print(f"             ✅ Scraped {len(results)} scheme(s)")
    
    # We close our current village here
    try:
        await page.click(SELECTORS["reset_btn"])
        await page.wait_for_selector(SELECTORS["show_btn"], state="attached", timeout = 10000)
    except Exception as e:
        print("             ❌ RESET FAILED. PLEASE RERUN THE CODE:", e)
        raise SystemExit(1)

# --- MAIN FUNCTION --- 
async def main():
    # Start Playwright
    async with async_playwright() as p:
        # Launch Browser and open tab
        browser = await p.chromium.launch(headless=True) # Change to headless=False if you want to see the actual Chromium browser
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(URL, timeout=60000)
            print("Page loaded!")
        except Exception as e:
            print("❌ Page failed to load, please try again:", e)
            await browser.close()
            return

        # 1.  LEVEL 1: STATE
        states = await get_options(page, SELECTORS["state"])

        # 2. Loop Through States. If you want to test a certain state/district/block, you can, for example, do state[1:2] which will check the 2nd state on the dropdown.
        # Pro tip: Kerala (15th states) is a good state to test because the villages have so many schemes.
        for state in states:
            print(f"[STATE] {state}")
            await select_and_wait(page, SELECTORS["state"], state)

            # 3. LEVEL 2: DISTRICT
            districts = await get_options(page, SELECTORS["district"])

            # 4. Loop Through Districts
            for district in districts:
                print(f"  └─ [DISTRICT] {district}")
                await select_and_wait(page, SELECTORS["district"], district)

                # 5. LEVEL 3: BLOCK
                blocks = await get_options(page, SELECTORS["block"])

                # 6. Loop Through blocks
                for block in blocks:
                    print(f"     └─ [BLOCK] {block}")
                    await select_and_wait(page, SELECTORS["block"], block)

                    # 7. LEVEL 4: PANCHAYAT
                    panchayats = await get_options(page, SELECTORS["panchayat"])

                    # 8. Loop Through Panchayats
                    for pan in panchayats:
                        print(f"        └─ [PANCHAYAT] {pan}")
                        await select_and_wait(page, SELECTORS["panchayat"], pan)

                        # 9. LEVEL 5: VILLAGE
                        villages = await get_options(page, SELECTORS["village"])

                        # 10. Loop Through Villages
                        for vil in villages:
                            print(f"            └─ [VILLAGE] {vil}")
                            await select_and_wait(page, SELECTORS["village"], vil)
                            await process_village(context, page, vil)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())