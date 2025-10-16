import os
import pandas as pd
import requests
from bs4 import BeautifulSoup
import sys

base_data_folder = "../data/RawDataCSVs"
tasklist_path = os.path.join(os.path.join(base_data_folder,'state_'+str(sys.argv[1])), 'tasks.csv')

if not os.path.isfile(outpath):
    response=requests.get(url_req)
    time.sleep(random.uniform(0.5, 2.0))
    # If the request is successful, process the JSON response
    if response.status_code == 200:
        if response.url != 'https://ejalshakti.gov.in/WQMIS/':
            soup = BeautifulSoup(response.text,'html.parser')
            table = soup.find("table", attrs={"class":"table"})
            if table:
                with suppress_stdout():  # suppress unwanted prints from read_html
                    dfs = pd.read_html(StringIO(str(table)))
                for df in dfs:
                    df.to_csv(outpath)

combined_datalist = list(zip(urls, folders, villids))

MAX_THREADS = 56
with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
    list(tqdm(executor.map(parse_page, combined_datalist), total=len(combined_datalist)))