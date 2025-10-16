import requests
import os
import json

base_data_folder = "../data/RawDataCSVs"

url = "https://ejalshakti.gov.in/WQMIS/Common/District_Bind"

# Loop through state IDs 1 to 32
for state_id in range(1, 33):
    data = {"state_id": str(state_id)}
    response = requests.post(url, data=data)

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
