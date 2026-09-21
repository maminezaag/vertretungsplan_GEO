"""
Surveillance du plan de remplacement (Vertretungsplan) du Gymnasium Eversten
pour la classe 5d — alerte par email en cas d'heure "Entfall" (cours annulé).

Fonctionnement (simplifié) :
  - Le script ne gère plus lui-même les horaires : c'est le planificateur
    externe (cron configuré via crontab.org, dans le workflow GitHub
    Actions) qui décide QUAND l'appeler.
  - À CHAQUE exécution, le script :
      1. Nettoie le fichier d'état : ne conserve que les entrées
         correspondant à AUJOURD'HUI et à DEMAIN (toute entrée plus
         ancienne, ex. de la veille, est supprimée).
      2. Relit la page du jour (subst_001.htm) ET la page du lendemain
         (subst_002.htm).
      3. Compare les entrées "Entfall" trouvées pour la 5d à celles déjà
         signalées dans le fichier d'état.
      4. S'il y a du NOUVEAU (par rapport à l'état précédent) : envoie un
         email avec uniquement les nouvelles entrées, et met à jour l'état.
      5. S'il n'y a rien de nouveau : aucun email n'est envoyé, mais
         l'état est quand même réécrit (pour refléter le nettoyage /
         la date courante).
"""

import os
import re
import json
import smtplib
from email.mime.text import MIMEText
from html import unescape
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# --- Configuration ---------------------------------------------------------

URL_AUJOURDHUI = "https://vertretungsplan.gymnasium-eversten.de/oeffentlich/subst_001.htm"
URL_DEMAIN = "https://vertretungsplan.gymnasium-eversten.de/oeffentlich/subst_002.htm"

CLASSE_CIBLE = "5d"
ART_CIBLE = "entfall"  # comparaison insensible à la casse

EMAIL_EXPEDITEUR = os.environ["GMAIL_ADDRESS"]
EMAIL_MOT_DE_PASSE = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_DESTINATAIRE = os.environ["GMAIL_TO"]

FICHIER_ETAT = Path(__file__).parent / "etat_vertretung_5d.json"

TZ_BERLIN = ZoneInfo("Europe/Berlin")


# --- Récupération et analyse de la page -------------------------------------

def recuperer_page(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def nettoyer_cellule(fragment_html: str) -> str:
    """Enlève les balises HTML restantes et décode les caractères
    spéciaux (ex : &amp; -> &) d'un morceau de HTML."""
    texte = re.sub(r"<[^>]+>", "", fragment_html)
    return unescape(texte).strip()


JOURS_SEMAINE = r"Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag"


def extraire_date_plan(page_html: str) -> str:
    """Extrait la date du PLAN affichée sur la page (ex : '18.9.2026 Freitag').

    Le mot suivant la date DOIT être un jour de la semaine allemand, pour
    ne pas confondre avec la date "gültig ab ..." (validité générale du
    plan) qui apparaît plus haut sur la page.
    """
    texte = nettoyer_cellule(page_html)
    m = re.search(rf"\d{{1,2}}\.\d{{1,2}}\.\d{{4}}\s+(?:{JOURS_SEMAINE})", texte)
    return m.group(0) if m else "date inconnue"


def extraire_entrees_entfall(page_html: str, date_plan: str, classe: str = CLASSE_CIBLE):
    """Renvoie la liste des entrées 'Entfall' pour la classe donnée.

    Chaque entrée est un dict avec une clé unique 'cle' utilisée pour la
    déduplication (date + heure + classe + matière + type).
    """
    entrees = []
    lignes = re.findall(r"<tr[^>]*>(.*?)</tr>", page_html, re.S | re.I)

    for ligne in lignes:
        cellules_html = re.findall(r"<td[^>]*>(.*?)</td>", ligne, re.S | re.I)
        cellules = [nettoyer_cellule(c) for c in cellules_html]
        if len(cellules) < 5:
            continue  # ligne d'en-tête, séparation, ou vide

        stunde, klasse, _raum_old, _raum_new, art = cellules[0:5]
        fach_old = cellules[5] if len(cellules) > 5 else ""
        fach_new = cellules[6] if len(cellules) > 6 else ""
        remarque = cellules[8] if len(cellules) > 8 else ""

        if not klasse or not art:
            continue  # ligne de séparation (nom de classe seul)

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


# --- État persistant ---------------------------------------------------------
#
# Structure du fichier d'état :
# {
#   "signaled": {
#       "2026-09-21": ["cle1", "cle2", ...],   # entrées déjà signalées pour AUJOURD'HUI
#       "2026-09-22": ["cle3", ...]            # entrées déjà signalées pour DEMAIN
#   }
# }
#
# Les clés du dict "signaled" sont des dates ISO (année-mois-jour) calculées
# à partir de la date d'exécution du script (pas du texte de la page), ce qui
# permet un nettoyage simple : à chaque exécution, on ne garde que les clés
# correspondant à aujourd'hui et à demain.

def lire_etat() -> dict:
    if FICHIER_ETAT.exists():
        try:
            return json.loads(FICHIER_ETAT.read_text())
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def ecrire_etat(etat: dict):
    FICHIER_ETAT.write_text(json.dumps(etat, ensure_ascii=False, indent=2))


# --- Email ---------------------------------------------------------------

def envoyer_email(sujet: str, corps: str):
    msg = MIMEText(corps)
    msg["Subject"] = sujet
    msg["From"] = EMAIL_EXPEDITEUR
    msg["To"] = EMAIL_DESTINATAIRE
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as serveur:
        serveur.login(EMAIL_EXPEDITEUR, EMAIL_MOT_DE_PASSE)
        serveur.send_message(msg)


def formater_entrees(entrees, date_plan: str, url: str) -> str:
    """Corps de l'email — en ALLEMAND (langue des destinataires)."""
    lignes = [f"Vertretungsplan vom {date_plan} — Klasse {CLASSE_CIBLE}\n"]
    for e in entrees:
        ligne = f"- Stunde {e['stunde']}: {e['fach'] or '(Fach unbekannt)'} — Entfall"
        if e["remarque"]:
            ligne += f" ({e['remarque']})"
        lignes.append(ligne)
    lignes.append(f"\nQuelle: {url}")
    return "\n".join(lignes)


# --- Traitement d'une page (aujourd'hui OU demain) --------------------------

def traiter_page(url: str, date_iso: str, signaled: dict, libelle_sujet: str) -> bool:
    """Récupère la page, envoie un email s'il y a de nouvelles entrées
    'Entfall', et met à jour `signaled[date_iso]` en place.

    Renvoie True si un email a été envoyé.
    """
    try:
        page_html = recuperer_page(url)
    except Exception as e:
        print(f"[ERREUR] Impossible de récupérer {url} : {e}")
        return False

    date_plan = extraire_date_plan(page_html)
    entrees = extraire_entrees_entfall(page_html, date_plan)
    deja_signalees = set(signaled.get(date_iso, []))
    nouvelles = [e for e in entrees if e["cle"] not in deja_signalees]

    print(
        f"{date_iso} ({url.rsplit('/', 1)[-1]}) — "
        f"{len(entrees)} entrée(s) 'Entfall' au total, {len(nouvelles)} nouvelle(s)."
    )

    email_envoye = False
    if nouvelles:
        corps = formater_entrees(nouvelles, date_plan, url)
        envoyer_email(
            f"Vertretungsplan Klasse {CLASSE_CIBLE} — Entfall {libelle_sujet} ({date_plan})",
            corps,
        )
        print("Email envoyé.")
        email_envoye = True

    # On garde toutes les entrées actuellement présentes sur la page,
    # plus celles déjà signalées auparavant (au cas où une entrée aurait
    # temporairement disparu de la page puis reviendrait, elle ne serait
    # pas re-signalée inutilement).
    signaled[date_iso] = sorted(deja_signalees | {e["cle"] for e in entrees})
    return email_envoye


# --- Logique principale -------------------------------------------------

def main():
    maintenant = datetime.now(TZ_BERLIN)
    aujourdhui = maintenant.date()
    demain = aujourdhui + timedelta(days=1)
    today_iso = aujourdhui.isoformat()
    tomorrow_iso = demain.isoformat()

    etat = lire_etat()
    signaled = etat.get("signaled", {})

    # --- Nettoyage : on ne garde que les entrées d'aujourd'hui et de demain ---
    supprimees = [d for d in signaled if d not in (today_iso, tomorrow_iso)]
    for d in supprimees:
        del signaled[d]
    if supprimees:
        print(f"Nettoyage — entrées supprimées pour : {', '.join(supprimees)}")

    # --- Vérification des deux pages ---
    traiter_page(URL_AUJOURDHUI, today_iso, signaled, "heute")
    traiter_page(URL_DEMAIN, tomorrow_iso, signaled, "morgen")

    # --- Sauvegarde de l'état (dans tous les cas, même sans nouveauté) ---
    etat["signaled"] = signaled
    ecrire_etat(etat)


if __name__ == "__main__":
    main()
