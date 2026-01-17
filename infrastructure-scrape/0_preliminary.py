# This is a preliminary script to scrape all the panchayats in the JJM database
import requests
from bs4 import BeautifulSoup
import re
import sqlite3

URL = "https://ejalshakti.gov.in/JJM/JJMReports/profiles/rpt_VillageProfile.aspx"
DB_NAME = "jjm_parallel_data.db"

def fill_queue():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # 1. Setup tables
    c.execute('''CREATE TABLE IF NOT EXISTS queue (
        state_val TEXT, dist_val TEXT, block_val TEXT, pan_val TEXT,
        state_name TEXT, dist_name TEXT, block_name TEXT, pan_name TEXT,
        status TEXT DEFAULT 'PENDING',
        PRIMARY KEY (block_val, pan_val))''')
    c.execute('''CREATE TABLE IF NOT EXISTS state_log (state_val TEXT PRIMARY KEY)''')   
    c.execute('''CREATE TABLE IF NOT EXISTS district_log (dist_val TEXT PRIMARY KEY)''')   
    c.execute('''CREATE TABLE IF NOT EXISTS block_log (block_val TEXT PRIMARY KEY)''')
    conn.commit()

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    
    print("🔍 Starting/Resuming Discovery Phase...")
    initial_soup = BeautifulSoup(session.get(URL).text, 'lxml')

    def get_opts(soup, sid):
        select = soup.find("select", id=re.compile(f"{sid}$"))
        if not select: return []
        return [(o.text.strip(), o["value"]) for o in select.find_all("option") 
                if "Select" not in o.text and o["value"] not in ["0", "-1", ""]]

    def post(target, payload, vs_soup):
        vs = {
            "__VIEWSTATE": vs_soup.find("input", {"name": "__VIEWSTATE"})["value"],
            "__VIEWSTATEGENERATOR": vs_soup.find("input", {"name": "__VIEWSTATEGENERATOR"})["value"],
            "__EVENTVALIDATION": vs_soup.find("input", {"name": "__EVENTVALIDATION"})["value"],
        }
        data = {**vs, "__EVENTTARGET": target.replace("_", "$"), "__EVENTARGUMENT": "", **payload}
        return BeautifulSoup(session.post(URL, data=data).text, 'lxml')

    # 1. States
    states = get_opts(initial_soup, "ddState")
    for s_name, s_val in states:

        # --- STATE RESUME LOGIC ---
        c.execute("SELECT 1 FROM state_log WHERE state_val = ?", (s_val,))
        if c.fetchone():
            # Jumps over the entire district immediately
            continue

        print(f"Mapping State: {s_name}")
        s_soup = post("ctl00$CPHPage$ddState", {"ctl00$CPHPage$ddState": s_val}, initial_soup)
        
        # 2. Districts
        districts = get_opts(s_soup, "ddDistrict")
        for d_name, d_val in districts:
            
            # --- DISTRICT RESUME LOGIC ---
            c.execute("SELECT 1 FROM district_log WHERE dist_val = ?", (d_val,))
            if c.fetchone():
                # Jumps over the entire district immediately
                continue 
            
            print(f"  📂 Processing District: {d_name}")
            d_payload = {"ctl00$CPHPage$ddState": s_val, "ctl00$CPHPage$ddDistrict": d_val}
            d_soup = post("ctl00$CPHPage$ddDistrict", d_payload, s_soup)
            
            # 3. Blocks
            blocks = get_opts(d_soup, "ddblock")
            for b_name, b_val in blocks:

                # --- BLOCK RESUME LOGIC ---
                c.execute("SELECT 1 FROM block_log WHERE block_val = ?", (b_val,))
                if c.fetchone():
                    # Jumps over the entire block immediately
                    continue 
                
                b_payload = {**d_payload, "ctl00$CPHPage$ddblock": b_val}
                b_soup = post("ctl00$CPHPage$ddblock", b_payload, d_soup)
                
                # 4. Panchayats
                panchayats = get_opts(b_soup, "ddPanchayat")
                data_to_insert = []
                for p_name, p_val in panchayats:
                    data_to_insert.append((s_val, d_val, b_val, p_val, s_name, d_name, b_name, p_name))
                
                if data_to_insert:
                    c.executemany("INSERT OR IGNORE INTO queue VALUES (?,?,?,?,?,?,?,?, 'PENDING')", data_to_insert)
                    c.execute("INSERT INTO block_log (block_val) VALUES (?)", (b_val,))
                    conn.commit()
                    print(f"      ✅ Added {len(data_to_insert)} Panchayats from {b_name}")

            # Once all blocks of a district is in the block_log table, the district is fully mapped
            c.execute("INSERT OR IGNORE INTO district_log (dist_val) VALUES (?)", (d_val,))
            conn.commit()
            print(f"  🏁 District {d_name} fully mapped.")
        
        # We will say the state is done if all the districts of the state are in the district_log table
        c.execute("INSERT OR IGNORE INTO state_log (state_val) VALUES (?)", (s_val,))
        conn.commit()
        print(f"🎉 {s_name} fully mapped!")

    print("✅ VALUES DATA COMPLETE")

if __name__ == "__main__":
    fill_queue()