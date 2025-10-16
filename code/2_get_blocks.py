
import requests
import json
import os

base_data_folder = "../data/RawDataCSVs"

# Function to make a POST request and append the JSON response to a file
def make_request_and_append(url, state_id,payload, output_file):
    try:
        # Send POST request
        response = requests.post(url, data=payload)
        
        # If the request is successful, process the JSON response
        if response.status_code == 200:
            response_data = response.json()

            # Open the file in append mode
            with open(output_file, 'a') as file:
                # If the file is empty, start the JSON array
                if file.tell() == 0:
                    file.write("[")  # Begin the JSON array

                else:
                    # Add a comma to separate the JSON objects
                    file.write(",\n")
                
                # Write the JSON response to the file
                json.dump(response_data, file)
                
        else:
            print(f"Failed to retrieve data. Status code: {response.status_code}")
    
    except Exception as e:
        print(f"Error making request: {e}")
# Loop over the list and extract the JJM_DistrictId

url = "https://ejalshakti.gov.in/WQMIS/Common/Block_Bind/"

# Loop through all state_* folders
for folder in os.listdir(base_data_folder):
    if folder.startswith("state_") and os.path.isdir(folder):
        response_path = os.path.join(folder, "response.json")
        state_id = folder.split('_')[-1]
        blocks_output_path = os.path.join(folder, "blocks.json")

        if os.path.exists(response_path):
            with open(response_path, 'r', encoding='utf-8') as file:
                try:
                    data = json.load(file)
                except json.JSONDecodeError:
                    print(f"Skipping {response_path} due to invalid JSON.")
                    continue
                    
                for item in tqdm(data):
                    district_id = item.get("JJM_DistrictId")
                    payload={"state_id":str(state_id),
                    "district_id":district_id}
                    make_request_and_append(url, state_id,payload, blocks_output_path)
                # After appending all responses, close the JSON array
                with open(blocks_output_path, 'a') as file:
                    file.write("\n]")  # Close the JSON array
        else:
            print(f"No response.json found in {folder}")

 

