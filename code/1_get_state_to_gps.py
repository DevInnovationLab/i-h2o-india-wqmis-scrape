import requests, csv, time
from bs4 import BeautifulSoup

BASE = "https://ejalshakti.gov.in"
PAGE = f"{BASE}/WQMIS/Report/Report_L"
DISTRICT_URL = f"{BASE}/WQMIS/Common/District_Bind_without_session"
BLOCK_URL = f"{BASE}/WQMIS/Common/Block_Bind_without_session"
GP_URL = f"{BASE}/WQMIS/Common/GetGramPanchayat_Bind_without_session"

# Create a reusable HTTP session so headers/cookies persist between requests
session = requests.Session()  # create a persistent HTTP session

# Pretend to be a browser by setting realistic headers
session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Referer": PAGE,
})

# Fetch the main page to extract the CSRF token and the state dropdown HTML
response = session.get(PAGE, timeout=30)
response.raise_for_status()

# Parse the HTML returned by the GET request
soup = BeautifulSoup(response.text, "html.parser")

# Find the anti-CSRF token hidden in the page
tok_el = soup.find("input", {"name": "__RequestVerificationToken"})
token = tok_el["value"] if tok_el else ""

# Locate the <select> element that contains all state <option> entries
state_select = (soup.select_one('select#StateId') or
                soup.select_one('select[name="StateId"]'))

# Extract each valid state name and state_id from the dropdown
states = []
for option in state_select.find_all("option"):
    # Clean the visible label of the <option>
    state_name = option.text.strip()
    # Extract the value attribute of the <option>
    state_id = option.get("value", "").strip()
    # Skip blank entries and the placeholder "--Select--"
    if state_name not in ("", "--Select--") and state_id != "":
        states.append((state_name, state_id))

# Open the CSV file where all scraped state/district data will be written
with open("data.csv", "w", newline="", encoding="utf-8") as file:
    write = csv.writer(file)
    write.writerow([
        "state_name", "state_id",
        "district_id", "district_name",
        "block_id", "block_name",
        "gp_id", "gp_name",
    ])
    for state_name, state_id in states:
        # Prepare POST data to request districts for the current state
        district_payload = {"state_id": state_id}
        if token != "":  # include CSRF token
            district_payload["__RequestVerificationToken"] = token
        # Perform the POST request to fetch district JSON for this state
        response = session.post(
            DISTRICT_URL,
            data=district_payload,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Origin": BASE, "Referer": PAGE,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            timeout=30,
        )
        response.raise_for_status()
        districts = response.json()

        # For each district, fetch blocks, then for each block fetch gram panchayats
        for district in districts:
            if not district.get("DistrictName") or district["DistrictName"] == "--Select--":
                continue
            district_name = district["DistrictName"]
            district_id = district["JJM_DistrictId"]

            # Prepare POST data to request blocks for this district
            block_payload = {
                "state_id": state_id,
                "District_id": district_id,
            }
            if token != "":
                block_payload["__RequestVerificationToken"] = token

            block_response = session.post(
                BLOCK_URL,
                data=block_payload,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Origin": BASE, "Referer": PAGE,
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
                timeout=30,
            )
            block_response.raise_for_status()
            blocks = block_response.json()

            for block in blocks:
                # Adjust keys if the JSON uses different names
                if not block.get("BlockName") or block["BlockName"] == "--Select--":
                    continue
                block_name = block["BlockName"]
                # Prefer an explicit ID field if present; otherwise fall back to "Block_id"
                block_id = block.get("JJM_BlockId")
                if not block_id:
                    continue

                # Prepare POST data to request gram panchayats for this block
                gp_payload = {
                    "state_id": state_id,
                    "District_id": district_id,
                    "Block_id": block_id,
                }
                if token != "":
                    gp_payload["__RequestVerificationToken"] = token

                gp_response = session.post(
                    GP_URL,
                    data=gp_payload,
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Origin": BASE, "Referer": PAGE,
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    },
                    timeout=30,
                )
                gp_response.raise_for_status()
                gps = gp_response.json()

                for gp in gps:
                    # Adjust keys if the JSON uses different names
                    if not gp.get("PanchayatName") or gp["PanchayatName"] == "--Select--":
                        continue
                    gp_name = gp["PanchayatName"]
                    gp_id = gp.get("JJM_PanchayatId")
                    if not gp_id:
                        continue

                    write.writerow([
                        state_name, state_id,
                        district_id, district_name,
                        block_id, block_name,
                        gp_id, gp_name,
                    ])

        # Pause briefly to avoid hitting the server too rapidly per state
        time.sleep(0.5)