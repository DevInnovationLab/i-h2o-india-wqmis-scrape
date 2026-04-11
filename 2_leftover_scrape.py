"""
2_leftover_scrape.py - Re-scrape villages with broken/missing data

Some villages ended up with all-N/A fields (state, district, block, panchayat)
due to network issues during the main scrape. This script re-scrapes them using
the JJM VillageId direct lookup feature on the Village Profile page, which
bypasses the dropdown chain entirely.

How it works:
  1. Queries the DB for villages where panchayat = 'N/A'
  2. Extracts the numeric village ID from the 'V_{id}' fallback format
  3. For each village:
     a. Loads the Village Profile page to get a fresh ViewState
     b. Selects the "JJM VillageId" radio button (postback)
     c. Enters the ID and clicks Show
     d. Parses and updates the village record + all sub-tables

Uses parallel workers (ThreadPool) to speed things up.
"""
import requests
from bs4 import BeautifulSoup
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

URL = "https://ejalshakti.gov.in/JJM/JJMReports/profiles/rpt_VillageProfile.aspx"
DOMAIN = "https://ejalshakti.gov.in/JJM/"
DB_NAME = "jjm_parallel_data.db"
NUM_WORKERS = 20


# ============================================================
# PARSING HELPERS (same as 1_scraper.py)
# ============================================================

def get_text_safe(soup, elem_id):
    """Extract text from an element by ID, returning 'N/A' if not found."""
    el = soup.find(id=elem_id)
    return el.get_text(strip=True) if el else "N/A"

def safe_int(value):
    try: return int(str(value).replace(',', '').strip())
    except: return 0

def safe_float(value):
    try: return float(str(value).replace(',', '').strip())
    except: return 0.0


# ============================================================
# ASP.NET HELPERS
# ============================================================

def get_asp_vars(soup):
    """Extract ViewState fields from the page."""
    return {f: soup.find("input", {"name": f})["value"]
            for f in ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"]}


# ============================================================
# SINGLE VILLAGE SCRAPER (runs in a thread)
# ============================================================

def scrape_one_village(old_jjm_id, village_name):
    """
    Scrape a single village by JJM VillageId direct lookup.
    Returns (old_jjm_id, village_name, success_bool, result_data_or_none).
    """
    v_val = old_jjm_id.replace("V_", "")
    if not v_val.isdigit():
        return (old_jjm_id, village_name, False, "non-numeric ID")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Content-Type": "application/x-www-form-urlencoded"
    })

    try:
        # Step 1: Load the page to get a fresh ViewState
        page = session.get(URL, timeout=30)
        soup = BeautifulSoup(page.text, 'lxml')
        vs = get_asp_vars(soup)

        # Step 2: Select "JJM VillageId" radio button (triggers a postback)
        radio_data = {
            **vs,
            "__EVENTTARGET": "ctl00$CPHPage$ddVillagecodetype$1",
            "__EVENTARGUMENT": "",
            "ctl00$CPHPage$ddVillagecodetype": "2",
        }
        radio_res = session.post(URL, data=radio_data, timeout=30)
        if "__VIEWSTATE" not in radio_res.text:
            return (old_jjm_id, village_name, False, "radio postback failed")

        radio_soup = BeautifulSoup(radio_res.text, 'lxml')
        vs2 = get_asp_vars(radio_soup)

        # Step 3: Enter the JJM Village ID and click Show
        show_data = {
            **vs2,
            "__EVENTTARGET": "ctl00$CPHPage$btnShow",
            "__EVENTARGUMENT": "",
            "ctl00$CPHPage$ddVillagecodetype": "2",
            "ctl00$CPHPage$txtVillagelgdcode": v_val,
        }
        show_res = session.post(URL, data=show_data, timeout=30)
        show_soup = BeautifulSoup(show_res.text, 'lxml')

        # Step 4: Parse the result
        return parse_village_result(show_soup, old_jjm_id, village_name, session)

    except Exception as e:
        return (old_jjm_id, village_name, False, str(e))


def parse_village_result(soup, old_jjm_id, village_name, session):
    """
    Parse a loaded village profile and return the extracted data.
    Returns (old_jjm_id, village_name, success_bool, parsed_data_dict_or_reason).
    """
    raw_vil_str = get_text_safe(soup, "CPHPage_lblVillage")
    jjm_match = re.search(r'JJM VillageId\s*:\s*(\d+)', raw_vil_str)

    if not jjm_match:
        return (old_jjm_id, village_name, False, "no JJM VillageId in response")

    jjm_id = jjm_match.group(1)
    state = get_text_safe(soup, "CPHPage_lblState")
    if state == "N/A":
        return (old_jjm_id, village_name, False, "state still N/A")

    # --- Collect all data ---
    lgd = re.search(r'LGD Code\s*:\s*(\d+)', raw_vil_str)
    scheme_links = soup.find_all("a", id=re.compile(r"CPHPage_rptscheme_hyscheme_\d+"))

    data = {
        "jjm_id": jjm_id,
        "village": (
            jjm_id, village_name, state, get_text_safe(soup, "CPHPage_lblDistrict"),
            get_text_safe(soup, "CPHPage_lblBlock"), get_text_safe(soup, "CPHPage_lblPanchayat"),
            lgd.group(1) if lgd else "N/A",
            safe_int(get_text_safe(soup, "CPHPage_lblToptalPop")), safe_int(get_text_safe(soup, "CPHPage_lblSCPop")),
            safe_int(get_text_safe(soup, "CPHPage_lblSTPop")), safe_int(get_text_safe(soup, "CPHPage_lblHouseHolds")),
            safe_int(get_text_safe(soup, "CPHPage_lnk_HouseConnection")), get_text_safe(soup, "CPHPage_lblIsPWS"),
            get_text_safe(soup, "CPHPage_lblvillagestatus"), len(scheme_links)
        ),
        "water_sources": [],
        "water_quality": [],
        "schools_anganwadis": [],
        "public_institutions": [],
        "village_committees": [],
        "scheme_mappings": [],
        "schemes": [],
    }

    # Water Sources
    for row in soup.find_all("span", id=re.compile(r"CPHPage_rptSource_lblHabitationName_\d+")):
        idx = row['id'].split('_')[-1]
        data["water_sources"].append((
            jjm_id, row.text, get_text_safe(soup, f"CPHPage_rptSource_hyLocation_{idx}"),
            get_text_safe(soup, f"CPHPage_rptSource_lblSourceTypeCategory_{idx}"),
            get_text_safe(soup, f"CPHPage_rptSource_hyscheme_{idx}")))

    # Water Quality
    for row in soup.find_all("span", id=re.compile(r"CPHPage_rptWQMIS_lblLab_FTK_testing_\d+")):
        idx = row['id'].split('_')[-1]
        data["water_quality"].append((
            jjm_id, row.text, get_text_safe(soup, f"CPHPage_rptWQMIS_lblStatus_{idx}"),
            get_text_safe(soup, f"CPHPage_rptWQMIS_item_last_testing_date_{idx}"),
            get_text_safe(soup, f"CPHPage_rptWQMIS_item_Sample_location_{idx}"),
            get_text_safe(soup, f"CPHPage_rptWQMIS_item_remedial_action_status_{idx}")))

    # Schools & Anganwadis
    for prefix, label in [("CPHPage_rptSchool_", "School"), ("CPHPage_rptSchool_anganwadi_", "Anganwadi")]:
        for row in soup.find_all("span", id=re.compile(rf"{prefix}lblVoucherNumber_\d+")):
            idx = row['id'].split('_')[-1]
            data["schools_anganwadis"].append((
                jjm_id, label, get_text_safe(soup, f"{prefix}lblVoucherNumber_{idx}"),
                get_text_safe(soup, f"{prefix}lblVoucherDate_{idx}"),
                get_text_safe(soup, f"{prefix}lblActivityName_{idx}"),
                get_text_safe(soup, f"{prefix}Label1_{idx}"),
                get_text_safe(soup, f"{prefix}Label2_{idx}")))

    # Public Institutions
    for row in soup.find_all("span", id=re.compile(r"CPHPage_rpt_otherpublicinstitution_lblVoucherNumber_\d+")):
        idx = row['id'].split('_')[-1]
        data["public_institutions"].append((
            jjm_id, get_text_safe(soup, f"CPHPage_rpt_otherpublicinstitution_lblVoucherNumber_{idx}"),
            get_text_safe(soup, f"CPHPage_rpt_otherpublicinstitution_lblVoucherDate_{idx}"),
            get_text_safe(soup, f"CPHPage_rpt_otherpublicinstitution_lblActivityName_{idx}"),
            get_text_safe(soup, f"CPHPage_rpt_otherpublicinstitution_Label2_{idx}")))

    # Village Committees
    for row in soup.find_all("span", id=re.compile(r"CPHPage_rptVillageLevel_lblHabitationName_\d+")):
        idx = row['id'].split('_')[-1]
        data["village_committees"].append((
            jjm_id, get_text_safe(soup, f"CPHPage_rptVillageLevel_Label10_{idx}"), row.text,
            get_text_safe(soup, f"CPHPage_rptVillageLevel_Label9_{idx}"),
            get_text_safe(soup, f"CPHPage_rptVillageLevel_Label1_{idx}")))

    # Schemes
    for link in scheme_links:
        href = link.get('href')
        if not href: continue
        try:
            parent_row = link.find_parent('tr')
            scheme_code = parent_row.find_all('td')[1].get_text(strip=True)
        except: continue
        data["scheme_mappings"].append((jjm_id, scheme_code))

        # Scrape scheme details if needed (will check DB later during save)
        clean_url = DOMAIN + href.replace("../../", "")
        scheme_data = scrape_scheme(session, clean_url, scheme_code)
        if scheme_data:
            data["schemes"].append(scheme_data)

    return (old_jjm_id, village_name, True, data)


def scrape_scheme(session, clean_url, scheme_code):
    """Fetch and parse scheme details. Returns params tuple or None."""
    for attempt in range(3):
        try:
            res = session.get(clean_url, timeout=20)
            if res.status_code == 200:
                s_soup = BeautifulSoup(res.text, 'lxml')
                raw_n = get_text_safe(s_soup, "CPHPage_lblSchemeName")
                return (
                    scheme_code, raw_n.split('(')[0].strip(), get_text_safe(s_soup, "CPHPage_lblAgencyName"),
                    get_text_safe(s_soup, "CPHPage_lblZone"), get_text_safe(s_soup, "CPHPage_lblCircle"),
                    get_text_safe(s_soup, "CPHPage_lblNodalDivision"), get_text_safe(s_soup, "CPHPage_lblyear"),
                    get_text_safe(s_soup, "CPHPage_lblcompletedate"), safe_int(get_text_safe(s_soup, "CPHPage_lblServiceLevel")),
                    get_text_safe(s_soup, "CPHPage_lblWorkOrder"), get_text_safe(s_soup, "CPHPage_lblis_work_started"),
                    get_text_safe(s_soup, "CPHPage_lblStatus"), safe_float(get_text_safe(s_soup, "CPHPage_lblphyprogressscheme")),
                    safe_float(get_text_safe(s_soup, "CPHPage_lblEstimatedCost")), safe_float(get_text_safe(s_soup, "CPHPage_lblTotalExp")),
                    safe_float(get_text_safe(s_soup, "CPHPage_lblCentralExp")), safe_float(get_text_safe(s_soup, "CPHPage_lblStateExp")),
                    get_text_safe(s_soup, "CPHPage_lblTypeofSource")
                )
        except:
            time.sleep(1)
    return None


# ============================================================
# DB SAVE (runs in main thread after workers return)
# ============================================================

def save_result_to_db(conn, old_jjm_id, data):
    """Save parsed village data to the database."""
    c = conn.cursor()
    jjm_id = data["jjm_id"]

    # Delete old broken record if ID changed
    if old_jjm_id != jjm_id:
        c.execute("DELETE FROM villages WHERE jjm_village_id = ?", (old_jjm_id,))
        for table in ["water_sources", "water_quality", "schools_anganwadis",
                       "public_institutions", "village_committees", "village_scheme_mapping"]:
            c.execute(f"DELETE FROM {table} WHERE village_id = ?", (old_jjm_id,))

    # Insert village
    c.execute('INSERT OR REPLACE INTO villages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', data["village"])

    # Clean sub-tables
    for table in ["water_sources", "water_quality", "schools_anganwadis",
                   "public_institutions", "village_committees", "village_scheme_mapping"]:
        c.execute(f"DELETE FROM {table} WHERE village_id = ?", (jjm_id,))

    # Insert sub-table data
    for row in data["water_sources"]:
        c.execute('INSERT INTO water_sources (village_id, habitation, source_location, source_type, scheme_name) VALUES (?,?,?,?,?)', row)
    for row in data["water_quality"]:
        c.execute('INSERT INTO water_quality (village_id, test_type, status, test_date, sample_location, remedial_action) VALUES (?,?,?,?,?,?)', row)
    for row in data["schools_anganwadis"]:
        c.execute('INSERT INTO schools_anganwadis (village_id, type, habitation, name, category, classification, has_tap_connection) VALUES (?,?,?,?,?,?,?)', row)
    for row in data["public_institutions"]:
        c.execute('INSERT INTO public_institutions (village_id, habitation, name, category, has_tap_connection) VALUES (?,?,?,?,?)', row)
    for row in data["village_committees"]:
        c.execute('INSERT INTO village_committees (village_id, level, name, gender, designation) VALUES (?,?,?,?,?)', row)
    for row in data["scheme_mappings"]:
        c.execute("INSERT OR IGNORE INTO village_scheme_mapping (village_id, scheme_code) VALUES (?,?)", row)
    for row in data["schemes"]:
        c.execute("SELECT 1 FROM schemes WHERE scheme_code = ?", (row[0],))
        if not c.fetchone():
            c.execute('''INSERT OR REPLACE INTO schemes (
                scheme_code, scheme_name, agency, zone, circle, division,
                sanction_year, completion_date, service_level, work_order_date,
                work_started, status, physical_progress, estimated_cost,
                total_expenditure, central_expenditure, state_expenditure, source_type
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', row)

    conn.commit()


# ============================================================
# MAIN
# ============================================================

def main():
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")

    broken = conn.execute(
        "SELECT jjm_village_id, village_name FROM villages WHERE panchayat = 'N/A'"
    ).fetchall()

    if not broken:
        print("No broken villages found. Nothing to do.")
        conn.close()
        return

    print(f"Found {len(broken)} villages with missing data.")
    print(f"Using {NUM_WORKERS} parallel workers. Starting re-scrape...\n")

    success = 0
    failed = 0
    total = len(broken)

    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as pool:
        futures = {
            pool.submit(scrape_one_village, old_id, name): (old_id, name)
            for old_id, name in broken
        }

        for future in as_completed(futures):
            old_jjm_id, village_name, ok, result = future.result()

            if ok:
                save_result_to_db(conn, old_jjm_id, result)
                success += 1
                print(f"✅ [{success + failed}/{total}] {village_name} — fixed!")
            else:
                failed += 1
                print(f"⚠️ [{success + failed}/{total}] {village_name} — {result}")

    conn.close()
    print(f"\n{'='*50}")
    print(f"Re-scrape complete: {success} fixed, {failed} still broken out of {total} total.")


if __name__ == "__main__":
    main()
