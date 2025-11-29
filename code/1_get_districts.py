import requests
import os
import json
from base64 import b64encode
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

base_data_folder = "../../i-h2o-india-wqmis-data/data/RawDataCSVs"

url = "https://ejalshakti.gov.in/WQMIS/Common/District_Bind_without_session"

key = b"8080808080808080"
iv = b"8080808080808080"

def encrypt(text):
    # Pad the plaintext and encrypt
    cipher = AES.new(key, AES.MODE_CBC, iv) 
    ciphertext_bytes = cipher.encrypt(pad(str(text).encode(), AES.block_size))
    # Encode to Base64 (to match CryptoJS .toString())
    ciphertext_b64 = b64encode(ciphertext_bytes).decode()
    #print(ciphertext_b64)
    return ciphertext_b64


# Loop through state IDs 1 to 32
#for state_id in range(1, 33):
for state_id in range(32, 33):
    s = requests.Session()
    s.get('https://ejalshakti.gov.in/WQMIS/Report/Report_L')
    s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})

    data = {"state_id": encrypt(str(state_id))}
    response = s.post(url, data=data)

    if response.status_code == 200:
        #print(f"Request for state ID {state_id} was successful.")

        # Create a folder named after the state ID
        folder_name = f"state_{state_id}"
        os.makedirs(os.path.join(base_data_folder,folder_name), exist_ok=True)

        # Save the response JSON to a file
        file_path = os.path.join(os.path.join(base_data_folder,folder_name), "response.json")
        with open(file_path, "w", encoding="utf-8") as f:
            try:
                json.dump(response.json(), f, ensure_ascii=False, indent=4)
            except ValueError:
                print(f"Warning: Non-JSON response for state ID {state_id}")
                f.write(response.text)
    else:
        print(f"Request failed for state ID {state_id} with status code: {response.status_code}")
