"""
*** FICHIER DE TEST TEMPORAIRE — À SUPPRIMER APRÈS LE TEST ***

Copie de test du script de surveillance Vertretungsplan, adaptée pour
pouvoir être forcée manuellement à n'importe quelle heure (jusqu'à minuit
inclus), sans attendre les créneaux 20h / 7h / 8h-15h30 du script réel.

Différences avec le script de production (alerte_vertretung_5d.py) :
  - CLASSE_CIBLE = "13" au lieu de "5d"
  - Aucune vérification d'heure : le script fait toujours la même chose,
    peu importe quand on le lance
  - Vérifie les DEUX pages (aujourd'hui + demain) à chaque exécution
  - Fichier d'état séparé (etat_TEST_13.json) pour ne pas toucher à
    l'état du script réel (etat_vertretung_5d.json)
  - Sujet d'email préfixé par "[TEST]"

Une fois le test terminé, il suffit de supprimer ce fichier et le fichier
etat_TEST_13.json — le script de production n'est pas affecté.
"""

import os
import re
import json
import smtplib
from email.mime.text import MIMEText
from html import unescape
from pathlib import Path

import requests

# --- Configuration (TEST) ---------------------------------------------------

URL_AUJOURDHUI = "https://vertretungsplan.gymnasium-eversten.de/oeffentlich/subst_001.htm"
URL_DEMAIN = "https://vertretungsplan.gymnasium-eversten.de/oeffentlich/subst_002.htm"

CLASSE_CIBLE = "13"  # <-- classe de test
ART_CIBLE = "entfall"

EMAIL_EXPEDITEUR = os.environ["GMAIL_ADDRESS"]
EMAIL_MOT_DE_PASSE = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_DESTINATAIRE = os.environ["GMAIL_TO"]

FICHIER_ETAT = Path(__file__).parent / "etat_TEST_13.json"


# --- Récupération et analyse de la page (identique au script réel) ---------

def recuperer_page(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def nettoyer_cellule(fragment_html: str) -> str:
    texte = re.sub(r"<[^>]+>", "", fragment_html)
    return unescape(texte).strip()


JOURS_SEMAINE = r"Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag"


def extraire_date_plan(page_html: str) -> str:
    texte = nettoyer_cellule(page_html)
    m = re.search(rf"\d{{1,2}}\.\d{{1,2}}\.\d{{4}}\s+(?:{JOURS_SEMAINE})", texte)
    return m.group(0) if m else "date inconnue"


def extraire_entrees_entfall(page_html: str, date_plan: str, classe: str = CLASSE_CIBLE):
    entrees = []
    lignes = re.findall(r"<tr[^>]*>(.*?)</tr>", page_html, re.S | re.I)

    for ligne in lignes:
        cellules_html = re.findall(r"<td[^>]*>(.*?)</td>", ligne, re.S | re.I)
        cellules = [nettoyer_cellule(c) for c in cellules_html]
        if len(cellules) < 5:
            continue

        stunde, klasse, _raum_old, _raum_new, art = cellules[0:5]
        fach_old = cellules[5] if len(cellules) > 5 else ""
        fach_new = cellules[6] if len(cellules) > 6 else ""
        remarque = cellules[8] if len(cellules) > 8 else ""

        if not klasse or not art:
            continue

        classes_ligne = [c.strip() for c in klasse.split(",")]
        if classe not in classes_ligne:
            continue
        if art.strip().lower() != ART_CIBLE:
            continue

        fach = fach_old or fach_new
        cle = f"{date_plan}|{stunde}|{klasse}|{fach}|{art}"
        entrees.append(
            {
                "cle": cle,
                "stunde": stunde,
                "klasse": klasse,
                "fach": fach,
                "art": art,
                "remarque": remarque,
            }
        )

    return entrees


# --- État persistant (fichier de TEST séparé) -------------------------------

def lire_etat() -> dict:
    if FICHIER_ETAT.exists():
        try:
            return json.loads(FICHIER_ETAT.read_text())
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def ecrire_etat(etat: dict):
    FICHIER_ETAT.write_text(json.dumps(etat, ensure_ascii=False, indent=2))


# --- Email -------------------------------------------------------------

def envoyer_email(sujet: str, corps: str):
    msg = MIMEText(corps)
    msg["Subject"] = f"[TEST] {sujet}"
    msg["From"] = EMAIL_EXPEDITEUR
    msg["To"] = EMAIL_DESTINATAIRE
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as serveur:
        serveur.login(EMAIL_EXPEDITEUR, EMAIL_MOT_DE_PASSE)
        serveur.send_message(msg)


def formater_entrees(entrees, date_plan: str, url: str) -> str:
    lignes = [f"[TEST] Plan du {date_plan} — classe {CLASSE_CIBLE}\n"]
    for e in entrees:
        ligne = f"- Heure {e['stunde']} : {e['fach'] or '(matière ?)'} — Entfall"
        if e["remarque"]:
            ligne += f" ({e['remarque']})"
        lignes.append(ligne)
    lignes.append(f"\nSource : {url}")
    return "\n".join(lignes)


# --- Logique principale (TEST — pas de contrainte d'heure) -----------------

def verifier_page(url: str, libelle: str, etat: dict, deja_signalees: set) -> list:
    """Récupère une page, en extrait les entrées Entfall pour CLASSE_CIBLE,
    renvoie la liste des entrées NOUVELLES (pas encore signalées)."""
    try:
        page_html = recuperer_page(url)
    except Exception as e:
        print(f"[ERREUR] Impossible de récupérer {url} : {e}")
        return []

    date_plan = extraire_date_plan(page_html)
    entrees = extraire_entrees_entfall(page_html, date_plan)
    print(f"{libelle} ({date_plan}) : {len(entrees)} entrée(s) 'Entfall' pour la classe {CLASSE_CIBLE}.")

    nouvelles = [e for e in entrees if e["cle"] not in deja_signalees]
    if nouvelles:
        corps = formater_entrees(nouvelles, date_plan, url)
        envoyer_email(f"Vertretungsplan {CLASSE_CIBLE} — {libelle} ({date_plan})", corps)
        print(f"  -> Email de test envoyé ({len(nouvelles)} nouvelle(s) entrée(s)).")
    else:
        print("  -> Rien de nouveau (ou aucune entrée trouvée).")

    return [e["cle"] for e in entrees]


def main():
    print("=== TEST — exécution forcée, sans contrainte d'heure ===")
    etat = lire_etat()
    deja_signalees = set(etat.get("signaled", []))

    cles_aujourdhui = verifier_page(URL_AUJOURDHUI, "aujourd'hui", etat, deja_signalees)
    cles_demain = verifier_page(URL_DEMAIN, "demain", etat, deja_signalees)

    etat["signaled"] = list(deja_signalees | set(cles_aujourdhui) | set(cles_demain))
    ecrire_etat(etat)
    print("=== Fin du test ===")


if __name__ == "__main__":
    main()
