"""
1_scraper.py - Parallel Village Profile Scraper

Processes the panchayat queue built by 0_preliminary.py. For each PENDING panchayat,
navigates the ASP.NET dropdown chain to load every village in that panchayat, then
extracts comprehensive village-level data:

  - Village demographics (population, households, tap connections)
  - Water infrastructure schemes (with detailed per-scheme scraping)
  - Water sources and quality test results
  - Schools, Anganwadis, and public institutions
  - Village water committee members

Architecture:
  - 100 parallel scraper processes (via Pebble ProcessPool)
  - 1 dedicated DB writer thread to serialize all SQLite writes
  - Work/rest cycles (20 min scrape, 2-3 min cooldown) to avoid IP blocking
  - Automatic database backups during cooldown periods
  - Tasks are batched by block (300 blocks per batch)

Output: Populates villages, schemes, water_sources, water_quality,
        schools_anganwadis, public_institutions, village_committees tables.
"""
import requests
from bs4 import BeautifulSoup
import re
import sqlite3
import time
import random
import threading
import multiprocessing
from concurrent.futures import TimeoutError, as_completed
from pebble import ProcessPool

# ============================================================
# CONFIG
# ============================================================
URL = "https://ejalshakti.gov.in/JJM/JJMReports/profiles/rpt_VillageProfile.aspx"
DOMAIN = "https://ejalshakti.gov.in/JJM/"
DB_NAME = "jjm_parallel_data.db"
NUM_SCRAPER_PROCESSES = 100       # Number of parallel scraper workers
BLOCK_BATCH_SIZE = 300            # Blocks per batch (keep high to avoid idle workers from "stragglers")
WORK_DURATION = 1200              # Seconds of scraping before a cooldown (20 minutes)


# ============================================================
# PARSING HELPERS
# ============================================================

def get_text_safe(soup, elem_id):
    """Extract text from an element by ID, returning 'N/A' if not found."""
    el = soup.find(id=elem_id)
    return el.get_text(strip=True) if el else "N/A"

def safe_int(value):
    """Convert a string (possibly with commas) to int, defaulting to 0."""
    try: return int(str(value).replace(',', '').strip())
    except: return 0

def safe_float(value):
    """Convert a string (possibly with commas) to float, defaulting to 0.0."""
    try: return float(str(value).replace(',', '').strip())
    except: return 0.0


# ============================================================
# SCRAPER WORKER
# ============================================================

class ScraperWorker:
    """
    Handles scraping for a single block. Each worker process creates its own
    ScraperWorker instance with its own requests session.

    Workflow per block:
      1. Navigate dropdowns: State -> District -> Block
      2. Loop through each panchayat in the block
      3. For each panchayat, get the village list and scrape each village
      4. Follow scheme links to get detailed scheme data
      5. Send all parsed data to the DB writer queue
    """
    def __init__(self, shared_scheme_cache):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded"})
        self.shared_scheme_cache = shared_scheme_cache

    def get_asp_vars(self, soup):
        """Extract ASP.NET ViewState fields needed for valid POST requests."""
        return {f: soup.find("input", {"name": f})["value"] for f in ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"]}

    def post_step(self, target, payload, vs):
        """
        Submit an ASP.NET postback and return (soup, new_viewstate).
        Retries up to 3 times with exponential backoff on failure.
        Returns (None, None) if all attempts fail.
        """
        for attempt in range(3):
            try:
                data = {**vs, "__EVENTTARGET": target.replace("_", "$"), "__EVENTARGUMENT": "", **payload}
                res = self.session.post(URL, data=data, timeout=30)
                if res.status_code == 200 and "__VIEWSTATE" in res.text:
                    soup = BeautifulSoup(res.text, "lxml")
                    return soup, self.get_asp_vars(soup)
            except:
                pass
            time.sleep(3 * (attempt + 1))
        return None, None

    def scrape_scheme_details(self, clean_url, scheme_code):
        """
        Follow a scheme link and extract detailed scheme information
        (agency, costs, progress, source type, etc.). Sends an INSERT
        to the DB writer queue.
        """
        s_res = None
        for attempt in range(3):
            try:
                s_res = self.session.get(clean_url, timeout=20)
                if s_res.status_code == 200: break
            except: time.sleep(1)

        if s_res and s_res.status_code == 200:
            try:
                s_soup = BeautifulSoup(s_res.text, 'lxml')
                raw_n = get_text_safe(s_soup, "CPHPage_lblSchemeName")

                sql = '''INSERT OR REPLACE INTO schemes (
                            scheme_code, scheme_name, agency, zone, circle, division,
                            sanction_year, completion_date, service_level, work_order_date,
                            work_started, status, physical_progress, estimated_cost,
                            total_expenditure, central_expenditure, state_expenditure, source_type
                         ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'''

                params = (
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
                write_queue.put(("INSERT_GENERIC", ("schemes", sql, params)))
            except Exception as e:
                print(f"⚠️ Error parsing details for Scheme {scheme_code}: {e}")

    def parse_village(self, html, v_name, v_val):
        """
        Parse a loaded village profile page and extract all data into the DB writer queue.
        Handles: village info, water sources, water quality, schools/anganwadis,
        public institutions, committees, and scheme links.
        """
        soup = BeautifulSoup(html, 'lxml')
        raw_vil_str = get_text_safe(soup, "CPHPage_lblVillage")
        jjm_match = re.search(r'JJM VillageId\s*:\s*(\d+)', raw_vil_str)
        jjm_id = jjm_match.group(1) if jjm_match else f"V_{v_val}"

        # Count scheme links (used for integrity checking later)
        scheme_links = soup.find_all("a", id=re.compile(r"CPHPage_rptscheme_hyscheme_\d+"))
        expected_schemes = len(scheme_links)

        # --- Village Main Data ---
        lgd = re.search(r'LGD Code\s*:\s*(\d+)', raw_vil_str)
        v_data = (
            jjm_id, v_name, get_text_safe(soup, "CPHPage_lblState"), get_text_safe(soup, "CPHPage_lblDistrict"),
            get_text_safe(soup, "CPHPage_lblBlock"), get_text_safe(soup, "CPHPage_lblPanchayat"), lgd.group(1) if lgd else "N/A",
            safe_int(get_text_safe(soup, "CPHPage_lblToptalPop")), safe_int(get_text_safe(soup, "CPHPage_lblSCPop")),
            safe_int(get_text_safe(soup, "CPHPage_lblSTPop")), safe_int(get_text_safe(soup, "CPHPage_lblHouseHolds")),
            safe_int(get_text_safe(soup, "CPHPage_lnk_HouseConnection")), get_text_safe(soup, "CPHPage_lblIsPWS"),
            get_text_safe(soup, "CPHPage_lblvillagestatus"), expected_schemes
        )
        write_queue.put(("INSERT_VILLAGE", v_data))

        # Clear old sub-table data for this village (in case of re-scrape)
        tables = ["water_sources", "water_quality", "schools_anganwadis", "public_institutions", "village_committees", "village_scheme_mapping"]
        for table in tables:
            write_queue.put(("INSERT_GENERIC", (table, f"DELETE FROM {table} WHERE village_id=?", (jjm_id,))))

        # --- Water Sources ---
        for row in soup.find_all("span", id=re.compile(r"CPHPage_rptSource_lblHabitationName_\d+")):
            idx = row['id'].split('_')[-1]
            sql = 'INSERT INTO water_sources (village_id, habitation, source_location, source_type, scheme_name) VALUES (?,?,?,?,?)'
            params = (jjm_id, row.text, get_text_safe(soup, f"CPHPage_rptSource_hyLocation_{idx}"),
                      get_text_safe(soup, f"CPHPage_rptSource_lblSourceTypeCategory_{idx}"), get_text_safe(soup, f"CPHPage_rptSource_hyscheme_{idx}"))
            write_queue.put(("INSERT_GENERIC", ("water_sources", sql, params)))

        # --- Water Quality ---
        for row in soup.find_all("span", id=re.compile(r"CPHPage_rptWQMIS_lblLab_FTK_testing_\d+")):
            idx = row['id'].split('_')[-1]
            sql = 'INSERT INTO water_quality (village_id, test_type, status, test_date, sample_location, remedial_action) VALUES (?,?,?,?,?,?)'
            params = (jjm_id, row.text, get_text_safe(soup, f"CPHPage_rptWQMIS_lblStatus_{idx}"), get_text_safe(soup, f"CPHPage_rptWQMIS_item_last_testing_date_{idx}"),
                      get_text_safe(soup, f"CPHPage_rptWQMIS_item_Sample_location_{idx}"), get_text_safe(soup, f"CPHPage_rptWQMIS_item_remedial_action_status_{idx}"))
            write_queue.put(("INSERT_GENERIC", ("water_quality", sql, params)))

        # --- Schools & Anganwadis ---
        for prefix, label in [("CPHPage_rptSchool_", "School"), ("CPHPage_rptSchool_anganwadi_", "Anganwadi")]:
            for row in soup.find_all("span", id=re.compile(rf"{prefix}lblVoucherNumber_\d+")):
                idx = row['id'].split('_')[-1]
                sql = 'INSERT INTO schools_anganwadis (village_id, type, habitation, name, category, classification, has_tap_connection) VALUES (?,?,?,?,?,?,?)'
                params = (jjm_id, label, get_text_safe(soup, f"{prefix}lblVoucherNumber_{idx}"), get_text_safe(soup, f"{prefix}lblVoucherDate_{idx}"),
                          get_text_safe(soup, f"{prefix}lblActivityName_{idx}"), get_text_safe(soup, f"{prefix}Label1_{idx}"), get_text_safe(soup, f"{prefix}Label2_{idx}"))
                write_queue.put(("INSERT_GENERIC", ("schools_anganwadis", sql, params)))

        # --- Public Institutions ---
        for row in soup.find_all("span", id=re.compile(r"CPHPage_rpt_otherpublicinstitution_lblVoucherNumber_\d+")):
            idx = row['id'].split('_')[-1]
            sql = 'INSERT INTO public_institutions (village_id, habitation, name, category, has_tap_connection) VALUES (?,?,?,?,?)'
            params = (jjm_id, get_text_safe(soup, f"CPHPage_rpt_otherpublicinstitution_lblVoucherNumber_{idx}"), get_text_safe(soup, f"CPHPage_rpt_otherpublicinstitution_lblVoucherDate_{idx}"),
                      get_text_safe(soup, f"CPHPage_rpt_otherpublicinstitution_lblActivityName_{idx}"), get_text_safe(soup, f"CPHPage_rpt_otherpublicinstitution_Label2_{idx}"))
            write_queue.put(("INSERT_GENERIC", ("public_institutions", sql, params)))

        # --- Village Committees ---
        for row in soup.find_all("span", id=re.compile(r"CPHPage_rptVillageLevel_lblHabitationName_\d+")):
            idx = row['id'].split('_')[-1]
            sql = 'INSERT INTO village_committees (village_id, level, name, gender, designation) VALUES (?,?,?,?,?)'
            params = (jjm_id, get_text_safe(soup, f"CPHPage_rptVillageLevel_Label10_{idx}"), row.text, get_text_safe(soup, f"CPHPage_rptVillageLevel_Label9_{idx}"), get_text_safe(soup, f"CPHPage_rptVillageLevel_Label1_{idx}"))
            write_queue.put(("INSERT_GENERIC", ("village_committees", sql, params)))

        # --- Schemes ---
        # Follow each scheme link to scrape detailed data. Uses a shared cache
        # across processes to avoid re-scraping the same scheme.
        for link in scheme_links:
            href = link.get('href')
            if not href: continue

            try:
                parent_row = link.find_parent('tr')
                scheme_code = parent_row.find_all('td')[1].get_text(strip=True)
            except: continue

            # Record village-to-scheme mapping
            write_queue.put(("INSERT_GENERIC", (
                "village_scheme_mapping",
                "INSERT OR IGNORE INTO village_scheme_mapping (village_id, scheme_code) VALUES (?,?)",
                (jjm_id, scheme_code)
            )))

            # Only scrape scheme details if not already cached
            if scheme_code not in self.shared_scheme_cache:
                clean_url = DOMAIN + href.replace("../../", "")
                self.scrape_scheme_details(clean_url, scheme_code)
                self.shared_scheme_cache.append(scheme_code)

    def process_block(self, block_task):
        """
        Navigate to a specific block and scrape all PENDING panchayats within it.
        Maintains the ASP.NET ViewState chain across dropdown selections.
        Each successfully scraped panchayat is checkpointed via MARK_DONE.
        """
        state_val, dist_val, block_val = block_task

        # Get the list of panchayats to scrape in this block
        conn = sqlite3.connect(DB_NAME)
        panchayats = conn.execute(
            "SELECT pan_val, pan_name FROM queue WHERE block_val=? AND status='PENDING'",
            (block_val,)
        ).fetchall()
        conn.close()

        if not panchayats: return

        try:
            # --- Navigate the dropdown chain ---
            res = self.session.get(URL, timeout=30)
            vs_current = self.get_asp_vars(BeautifulSoup(res.text, "lxml"))

            # Select State
            s_dist, vs_current = self.post_step("ctl00$CPHPage$ddState", {"ctl00$CPHPage$ddState": state_val}, vs_current)
            if not vs_current: return

            # Select District
            s_block, vs_current = self.post_step("ctl00$CPHPage$ddDistrict", {"ctl00$CPHPage$ddState": state_val, "ctl00$CPHPage$ddDistrict": dist_val}, vs_current)
            if not vs_current: return

            # Select Block
            s_pan_list, vs_current = self.post_step("ctl00$CPHPage$ddblock", {
                "ctl00$CPHPage$ddState": state_val, "ctl00$CPHPage$ddDistrict": dist_val, "ctl00$CPHPage$ddblock": block_val
            }, vs_current)
            if not vs_current: return

            # --- Loop through panchayats in this block ---
            for pan_val, pan_name in panchayats:

                # Select Panchayat
                s_pan, vs_current = self.post_step("ctl00$CPHPage$ddblock", {
                    "ctl00$CPHPage$ddState": state_val, "ctl00$CPHPage$ddDistrict": dist_val,
                    "ctl00$CPHPage$ddblock": block_val, "ctl00$CPHPage$ddPanchayat": pan_val
                }, vs_current)

                if not vs_current:
                    print(f"❌ Chain broken at {pan_name}. Ending block task.")
                    return

                # Get village dropdown
                s_vil, vs_current = self.post_step("ctl00$CPHPage$ddPanchayat", {
                    "ctl00$CPHPage$ddState": state_val, "ctl00$CPHPage$ddDistrict": dist_val,
                    "ctl00$CPHPage$ddblock": block_val, "ctl00$CPHPage$ddPanchayat": pan_val
                }, vs_current)

                vil_select = s_vil.find("select", id=re.compile("ddVillage$")) if s_vil else None
                if not vil_select:
                    print(f"⚠️ Page sync error for {pan_name}. Moving to next.")
                    continue

                # Scrape each village in this panchayat
                villages = [(o.text.strip(), o["value"]) for o in vil_select.find_all("option") if o["value"] not in ["0", "-1"]]

                for v_name, v_val in villages:
                    p_final = {
                        "ctl00$CPHPage$ddState": state_val, "ctl00$CPHPage$ddDistrict": dist_val,
                        "ctl00$CPHPage$ddblock": block_val, "ctl00$CPHPage$ddPanchayat": pan_val,
                        "ctl00$CPHPage$ddVillage": v_val, "ctl00$CPHPage$btnShow": "Show"
                    }
                    report_res = self.session.post(URL, data={**vs_current, "__EVENTTARGET": "", **p_final}, timeout=30)
                    self.parse_village(report_res.text, v_name, v_val)

                # Checkpoint: mark this panchayat as done
                write_queue.put(("MARK_DONE", (block_val, pan_val, pan_name)))

        except Exception as e:
            print(f"❌ Error on Block {block_val}: {e}")


# ============================================================
# DB WRITER (runs as a background thread in the main process)
# ============================================================

def db_writer_listener():
    """
    Dedicated database writer that serializes all writes from worker processes.
    Receives tasks via a multiprocessing Queue and processes them one at a time.
    This prevents SQLite contention from 100 concurrent workers.

    Task types:
      - INSERT_VILLAGE:  insert/replace a village record
      - INSERT_GENERIC:  execute any SQL (schemes, water sources, etc.)
      - MARK_DONE:       update a panchayat's status to DONE + commit
      - STOP:            shut down the writer
    """
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    c = conn.cursor()

    # Get starting progress for the log
    try:
        c.execute("SELECT COUNT(*) FROM queue")
        total_tasks = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM queue WHERE status='DONE'")
        tasks_done = c.fetchone()[0]
        print(f"📊 Tracking Progress: {tasks_done:,}/{total_tasks:,} panchayats already done.")
    except:
        total_tasks, tasks_done = 1, 0

    print("💾 DB Writer started...")

    while True:
        try:
            task_type, data = write_queue.get()

            if task_type == "STOP":
                conn.commit(); conn.close(); break

            elif task_type == "INSERT_VILLAGE":
                c.execute('INSERT OR REPLACE INTO villages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', data)

            elif task_type == "INSERT_GENERIC":
                table_name, sql, params = data
                c.execute(sql, params)

            elif task_type == "MARK_DONE":
                block_val, pan_val, pan_name = data
                c.execute("UPDATE queue SET status = 'DONE' WHERE block_val = ? AND pan_val = ?", (block_val, pan_val))
                conn.commit()
                tasks_done += 1
                percent = (tasks_done / total_tasks) * 100 if total_tasks > 0 else 0
                print(f"✅ [{tasks_done:,} scraped out of {total_tasks:,} panchayats | {percent:.2f}%] Finished: {pan_name}")

        except Exception as e:
            print(f"❌ DB Writer Error: {e}")


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def init_db():
    """Create all data tables if they don't already exist."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS villages (
        jjm_village_id TEXT PRIMARY KEY, village_name TEXT, state TEXT, district TEXT,
        block TEXT, panchayat TEXT, lgd_code TEXT, population_total INTEGER,
        population_sc INTEGER, population_st INTEGER, households_total INTEGER,
        tap_connections INTEGER, is_pws TEXT, jjm_status TEXT, expected_scheme_count INTEGER)''')

    c.execute('''CREATE TABLE IF NOT EXISTS schemes (
        scheme_code TEXT PRIMARY KEY, scheme_name TEXT, agency TEXT, zone TEXT, circle TEXT, division TEXT,
        sanction_year TEXT, completion_date TEXT, service_level INTEGER, work_order_date TEXT,
        work_started TEXT, status TEXT, physical_progress REAL, estimated_cost REAL,
        total_expenditure REAL, central_expenditure REAL, state_expenditure REAL, source_type TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS village_scheme_mapping (
        village_id TEXT, scheme_code TEXT,
        PRIMARY KEY (village_id, scheme_code))''')

    c.execute('''CREATE TABLE IF NOT EXISTS water_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT, village_id TEXT, habitation TEXT,
        source_location TEXT, source_type TEXT, scheme_name TEXT,
        FOREIGN KEY(village_id) REFERENCES villages(jjm_village_id))''')

    c.execute('''CREATE TABLE IF NOT EXISTS water_quality (
        id INTEGER PRIMARY KEY AUTOINCREMENT, village_id TEXT, test_type TEXT,
        status TEXT, test_date TEXT, sample_location TEXT, remedial_action TEXT,
        FOREIGN KEY(village_id) REFERENCES villages(jjm_village_id))''')

    c.execute('''CREATE TABLE IF NOT EXISTS schools_anganwadis (
        id INTEGER PRIMARY KEY AUTOINCREMENT, village_id TEXT, type TEXT,
        habitation TEXT, name TEXT, category TEXT, classification TEXT, has_tap_connection TEXT,
        FOREIGN KEY(village_id) REFERENCES villages(jjm_village_id))''')

    c.execute('''CREATE TABLE IF NOT EXISTS public_institutions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, village_id TEXT, habitation TEXT,
        name TEXT, category TEXT, has_tap_connection TEXT,
        FOREIGN KEY(village_id) REFERENCES villages(jjm_village_id))''')

    c.execute('''CREATE TABLE IF NOT EXISTS village_committees (
        id INTEGER PRIMARY KEY AUTOINCREMENT, village_id TEXT, level TEXT,
        name TEXT, gender TEXT, designation TEXT,
        FOREIGN KEY(village_id) REFERENCES villages(jjm_village_id))''')
    conn.commit(); conn.close()


# ============================================================
# MULTIPROCESSING HELPERS
# ============================================================

def init_worker_process(q, cache):
    """Initialize globals in each worker process (called by ProcessPool initializer)."""
    global write_queue
    write_queue = q
    global shared_scheme_cache
    shared_scheme_cache = cache

def run_scraper_task(task):
    """Entry point for each worker process. Creates a ScraperWorker and processes one block."""
    worker = ScraperWorker(shared_scheme_cache)
    worker.process_block(task)
    return task[2]  # Returns block_val


# ============================================================
# MAIN
# ============================================================

def main():
    """
    Main scraping loop with work/rest cycles.

    1. Grab a batch of PENDING blocks from the queue
    2. Dispatch them to the process pool
    3. After WORK_DURATION seconds, stop and create a DB backup
    4. Cooldown for 2-3 minutes, then repeat
    """
    global write_queue
    init_db()

    # Pre-load existing scheme codes to avoid duplicate scraping
    conn = sqlite3.connect(DB_NAME, timeout=30)
    try:
        existing_schemes = [row[0] for row in conn.execute("SELECT scheme_code FROM schemes").fetchall()]
    except:
        existing_schemes = []
    conn.close()

    # Setup multiprocessing shared state
    manager = multiprocessing.Manager()
    write_queue = manager.Queue()
    shared_scheme_cache = manager.list(existing_schemes)

    # Start the DB writer thread
    writer_thread = threading.Thread(target=db_writer_listener, daemon=True)
    writer_thread.start()

    last_rest_time = time.time()
    try:
        while True:

            # --- Cooldown check ---
            elapsed_time = time.time() - last_rest_time
            if elapsed_time > WORK_DURATION:

                # Create a backup during the break
                print(f"🗄️ Backlog cleared! Creating a backup for {DB_NAME}")
                main = sqlite3.connect(DB_NAME)
                backup = sqlite3.connect("jjm_parallel_data_backup.db")
                sqlite3.connect(DB_NAME).backup(backup)
                backup.close()
                main.close()

                COOLDOWN_PERIOD = random.uniform(120, 180)
                print(f"⏰ Backup created! Resting for {int(COOLDOWN_PERIOD)}s...")
                time.sleep(COOLDOWN_PERIOD)

                last_rest_time = time.time()

            # --- Grab next batch of blocks ---
            conn = sqlite3.connect(DB_NAME, timeout=30)
            tasks = conn.execute(f"SELECT DISTINCT state_val, dist_val, block_val FROM queue WHERE status = 'PENDING' LIMIT {BLOCK_BATCH_SIZE}").fetchall()
            conn.close()

            if not tasks:
                print("💤 No pending tasks, closing in 30 seconds...")
                time.sleep(30)
                continue

            print(f"⚡️ Batch Start: Dispatching {len(tasks)} blocks...")

            # --- Dispatch to process pool ---
            with ProcessPool(max_workers=NUM_SCRAPER_PROCESSES, initializer=init_worker_process, initargs=(write_queue, shared_scheme_cache)) as pool:

                # Each task gets a 10-minute timeout. Slow blocks will be retried in later batches.
                futures = [pool.schedule(run_scraper_task, args=(task,), timeout=600) for task in tasks]

                for future in as_completed(futures):
                    # Check if we've exceeded the work duration mid-batch
                    if time.time() - last_rest_time > WORK_DURATION:
                        print(f"⚠️ Timer expired mid-batch ({int(time.time() - last_rest_time)}s elapsed)! Stopping batch...")
                        future.cancel()
                        pool.stop()
                        pool.join()
                        print(f"⏳ Waiting for DB Writer to clear backlog ({write_queue.qsize():,} items approx). This will take a while...")
                        while not write_queue.empty():
                            time.sleep(1)
                        break
                    try:
                        block_val = future.result()
                        print(f"🧱 Block {block_val} finished | ⏰ {int(time.time() - last_rest_time)}s elapsed")
                    except TimeoutError:
                        print(f"💀 A block was killed. Progress for finished panchayats was saved | ⏰ {int(time.time() - last_rest_time)}s elapsed")
                    except Exception as e:
                        print(f"❌ Task Error: {e}")

    except KeyboardInterrupt:
        print("\n🛑 Shutdown signal received! Cleaning up...")
    finally:
        print("📨 Sending stop signal to DB writer...")
        write_queue.put(("STOP", None))
        writer_thread.join(timeout=5)
        print("✅ Exit complete.")

if __name__ == "__main__":
    main()
