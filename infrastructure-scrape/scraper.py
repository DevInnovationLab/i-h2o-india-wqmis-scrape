# This script was written to scrape the ejalshakti website. WIP.
import re
import time
import asyncio # For doing multiple processes at once (useful for opening many scheme links at once)
from playwright.async_api import async_playwright # Our main engine!

# Define our website
URL = "https://ejalshakti.gov.in/JJM/JJMReports/profiles/rpt_VillageProfile.aspx"

# Define our HTML Selectors so it has self-explanatory names
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
        print("❌ The page is still loading, but it took longer than 120 seconds so we must proceed:", e)

# Options Scraper Function (scrapes the state names, district names, panchayat names, etc)
async def get_options(page, selector_id):
    # Tries to get the dropdown menu
    try:
        await page.wait_for_selector(selector_id, state="attached", timeout=60000)
        # Javascript code that basically ensures the dropdown exists, and we have an option that does not have the word select (it takes time to populate the list)
        await page.wait_for_function(
            f"""() => {{
                const el = document.querySelector('{selector_id}');
                return el && Array.from(el.options).some(o => !o.text.includes('Select') && o.text.trim() !== '');
            }}""",
            timeout=60000
        )
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
        cost = "N/A"
        try:
            # We request the raw HTML code instead of opening a tab
            # This shares cookies with the main browser window, so the session persists
            response = await context.request.get(url, timeout=60000)
            
            if response.ok:
                body = await response.text()
                
                # We use a simple regex to find the number inside the specific HTML tag (BeautifulSoup is more robust, but re is simpler as the website is static)
                # Remember that we can add more stuff! This is just to test if it works
                # Looks for: <span id="CPHPage_lblEstimatedCost">123.45</span>
                match = re.search(r'id="CPHPage_lblEstimatedCost"[^>]*>(.*?)</span>', body)
                
                if match:
                    cost = match.group(1)
                    print(f"             Scheme cost: {cost}")
            else:
                print(f"             ❌ Server returned status {response.status}")
                
        except Exception as e:
            cost = "Error"
            print(f"             ❌ Request failed: {e}")
        
        return cost

# Village Recovery function (if the reset/show button fails we attempt a full reload and reselect)
async def full_recovery(page, current_state, current_district, current_block, current_panchayat):
    print("             ❌ Attempting page reload for recovery...")
    try:
        await page.reload(wait_until="domcontentloaded", timeout=120000) # A regular reload
        print("             🔄 Page reloaded successfully.")
        return
    except Exception as e:
        print("             ❌ Simple reload failed, attempting full recovery:", e) # We visit the site from scratch and attempt to go to the next village
        try:
            for attempt in range(3):
                try:
                    await page.goto(URL, timeout=120000)
                    break
                except Exception as _:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1)

            await select_and_wait(page, SELECTORS["state"], current_state)
            await select_and_wait(page, SELECTORS["district"], current_district)
            await select_and_wait(page, SELECTORS["block"], current_block)
            await select_and_wait(page, SELECTORS["panchayat"], current_panchayat)
            print("             🔄 Full recovery successful.")
        except Exception as e2:
            print("             ❌ Full recovery failed. Something is terribly wrong! Stopping script to prevent incorrect scraping:", e2)
            raise SystemExit(1)

# Village Processor Function (this is the main workhorse as this function processes the villages)
async def process_village(context, page, village_name, current_state, current_district, current_block, current_panchayat):

    # 1. We click the show button
    try:
        async with page.expect_response(lambda response: response.url == URL and response.request.method == "POST", timeout=60000):
            await page.click(SELECTORS["show_btn"])
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=120000) # We wait until the content is loaded because the loading here refreshes the page (not a spinner) 
        except Exception as e:
            print("             ❌ DOM load wait failed, continuing anyway:", e)
    except Exception as e:
        print(f"             ❌ Show click failed for {village_name}, attempting recovery:", e)
        await full_recovery(page, current_state, current_district, current_block, current_panchayat)
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
        # We select the next village
        try:
            await page.wait_for_selector(SELECTORS["reset_btn"], state="attached", timeout=60000)
            await page.click(SELECTORS["reset_btn"])
            await page.wait_for_selector(SELECTORS["show_btn"], state="attached", timeout=60000)
        except:
            await full_recovery(page, current_state, current_district, current_block, current_panchayat)
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
        results = await asyncio.gather(*tasks, return_exceptions=True)
        print(f"             ✅ Scraped {len(results)} scheme(s)")
    
    # Reset to prepare for next village
    try:
        await page.wait_for_selector(SELECTORS["reset_btn"], state="attached", timeout=60000)
        await page.click(SELECTORS["reset_btn"])
        await page.wait_for_selector(SELECTORS["show_btn"], state="attached", timeout=60000)
    except:
        await full_recovery(page, current_state, current_district, current_block, current_panchayat)
        return

# --- MAIN FUNCTION --- 
async def main():
    start_time = None
    processed = 0
    # Track where we are in the hierarchy (useful for when the website crashes and we perform a complete reload)
    current_state = None
    current_district = None
    current_block = None
    current_panchayat = None
    current_village = None
    # Start Playwright
    async with async_playwright() as p:
        # Launch Browser and open tab
        browser = await p.chromium.launch(headless=True) # Change to headless=False if you want to see the actual Chromium browser
        context = await browser.new_context()
        page = await context.new_page()
        print("Connecting...")
        for attempt in range(3):
            try:
                await page.goto(URL, timeout=120000)
                print("Page loaded!")
                break
            except Exception as e:
                print(f"❌ Page load failed (attempt {attempt+1}/3):", e)
                await asyncio.sleep(1)

        else:
            print("❌ Page failed to load after 3 attempts. Please retry later.")
            await browser.close()
            return

        # 1.  LEVEL 1: STATE
        states = await get_options(page, SELECTORS["state"])

        # 2. Loop Through States. If you want to test a certain state/district/block, you can, for example, do state[1:2] which will check the 2nd state on the dropdown.
        # Pro tip: Kerala (15th states) is a good state to test because the villages have so many schemes.
        for state in states:
            current_state = state
            print(f"[STATE] {state}")
            await select_and_wait(page, SELECTORS["state"], state)

            # 3. LEVEL 2: DISTRICT
            districts = await get_options(page, SELECTORS["district"])

            # 4. Loop Through Districts
            for district in districts:
                current_district = district
                print(f"  └─ [DISTRICT] {district}")
                await select_and_wait(page, SELECTORS["district"], district)

                # 5. LEVEL 3: BLOCK
                blocks = await get_options(page, SELECTORS["block"])

                # 6. Loop Through blocks
                for block in blocks:
                    current_block = block
                    print(f"     └─ [BLOCK] {block}")
                    await select_and_wait(page, SELECTORS["block"], block)

                    # 7. LEVEL 4: PANCHAYAT
                    panchayats = await get_options(page, SELECTORS["panchayat"])

                    # 8. Loop Through Panchayats
                    for pan in panchayats:
                        current_panchayat = pan
                        print(f"        └─ [PANCHAYAT] {pan}")
                        await select_and_wait(page, SELECTORS["panchayat"], pan)

                        # 9. LEVEL 5: VILLAGE
                        villages = await get_options(page, SELECTORS["village"])

                        # 10. Loop Through Villages with a timer
                        for vil in villages:
                            current_village = vil
                            print(f"            └─ [VILLAGE] {vil}")
                            await select_and_wait(page, SELECTORS["village"], vil)
                            if start_time is None:
                                start_time = time.time()   # start timing from first village only
                            await process_village(context, page, vil, current_state, current_district, current_block, current_panchayat)
                            processed += 1
                            elapsed = time.time() - start_time
                            avg = elapsed / processed
                            print(f"             ⏱️ Processed {processed} villages | Avg {avg:.2f}s each | Elapsed {elapsed/60:.2f} min")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())