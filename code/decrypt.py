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

print(encrypt("Chhattisgarh"))