"""
Surveillance du plan de remplacement (Vertretungsplan) du Gymnasium Eversten
pour la classe 5d — alerte par email en cas d'heure "Entfall" (cours annulé).

Fonctionnement :
  - Le planificateur externe (cron-job.org) appelle ce workflow DE LA MÊME
    FAÇON à n'importe quel moment (un seul cronjob, un seul URL, aucun
    paramètre). C'est LE SCRIPT qui détecte l'heure d'exécution (heure de
    Berlin) et choisit lui-même le mode adéquat :

        07h00–07h59  → matin   : lit la page d'AUJOURD'HUI (subst_001.htm)
                                  et envoie TOUJOURS un email complet avec
                                  toutes les entrées "Entfall" de la 5d.
                                  (Une seule fois par jour — garde interne.)
        08h00–12h59  → journee : relit la page d'AUJOURD'HUI et envoie un
                                  email UNIQUEMENT si une entrée nouvelle
                                  (absente de la baseline précédente) est
                                  détectée. Sinon : aucun email.
        19h00–20h59  → soir    : lit la page de DEMAIN (subst_002.htm) et
                                  envoie TOUJOURS un email complet.
                                  (Une seule fois par jour — garde interne.)
        autre heure  → rien à faire, le script s'arrête proprement.

  - La variable d'environnement MODE peut toujours imposer un mode
    (soir/matin/journee/auto) — utilisée pour les tests manuels via
    workflow_dispatch. "auto" ou une valeur vide = détection horaire.

  - Nettoyage automatique : à chaque exécution, quel que soit le mode, les
    entrées d'état antérieures à aujourd'hui sont supprimées (on ne garde
    que "aujourd'hui" et "demain").
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

# Fenêtres horaires (HEURE de Berlin) → mode, pour la détection automatique.
# Le script est appelé par le planificateur externe de façon IDENTIQUE toute
# la journée ; ce sont ces fenêtres qui déterminent ce qu'il fait.
FENETRES_HORAIRES = {
    "matin":   {7},                 # 07h00–07h59 : rapport complet du jour
    "journee": {8, 9, 10, 11, 12},  # 08h00–12h59 : surveillance différentielle
    "soir":    {19, 20},            # 19h00–20h59 : rapport complet du lendemain
}

# Correspondance numéro de "Stunde" -> horaire réel, pour affichage dans l'email.
STUNDEN_ZEITEN = {
    "1": "07:50–08:35",
    "2": "08:40–09:25",
    "3": "09:45–10:30",
    "4": "10:35–11:20",
    "5": "11:40–12:25",
    "6": "12:30–13:15",
}

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
#   },
#   "last_full_report": {
#       "matin": "2026-09-21",   # date du dernier rapport complet du matin
#       "soir":  "2026-09-21"    # date du dernier rapport complet du soir
#   }
# }
#
# "signaled" : clés des entrées déjà connues (baseline pour les diffs).
# "last_full_report" : garantit qu'on n'envoie le rapport complet
#   (matin/soir) qu'UNE seule fois par jour, même si le planificateur
#   externe appelle le script plusieurs fois dans la fenêtre horaire.
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
        zeit = STUNDEN_ZEITEN.get(e["stunde"].strip())
        stunde_label = f"Stunde {e['stunde']} ({zeit})" if zeit else f"Stunde {e['stunde']}"
        ligne = f"- {stunde_label}: {e['fach'] or '(Fach unbekannt)'} — Entfall"
        if e["remarque"]:
            ligne += f" ({e['remarque']})"
        lignes.append(ligne)
    lignes.append(f"\nQuelle: {url}")
    return "\n".join(lignes)


def traiter_page(url: str, date_iso: str, signaled: dict, libelle_sujet: str, forcer_envoi: bool = False):
    """Récupère la page, met à jour `signaled[date_iso]` en place, et envoie
    un email selon le mode :
      - forcer_envoi=True  : envoie TOUJOURS un mail complet avec toutes les
        entrées "Entfall" trouvées (utilisé pour les modes "matin" et "soir").
      - forcer_envoi=False : envoie uniquement les entrées NOUVELLES par
        rapport à l'état déjà enregistré (utilisé pour le mode "journee").

    Valeur de retour :
      - True  : email envoyé
      - False : page lue avec succès mais rien à envoyer
      - None  : échec de la récupération de la page (on réessaiera au
                prochain appel du planificateur)
    """
    try:
        page_html = recuperer_page(url)
    except Exception as e:
        print(f"[ERREUR] Impossible de récupérer {url} : {e}")
        return None

    date_plan = extraire_date_plan(page_html)
    entrees = extraire_entrees_entfall(page_html, date_plan)
    deja_signalees = set(signaled.get(date_iso, []))

    if forcer_envoi:
        a_envoyer = entrees
    else:
        a_envoyer = [e for e in entrees if e["cle"] not in deja_signalees]

    print(
        f"{date_iso} ({url.rsplit('/', 1)[-1]}) — "
        f"{len(entrees)} entrée(s) 'Entfall' au total, {len(a_envoyer)} à envoyer "
        f"({'envoi forcé' if forcer_envoi else 'diff uniquement'})."
    )

    email_envoye = False
    if a_envoyer:
        corps = formater_entrees(a_envoyer, date_plan, url)
        envoyer_email(
            f"Vertretungsplan Klasse {CLASSE_CIBLE} — Entfall {libelle_sujet} ({date_plan})",
            corps,
        )
        print("Email envoyé.")
        email_envoye = True
    else:
        print("Rien à envoyer.")

    # On garde toutes les entrées actuellement présentes sur la page, plus
    # celles déjà signalées auparavant (baseline pour les diffs suivants).
    signaled[date_iso] = sorted(deja_signalees | {e["cle"] for e in entrees})
    return email_envoye


# --- Détection automatique du mode -------------------------------------------

def detecter_mode(heure_berlin: int):
    """Renvoie le mode correspondant à l'heure de Berlin, ou None si on est
    en dehors de toute fenêtre horaire (dans ce cas : rien à faire)."""
    for mode, heures in FENETRES_HORAIRES.items():
        if heure_berlin in heures:
            return mode
    return None


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

    # --- Choix du mode ---
    # 1) MODE imposé par l'appelant (variable d'environnement, ex. test
    #    manuel via workflow_dispatch) ;
    # 2) sinon : détection automatique selon l'heure d'exécution à Berlin.
    mode = os.environ.get("MODE", "").strip().lower()
    if mode in ("", "auto"):
        mode = detecter_mode(maintenant.hour)
        if mode is None:
            print(f"{maintenant:%H:%M} (heure de Berlin) : hors fenêtre horaire — aucune action.")
            return
        print(f"{maintenant:%H:%M} (heure de Berlin) : mode détecté automatiquement = '{mode}'.")
    else:
        print(f"Mode imposé par l'appelant : '{mode}'.")

    if mode not in FENETRES_HORAIRES:
        print(
            f"[ERREUR] MODE inconnu : '{mode}'. "
            "Valeurs attendues : 'soir', 'matin', 'journee' ou 'auto'."
        )
        return

    # --- Garde "une seule fois par jour" pour les rapports complets ---
    # Le planificateur externe peut appeler plusieurs fois dans la fenêtre
    # (ex. toutes les 15 min entre 19h et 21h) : le rapport complet ne doit
    # partir qu'une seule fois.
    if mode in ("matin", "soir"):
        dernier_rapport = etat.get("last_full_report", {}).get(mode)
        if dernier_rapport == today_iso:
            print(f"Rapport '{mode}' déjà généré aujourd'hui ({dernier_rapport}) — ignoré.")
            return

    # --- Exécution du mode ---
    if mode == "soir":
        # Info complète sur DEMAIN, toujours envoyée (une fois par jour).
        resultat = traiter_page(URL_DEMAIN, tomorrow_iso, signaled, "morgen", forcer_envoi=True)
    elif mode == "matin":
        # Info complète sur AUJOURD'HUI, toujours renvoyée (une fois par
        # jour, même si déjà envoyée la veille à 20h). Sert aussi de
        # nouvelle "baseline" pour les vérifications de la journée.
        resultat = traiter_page(URL_AUJOURDHUI, today_iso, signaled, "heute", forcer_envoi=True)
    else:  # journee
        # Uniquement les nouveautés par rapport à l'état laissé par le
        # passage de 07h15 (ou par un passage "journee" précédent).
        resultat = traiter_page(URL_AUJOURDHUI, today_iso, signaled, "heute", forcer_envoi=False)

    # On marque le rapport complet comme "fait pour aujourd'hui" seulement si
    # la page a été lue avec succès (resultat None = échec -> on réessaiera
    # au prochain appel du planificateur dans la même fenêtre).
    if mode in ("matin", "soir") and resultat is not None:
        etat.setdefault("last_full_report", {})[mode] = today_iso

    # --- Sauvegarde de l'état (dans tous les cas, même sans nouveauté) ---
    etat["signaled"] = signaled
    ecrire_etat(etat)


if __name__ == "__main__":
    main()
