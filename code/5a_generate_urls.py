import requests
import json
from tqdm import tqdm
from base64 import b64encode
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from bs4 import BeautifulSoup
import pandas as pd
import os
from concurrent.futures import ThreadPoolExecutor
import time
import random
from io import StringIO
import sys
import contextlib

base_data_folder = "../data/RawDataCSVs"

@contextlib.contextmanager
def suppress_stdout():
    with open(os.devnull, "w") as devnull:
        old_stdout = sys.stdout
        sys.stdout = devnull
        try:
            yield
        finally:
            sys.stdout = old_stdout
# Key and IV used by the site (as UTF-8 encoded bytes)
key = b"8080808080808080"
iv = b"8080808080808080"

url = "https://ejalshakti.gov.in/WQMIS/Report/labtestingsamplelist"

def encrypt(text):
    # Pad the plaintext and encrypt
    cipher = AES.new(key, AES.MODE_CBC, iv) 
    ciphertext_bytes = cipher.encrypt(pad(str(text).encode(), AES.block_size))
    # Encode to Base64 (to match CryptoJS .toString())
    ciphertext_b64 = b64encode(ciphertext_bytes).decode()
    #print(ciphertext_b64)
    return ciphertext_b64

# Loop through all state_* folders
for folder in os.listdir(base_data_folder):
    if folder.startswith("state_") and os.path.isdir(folder):
        urls = []
        outpaths = []
        response_path = os.path.join(folder, "response.json")
        state_id = folder.split('_')[-1]
        blocks_path = os.path.join(folder, "blocks.json")
        gps_path = os.path.join(folder, "gps.json")
        villages_path = os.path.join(folder, "villages.json")

        if os.path.exists(villages_path):
            with open(villages_path, 'r', encoding='utf-8') as file:
                try:
                    data = json.load(file)
                except json.JSONDecodeError:
                    print(f"Skipping {villages_path} due to invalid JSON.")
                    continue


                # Loop over the list and extract the JJM_DistrictId
                for nested_list in data:
                    for item in nested_list:
                        # Extract the relevant information
                        base_fy = '2024-2025'
                        base_stid = str(item.get("state_id"))
                        base_dtid = str(item.get("District_id"))
                        base_blid = str(item.get("Block_id"))
                        base_gpid = str(item.get("Grampanchayat_id"))
                        base_villid = str(item.get("JJM_VillageId"))
                        
                        params = {
                            "currentstatus":3,
                            "fy":encrypt(base_fy),
                            "stid":encrypt(base_stid),
                            "dtid":encrypt(base_dtid),
                            "blid":encrypt(base_blid),
                            "gpid":encrypt(base_gpid),
                            "villid":encrypt(base_villid)
                        }
                        
                        url_req = url + "?" + "currentstatus=3&" + "fy=" + params["fy"] + "&stid=" + params["stid"] + "&dtid=" + params["dtid"] + "&blid=" + params["blid"] + "&gpid=" + params["gpid"] + "&villid=" + params["villid"]
                        urls.append(url_req)
                        outpaths.append(os.path.join(folder,base_villid+".csv"))

        tasklist_path = os.path.join(os.path.join(base_data_folder,folder), 'tasks.csv')

        with open(tasklist_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['url', 'output_file'])  # header
            writer.writerows(list(zip(urls,outpaths)))