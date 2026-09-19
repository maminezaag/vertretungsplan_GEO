"""
Surveillance du plan de remplacement (Vertretungsplan) du Gymnasium Eversten
pour la classe 5d — alerte par email en cas d'heure "Entfall" (cours annulé).

Cycle quotidien (heure de Berlin) :
  - 20h00 : lit la page du LENDEMAIN (subst_002.htm), envoie un email
            complet avec toutes les heures "Entfall" trouvées pour la 5d.
  - 07h00 : relit la page du jour même (subst_001.htm) et renvoie un email
            complet (le contenu peut répéter celui de 20h, c'est voulu).
  - 08h00 à 15h30 : vérifie régulièrement la page du jour, envoie un email
            UNIQUEMENT si une nouvelle entrée (non signalée auparavant)
            apparaît.
  - 15h30 : arrête le cycle et supprime le fichier d'état
            (la prochaine action utile aura lieu à 20h00).

Le script est conçu pour être appelé fréquemment (ex. toutes les 10 min)
par un planificateur externe (GitHub Actions) ; c'est LUI qui décide, en
fonction de l'heure de Berlin, ce qu'il doit faire à chaque appel. Cela
évite les soucis liés au changement d'heure été/hiver (le cron de GitHub
Actions est toujours en UTC).
"""

import os
import re
import json
import smtplib
from email.mime.text import MIMEText
from html import unescape
from pathlib import Path
from datetime import datetime
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
    spéciaux (ex : &amp; -> &) d'un morceau de HTML, comme le faisait
    déjà `re.search` sur du texte brut dans l'ancien script."""
    texte = re.sub(r"<[^>]+>", "", fragment_html)
    return unescape(texte).strip()


def extraire_date_plan(page_html: str) -> str:
    """Extrait la date affichée sur la page (ex : '18.9.2026 Freitag')."""
    texte = nettoyer_cellule(page_html)
    m = re.search(r"\d{1,2}\.\d{1,2}\.\d{4}\s+\w+", texte)
    return m.group(0) if m else "date inconnue"


def extraire_entrees_entfall(page_html: str, date_plan: str, classe: str = CLASSE_CIBLE):
    """Renvoie la liste des entrées 'Entfall' pour la classe donnée.

    Pas de bibliothèque externe : on repère les lignes <tr>...</tr> et
    les cellules <td>...</td> avec de simples expressions régulières,
    exactement dans le même esprit que le `re.search` de l'ancien
    script (juste appliqué à plusieurs cellules au lieu d'une seule).

    Chaque entrée est un dict avec une clé unique 'cle' utilisée pour la
    déduplication (heure + classe + matière + type).
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
        # La date fait partie de la clé : une même combinaison heure/classe/
        # matière un autre jour ne sera jamais confondue avec celle d'aujourd'hui.
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

def lire_etat() -> dict:
    if FICHIER_ETAT.exists():
        try:
            return json.loads(FICHIER_ETAT.read_text())
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def ecrire_etat(etat: dict):
    FICHIER_ETAT.write_text(json.dumps(etat, ensure_ascii=False, indent=2))


def supprimer_etat():
    if FICHIER_ETAT.exists():
        FICHIER_ETAT.unlink()


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
    lignes = [f"Plan du {date_plan} — classe {CLASSE_CIBLE}\n"]
    for e in entrees:
        ligne = f"- Heure {e['stunde']} : {e['fach'] or '(matière ?)'} — Entfall"
        if e["remarque"]:
            ligne += f" ({e['remarque']})"
        lignes.append(ligne)
    lignes.append(f"\nSource : {url}")
    return "\n".join(lignes)


# --- Logique principale -------------------------------------------------

def main():
    maintenant = datetime.now(TZ_BERLIN)
    heure, minute = maintenant.hour, maintenant.minute
    aujourdhui = maintenant.date().isoformat()

    etat = lire_etat()

    # --- 15h30 à 15h59 : arrêt du cycle, on efface l'état ---
    if heure == 15 and minute >= 30:
        if FICHIER_ETAT.exists():
            supprimer_etat()
            print("15h30 — arrêt du cycle journalier, fichier d'état supprimé.")
        else:
            print("15h30 — rien à faire (fichier d'état déjà absent).")
        return

    # --- 20h00 à 20h59 : lecture du plan du LENDEMAIN ---
    if heure == 20:
        if etat.get("dernier_envoi_soir") == aujourdhui:
            print("20h — déjà exécuté aujourd'hui, on ignore.")
            return
        try:
            page_html = recuperer_page(URL_DEMAIN)
        except Exception as e:
            print(f"[ERREUR] Impossible de récupérer {URL_DEMAIN} : {e}")
            return  # on retentera au prochain passage (toujours dans l'heure 20h)

        date_plan = extraire_date_plan(page_html)
        entrees = extraire_entrees_entfall(page_html, date_plan)
        print(f"20h — {len(entrees)} entrée(s) 'Entfall' trouvée(s) pour {CLASSE_CIBLE} ({date_plan}).")

        if entrees:
            corps = formater_entrees(entrees, date_plan, URL_DEMAIN)
            envoyer_email(f"Vertretungsplan {CLASSE_CIBLE} — Entfall demain ({date_plan})", corps)
            print("Email envoyé (soir).")

        ecrire_etat(
            {
                "date_cible": date_plan,
                "signaled": [e["cle"] for e in entrees],
                "dernier_envoi_soir": aujourdhui,
                "dernier_envoi_matin": etat.get("dernier_envoi_matin"),
            }
        )
        return

    # --- 07h00 à 07h59 : relecture complète du plan du JOUR ---
    if heure == 7:
        if etat.get("dernier_envoi_matin") == aujourdhui:
            print("7h — déjà exécuté aujourd'hui, on ignore.")
            return
        try:
            page_html = recuperer_page(URL_AUJOURDHUI)
        except Exception as e:
            print(f"[ERREUR] Impossible de récupérer {URL_AUJOURDHUI} : {e}")
            return

        date_plan = extraire_date_plan(page_html)
        entrees = extraire_entrees_entfall(page_html, date_plan)
        print(f"7h — {len(entrees)} entrée(s) 'Entfall' trouvée(s) pour {CLASSE_CIBLE} ({date_plan}).")

        if entrees:
            corps = formater_entrees(entrees, date_plan, URL_AUJOURDHUI)
            envoyer_email(f"Vertretungsplan {CLASSE_CIBLE} — Entfall aujourd'hui ({date_plan})", corps)
            print("Email envoyé (matin).")

        etat["date_cible"] = date_plan
        etat["signaled"] = [e["cle"] for e in entrees]
        etat["dernier_envoi_matin"] = aujourdhui
        ecrire_etat(etat)
        return

    # --- 08h00 à 15h29 : vérification des nouveautés uniquement ---
    if (8 <= heure < 15) or (heure == 15 and minute < 30):
        try:
            page_html = recuperer_page(URL_AUJOURDHUI)
        except Exception as e:
            print(f"[ERREUR] Impossible de récupérer {URL_AUJOURDHUI} : {e}")
            return

        date_plan = extraire_date_plan(page_html)
        entrees = extraire_entrees_entfall(page_html, date_plan)
        deja_signalees = set(etat.get("signaled", []))
        nouvelles = [e for e in entrees if e["cle"] not in deja_signalees]

        if nouvelles:
            corps = formater_entrees(nouvelles, date_plan, URL_AUJOURDHUI)
            envoyer_email(f"Vertretungsplan {CLASSE_CIBLE} — nouvelle entrée Entfall ({date_plan})", corps)
            print(f"Journée — {len(nouvelles)} nouvelle(s) entrée(s), email envoyé.")
        else:
            print("Journée — aucune nouvelle entrée.")

        etat["date_cible"] = date_plan
        etat["signaled"] = list(deja_signalees | {e["cle"] for e in entrees})
        ecrire_etat(etat)
        return

    print(f"{heure}h{minute:02d} — hors des plages surveillées, rien à faire.")


if __name__ == "__main__":
    main()
