import requests
from bs4 import BeautifulSoup
import re
import sqlite3
import time
import threading
import multiprocessing
from concurrent.futures import TimeoutError
from pebble import ProcessPool

# --- CONFIG ---
URL = "https://ejalshakti.gov.in/JJM/JJMReports/profiles/rpt_VillageProfile.aspx"
DOMAIN = "https://ejalshakti.gov.in/JJM/"
DB_NAME = "jjm_parallel_data.db"
NUM_SCRAPER_PROCESSES = 10 # Adjust this amount if you want to increase/decrease the workers/scrapers
BATCH_SIZE = 100 # The scrapers work in batch to organize what the workers can work on. Keep this high so that the workers could keep working because sometimes there's a "struggler" worker.

# --- PARSING HELPERS ---
# These are just functions that we use to grab the texts and format them
def get_text_safe(soup, elem_id):
    el = soup.find(id=elem_id)
    return el.get_text(strip=True) if el else "N/A"

def safe_int(value):
    try: return int(str(value).replace(',', '').strip())
    except: return 0

def safe_float(value):
    try: return float(str(value).replace(',', '').strip())
    except: return 0.0

# --- SCRAPER LOGIC ---
# This is the brain of the scraper workers.
class ScraperWorker:
    def __init__(self, shared_scheme_cache):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded"})
        self.shared_scheme_cache = shared_scheme_cache

    # This is a short dictionary maker to create what's necessary to make a valid POST request in each dropdown.
    def get_asp_vars(self, soup):
        return {f: soup.find("input", {"name": f})["value"] for f in ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"]}

    # Sends a POST and returns the soup + the new VIEWSTATE
    def post_step(self, target, payload, vs):
        
        for attempt in range(3):
            try:
                data = {**vs, "__EVENTTARGET": target.replace("_", "$"), "__EVENTARGUMENT": "", **payload}
                res = self.session.post(URL, data=data, timeout=30)
                if res.status_code == 200 and "__VIEWSTATE" in res.text:
                    soup = BeautifulSoup(res.text, "lxml")
                    # Return both the soup and the NEW vars for the next step
                    return soup, self.get_asp_vars(soup)
            except:
                pass
            time.sleep(3 * (attempt + 1))
        return None, None
    
    # This is how we visit and scrape scheme links
    def scrape_scheme_details(self, clean_url, scheme_code):
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
                print(f"⚠️ Error parsing details for Scheme {scheme_code}: {e}") # A skipped scheme will be counted in the expected_scheme_count and we can always scrape it back later! This is very rare though.

    # This is where we process the village and their schemes
    def parse_village(self, html, v_name, v_val):
        soup = BeautifulSoup(html, 'lxml') 
        raw_vil_str = get_text_safe(soup, "CPHPage_lblVillage")
        jjm_match = re.search(r'JJM VillageId\s*:\s*(\d+)', raw_vil_str)
        jjm_id = jjm_match.group(1) if jjm_match else f"V_{v_val}"
        
        # Scheme Counter (very important to check scheme integrity!)
        scheme_links = soup.find_all("a", id=re.compile(r"CPHPage_rptscheme_hyscheme_\d+"))
        expected_schemes = len(scheme_links)

        # 1. Village main data
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

        tables = ["water_sources", "water_quality", "schools_anganwadis", "public_institutions", "village_committees", "village_scheme_mapping"]
        for table in tables:
            write_queue.put(("INSERT_GENERIC", (table, f"DELETE FROM {table} WHERE village_id=?", (jjm_id,))))

        # 2. Water Sources
        for row in soup.find_all("span", id=re.compile(r"CPHPage_rptSource_lblHabitationName_\d+")):
            idx = row['id'].split('_')[-1]
            sql = 'INSERT INTO water_sources (village_id, habitation, source_location, source_type, scheme_name) VALUES (?,?,?,?,?)'
            params = (jjm_id, row.text, get_text_safe(soup, f"CPHPage_rptSource_hyLocation_{idx}"),
                      get_text_safe(soup, f"CPHPage_rptSource_lblSourceTypeCategory_{idx}"), get_text_safe(soup, f"CPHPage_rptSource_hyscheme_{idx}"))
            write_queue.put(("INSERT_GENERIC", ("water_sources", sql, params)))

        # 3. Water Quality
        for row in soup.find_all("span", id=re.compile(r"CPHPage_rptWQMIS_lblLab_FTK_testing_\d+")):
            idx = row['id'].split('_')[-1]
            sql = 'INSERT INTO water_quality (village_id, test_type, status, test_date, sample_location, remedial_action) VALUES (?,?,?,?,?,?)'
            params = (jjm_id, row.text, get_text_safe(soup, f"CPHPage_rptWQMIS_lblStatus_{idx}"), get_text_safe(soup, f"CPHPage_rptWQMIS_item_last_testing_date_{idx}"), 
                      get_text_safe(soup, f"CPHPage_rptWQMIS_item_Sample_location_{idx}"), get_text_safe(soup, f"CPHPage_rptWQMIS_item_remedial_action_status_{idx}"))
            write_queue.put(("INSERT_GENERIC", ("water_quality", sql, params)))

        # 4. Schools/Anganwadis
        for prefix, label in [("CPHPage_rptSchool_", "School"), ("CPHPage_rptSchool_anganwadi_", "Anganwadi")]:
            for row in soup.find_all("span", id=re.compile(rf"{prefix}lblVoucherNumber_\d+")):
                idx = row['id'].split('_')[-1]
                sql = 'INSERT INTO schools_anganwadis (village_id, type, habitation, name, category, classification, has_tap_connection) VALUES (?,?,?,?,?,?,?)'
                params = (jjm_id, label, get_text_safe(soup, f"{prefix}lblVoucherNumber_{idx}"), get_text_safe(soup, f"{prefix}lblVoucherDate_{idx}"),
                          get_text_safe(soup, f"{prefix}lblActivityName_{idx}"), get_text_safe(soup, f"{prefix}Label1_{idx}"), get_text_safe(soup, f"{prefix}Label2_{idx}"))
                write_queue.put(("INSERT_GENERIC", ("schools_anganwadis", sql, params)))

        # 5. Public Institutions
        for row in soup.find_all("span", id=re.compile(r"CPHPage_rpt_otherpublicinstitution_lblVoucherNumber_\d+")):
            idx = row['id'].split('_')[-1]
            sql = 'INSERT INTO public_institutions (village_id, habitation, name, category, has_tap_connection) VALUES (?,?,?,?,?)'
            params = (jjm_id, get_text_safe(soup, f"CPHPage_rpt_otherpublicinstitution_lblVoucherNumber_{idx}"), get_text_safe(soup, f"CPHPage_rpt_otherpublicinstitution_lblVoucherDate_{idx}"),
                      get_text_safe(soup, f"CPHPage_rpt_otherpublicinstitution_lblActivityName_{idx}"), get_text_safe(soup, f"CPHPage_rpt_otherpublicinstitution_Label2_{idx}"))
            write_queue.put(("INSERT_GENERIC", ("public_institutions", sql, params)))

        # 6. Committees
        for row in soup.find_all("span", id=re.compile(r"CPHPage_rptVillageLevel_lblHabitationName_\d+")):
            idx = row['id'].split('_')[-1]
            sql = 'INSERT INTO village_committees (village_id, level, name, gender, designation) VALUES (?,?,?,?,?)'
            params = (jjm_id, get_text_safe(soup, f"CPHPage_rptVillageLevel_Label10_{idx}"), row.text, get_text_safe(soup, f"CPHPage_rptVillageLevel_Label9_{idx}"), get_text_safe(soup, f"CPHPage_rptVillageLevel_Label1_{idx}"))
            write_queue.put(("INSERT_GENERIC", ("village_committees", sql, params)))

        # 7. Schemes.

        for link in scheme_links:
            href = link.get('href')
            if not href: continue

            # Extract the scheme_code which will be checked for duplicates
            try:
                parent_row = link.find_parent('tr')
                scheme_code = parent_row.find_all('td')[1].get_text(strip=True)
            except: continue

            # Record the scheme to village mapping
            write_queue.put(("INSERT_GENERIC", (
                "village_scheme_mapping", 
                "INSERT OR IGNORE INTO village_scheme_mapping (village_id, scheme_code) VALUES (?,?)", 
                (jjm_id, scheme_code)
            )))

            if scheme_code not in self.shared_scheme_cache:
                clean_url = DOMAIN + href.replace("../../", "")
                self.scrape_scheme_details(clean_url, scheme_code)
                self.shared_scheme_cache.append(scheme_code)

# This is how we navigate the website. We navigate per block to save bandwidth while still keeping it pretty granular.
    def process_block(self, block_task):
        state_val, dist_val, block_val = block_task
        
        # 1. Get the 'Checklist' for this block from our DB
        conn = sqlite3.connect(DB_NAME)
        panchayats = conn.execute(
            "SELECT pan_val, pan_name FROM queue WHERE block_val=? AND status='PENDING'", 
            (block_val,)
        ).fetchall()
        conn.close()

        if not panchayats: return

        try:
            # --- START THE CHAIN ---
            res = self.session.get(URL, timeout=30)
            vs_current = self.get_asp_vars(BeautifulSoup(res.text, "lxml"))
            
            # Step A: Select State
            s_dist, vs_current = self.post_step("ctl00$CPHPage$ddState", {"ctl00$CPHPage$ddState": state_val}, vs_current)
            if not vs_current: return
            
            # Step B: Select District
            s_block, vs_current = self.post_step("ctl00$CPHPage$ddDistrict", {"ctl00$CPHPage$ddState": state_val, "ctl00$CPHPage$ddDistrict": dist_val}, vs_current)
            if not vs_current: return

            # Step C: Select Block
            s_pan_list, vs_current = self.post_step("ctl00$CPHPage$ddblock", {
                "ctl00$CPHPage$ddState": state_val, "ctl00$CPHPage$ddDistrict": dist_val, "ctl00$CPHPage$ddblock": block_val
            }, vs_current)
            if not vs_current: return

            # --- LOOP THROUGH THE CHAIN ---
            for pan_val, pan_name in panchayats:
                # 1. Select Panchayat -> Updates vs_current for the next iteration
                s_pan, vs_current = self.post_step("ctl00$CPHPage$ddblock", {
                    "ctl00$CPHPage$ddState": state_val, "ctl00$CPHPage$ddDistrict": dist_val, 
                    "ctl00$CPHPage$ddblock": block_val, "ctl00$CPHPage$ddPanchayat": pan_val
                }, vs_current)

                if not vs_current:
                    print(f"❌ Chain broken at {pan_name}. Ending block task.")
                    return

                # 2. Get Village Dropdown -> Updates vs_current again
                s_vil, vs_current = self.post_step("ctl00$CPHPage$ddPanchayat", {
                    "ctl00$CPHPage$ddState": state_val, "ctl00$CPHPage$ddDistrict": dist_val, 
                    "ctl00$CPHPage$ddblock": block_val, "ctl00$CPHPage$ddPanchayat": pan_val
                }, vs_current)

                vil_select = s_vil.find("select", id=re.compile("ddVillage$")) if s_vil else None
                if not vil_select:
                    print(f"⚠️ Page sync error for {pan_name}. Moving to next.")
                    continue

                # 3. Scrape Villages
                villages = [(o.text.strip(), o["value"]) for o in vil_select.find_all("option") if o["value"] not in ["0", "-1"]]
                
                for v_name, v_val in villages:
                    p_final = {
                        "ctl00$CPHPage$ddState": state_val, "ctl00$CPHPage$ddDistrict": dist_val, 
                        "ctl00$CPHPage$ddblock": block_val, "ctl00$CPHPage$ddPanchayat": pan_val, 
                        "ctl00$CPHPage$ddVillage": v_val, "ctl00$CPHPage$btnShow": "Show"
                    }
                    # Final report doesn't usually need to update vs_current because it doesn't change dropdowns
                    report_res = self.session.post(URL, data={**vs_current, "__EVENTTARGET": "", **p_final}, timeout=30)
                    self.parse_village(report_res.text, v_name, v_val)
                
                # Success! Checkpoint this Panchayat
                write_queue.put(("MARK_DONE", (block_val, pan_val, pan_name)))
            
        except Exception as e:
            print(f"❌ Error on Block {block_val}: {e}")

# --- DB WRITER ---
# This function runs as a background "Listener" thread in Python. 
# Its job is to protect the database from being overwhelmed by multiple workers trying to write at the exact same time.
def db_writer_listener():
    # Establish a single, stable connection to the database file.
    conn = sqlite3.connect(DB_NAME, timeout=30)
    # Enable WAL mode for better performance during high-speed parallel scraping.
    conn.execute("PRAGMA journal_mode=WAL") 
    c = conn.cursor()
    
    # Figure out the starting progress for the logs.
    try:
        c.execute("SELECT COUNT(*) FROM queue")
        total_tasks = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM queue WHERE status='DONE'")
        tasks_done = c.fetchone()[0]
        print(f"📊 Tracking Progress: {tasks_done:,}/{total_tasks:,} panchayats already done.")
    except:
        total_tasks, tasks_done = 1, 0

    print("💾 DB Writer started...")
    
    # This loop keeps Python 'waiting' for data from the workers.
    while True:
        try:
            # Python 'blocks' (pauses) here until a worker puts a task in the queue.
            # We use 'Tuple Unpacking' to separate the Task Name from the Data.
            task_type, data = write_queue.get()

            # TASK TYPE: STOP
            # This is only used when the scraping is all finished.
            if task_type == "STOP":
                conn.commit(); conn.close(); break
            
            # TASK TYPE: INSERT_VILLAGE
            # Handles the primary village profile data.
            # Uses 'OR REPLACE' so Python doesn't crash on duplicate Village IDs.
            elif task_type == "INSERT_VILLAGE":
                c.execute('INSERT OR REPLACE INTO villages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', data)

            # TASK TYPE: INSERT_GENERIC
            # A 'Catch-All' task that handles sub-tables like Schemes, Water Sources, etc. The SQL code is already written previously
            elif task_type == "INSERT_GENERIC":
                table_name, sql, params = data
                c.execute(sql, params)
            
            # TASK TYPE: MARK_DONE
            # This is the bookkeper task that updates our panchayat
            elif task_type == "MARK_DONE":
                block_val, pan_val, pan_name = data
                # Only NOW do we change status to DONE. This is important for safe bookkeeping
                c.execute("UPDATE queue SET status = 'DONE' WHERE block_val = ? AND pan_val = ?", (block_val, pan_val))
                conn.commit()
                tasks_done += 1
                percent = (tasks_done / total_tasks) * 100 if total_tasks > 0 else 0
                print(f"✅ [{tasks_done:,} done out of {total_tasks:,} panchayats | {percent:.2f}%] Finished: {pan_name}")

        except Exception as e:
            print(f"❌ DB Writer Error: {e}")

# --- INIT DB ---
# Create the tables if not already
def init_db():
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

# --- MULTIPROCESSING HELPERS ---

# Function to start a worker
def init_worker_process(q, cache):
    global write_queue
    write_queue = q
    global shared_scheme_cache
    shared_scheme_cache = cache

# A wrapper function to run the worker class inside a process
def run_scraper_task(task):
    worker = ScraperWorker(shared_scheme_cache)
    return worker.process_block(task)

# --- Main function ---
def main():
    global write_queue
    init_db()
    conn = sqlite3.connect(DB_NAME, timeout=30)
    try:
        existing_schemes = [row[0] for row in conn.execute("SELECT scheme_code FROM schemes").fetchall()]
    except:
        existing_schemes = []
    conn.close()
    
    # 1. Setup Multiprocessing Manager
    manager = multiprocessing.Manager()
    write_queue = manager.Queue()
    shared_scheme_cache = manager.list(existing_schemes) # Shared list

    # 2. Start DB Writer (Runs in a Thread in the Main Process)
    writer_thread = threading.Thread(target=db_writer_listener, daemon=True)
    writer_thread.start()

    while True:
        # 3. Grab Batch
        conn = sqlite3.connect(DB_NAME, timeout=30)
        tasks = conn.execute(f"SELECT DISTINCT state_val, dist_val, block_val FROM queue WHERE status = 'PENDING' LIMIT {BATCH_SIZE}").fetchall()
        conn.close()

        if not tasks:
            print("💤 No pending tasks, closing in 30 seconds...")
            time.sleep(30); continue

        print(f"⚡️ Batch Start: Dispatching {len(tasks)} tasks...")

        # 4. We pass the shared queue to every new process via 'initializer'
        with ProcessPool(max_workers=NUM_SCRAPER_PROCESSES, initializer=init_worker_process, initargs=(write_queue, shared_scheme_cache)) as pool:
            
            # EDIT THE MAXIMUM TASK SPEED HERE. We set it to 10 minutes because some blocks are quite large but we don't wanna take too long. We can always scrape the left out blocks later because we record the progress at the panchayat level
            future = pool.map(run_scraper_task, tasks, timeout=600)
            
            iterator = future.result()
            
            while True:
                try:
                    next(iterator)
                except StopIteration:
                    break # Batch finished
                except TimeoutError:
                    print(f"💀 Block {tasks[0][2]} (State {tasks[0][0]}) was killed. Progress for finished panchayats was saved.")
                    # This is a very slow block (a struggler) that takes more than 10 minutes. We skip it for the batch as it may be better to scrape it in later batches.
                except Exception as e:
                    print(f"❌ Task Error: {e}")

    # Cleanup when it's all finished
    write_queue.put(("STOP", None))
    writer_thread.join()
    print("✅ All Done.")
    
if __name__ == "__main__":
    main()