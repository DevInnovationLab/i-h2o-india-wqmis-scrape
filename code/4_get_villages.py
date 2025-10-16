import requests
import json
import os

base_data_folder = "../data/RawDataCSVs"

# Function to make a POST request and append the JSON response to a file
def make_request_and_append(url, payload, stateid, districtid, blockid, gpid,output_file):
    try:
        # Send POST request
        response = requests.post(url, data=payload)
        
        # If the request is successful, process the JSON response
        if response.status_code == 200:
            response_data = response.json()

            # Add stateid, districtid, and blockid to the response data
            for item in response_data:
                #print(item)
                # Add the missing info to each item in the response
                item['District_id'] = districtid
                item['Block_id'] = blockid
                item['Grampanchayat_id'] = gpid
                item['state_id'] = stateid

            # Open the file in append mode
            with open(output_file, 'a') as file:
                # If the file is empty, start the JSON array
                if file.tell() == 0:
                    file.write("[")  # Begin the JSON array

                else:
                    # Add a comma to separate the JSON objects
                    file.write(",\n")
                
                # Write the updated JSON response to the file
                json.dump(response_data, file)
                
        else:
            print(f"Failed to retrieve data. Status code: {response.status_code}")
    
    except Exception as e:
        print(f"Error making request: {e}")

url="https://ejalshakti.gov.in/WQMIS/Common/GetVillage_Bind/"

# Loop through all state_* folders
for folder in os.listdir(base_data_folder):
    if folder.startswith("state_") and os.path.isdir(folder):
        response_path = os.path.join(folder, "response.json")
        state_id = folder.split('_')[-1]
        blocks_path = os.path.join(folder, "blocks.json")
        gps_path = os.path.join(folder, "gps.json")
        villages_path = os.path.join(folder, "villages.json")

        if os.path.exists(gps_path):
            with open(gps_path, 'r', encoding='utf-8') as file:
                try:
                    data = json.load(file)
                except json.JSONDecodeError:
                    print(f"Skipping {gps_path} due to invalid JSON.")
                    continue


                # Loop over the list and extract the JJM_DistrictId
                for nested_list in data:
                    for item in nested_list:
                        # Extract the relevant information
                        stateid = item.get("state_id")
                        districtid = item.get("District_id")
                        blockid = item.get("Block_id")
                        gpid = item.get("JJM_PanchayatId")
                        
                        # Construct the payload dictionary
                        payload = {
                            "District_id": districtid,
                            "Block_id": blockid,
                            "Grampanchayat_id":gpid
                        }
                        
                        make_request_and_append(url, payload, stateid, districtid, blockid, gpid,villages_path)

                # After appending all responses, close the JSON array
                with open(villages_path, 'a') as file:
                    file.write("\n]")  # Close the JSON array
