import requests
from concurrent.futures import ThreadPoolExecutor
import os
import sys
import time
from time import sleep
import nguyenthanhngoc
from nguyenthanhngoc import *
import random

def clear():
    os.system("cls" if os.name == "nt" else "clear")
clear()

def check_internet_connection():
     try:
        respone = requests.get("https://www.google.com", timeout=5)
        return True
     except requests.ConnectionError:
        return False
if not check_internet_connection():
    print("\n\033[1;31minternet lord cak ak?:)")
    sys.exit(1)
    
def loading(seconds):
    print("\033[1;35mLoading", end="", flush=True)
    for _ in range(seconds):
        time.sleep(1)
        print("!", end="", flush=True)
    print("\n\033[1;35mDone !")
    sleep(2)
loading(5)

def clear():
    os.system("cls" if os.name == "nt" else "clear")
clear()

Write.Print(f"""

████████╗██╗  ██╗ █████╗ ███╗   ██╗██╗  ██╗    ██╗   ██╗██╗   ██╗
╚══██╔══╝██║  ██║██╔══██╗████╗  ██║██║  ██║    ██║   ██║╚██╗ ██╔╝
   ██║   ███████║███████║██╔██╗ ██║███████║    ██║   ██║ ╚████╔╝ 
   ██║   ██╔══██║██╔══██║██║╚██╗██║██╔══██║    ╚██╗ ██╔╝  ╚██╔╝  
   ██║   ██║  ██║██║  ██║██║ ╚████║██║  ██║     ╚████╔╝    ██║   
   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝      ╚═══╝     ╚═╝   
   
    ┌───────────────────└┐ 𝐖𝐄𝐋𝐂𝐎𝐌𝐄 └┐──────────────────┐
    │
    │   𝐓𝐎𝐎𝐋 𝐍𝐀𝐌𝐄: Check Proxy V7
    │   𝐕𝐄𝐑𝐒𝐈𝐎𝐍: 7.0
    │   𝐔𝐏𝐃𝐀𝐓𝐄𝐃 𝐎𝐍 𝐃𝐀𝐓𝐄: 24/12/2024
    │   𝐓𝐎𝐎𝐋 𝐀𝐃𝐌𝐈𝐍: Alex 
    │
    └──────────────────────────────────────────────────┘
\n""", Colors.rainbow, interval=0.005)
print("\033[1;32m++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++")
proxy_list = input("\033[1;32m File Proxy Cần Lọc: \033[1;33m")
with open(proxy_list, 'r') as file:
    proxy_list = file.read().split("\n")
    proxy_count = len(proxy_list)
luu = input("\033[1;31m File Lưu Proxy Live: \033[1;36m")
print(f" \033[1;31m Tất Cả Gồm: \033[1;37m {proxy_count} \033[1;31mProxy Trong File")
print("\033[1;34m Chờ Chút\033[1;37m Tool\033[1;31m Bắt Đầu\033[1;37m Lọc \033[1;31mProxy")
print("\033[1;32m++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++")
sleep(3)
list = []
for p in proxy_list:
    prx = p.strip("\n")
    list.append(prx)
    
def check_proxy(proxy):
    proxies = {
        'http': f'http://{proxy}',
        'https': f'http://{proxy}'
    }
    
    try:
        response = requests.get('http://httpbin.org/ip', proxies=proxies, timeout=5)
        if response.status_code == 200 or response.status_code == 202 or response.status_code == 504 or response.status_code == 503 or response.status_code == 502 or response.status_code == 500:
            detect_location(proxy)
            return True
    except requests.exceptions.RequestException:
        pass

    print(f" \033[1;37m[\033[1;31m★\033[1;37m] \033[1;37m{proxy} \033[1;31m× \033[1;37mNhu Lon \033[1;31m× \033[1;31mDie")
    return False


def detect_location(proxy):
    ip_address = proxy.split(':')[0]
    url = f"http://ip-api.com/json/{ip_address}"
    response = requests.get(url)

    if response.status_code == 200:
        data = response.json()
        if data["status"] == "success":
            print(f" \033[1;37m[\033[1;31m★\033[1;37m] \033[1;37m{proxy} \033[1;31m√ \033[1;37m{data['country']}/{data['city']} \033[1;31m√ \033[1;32mLive")
            open(luu,'a').write(proxy+'\n')
        else:
            print(" \033[1;37m[\033[1;31m+\033[1;37m] \033[1;31mFailed to detect location for proxy.")


def process_proxy(proxy):
    if check_proxy(proxy):
        pass


num_workers = 200

with ThreadPoolExecutor(max_workers=num_workers) as executor:
    executor.map(process_proxy, proxy_list)

print(
    f" \033[1;31mĐã lọc xong Proxy - đã lưu vào \033[1;37m{luu} \033[1;31mCó tất cả \033[1;37m%s \033[1;31mProxy Live"
    % (len(open(f"{luu}").readlines()))
)
print("\033[1;31m Cảm ơn đã sử dụng tool")
logout = input(" Nhấn enter để thoát tool")
exit()