"""
app.py - Serveur Flask + Flask-SocketIO du Loup-Garou multijoueur.

Rôle du serveur :
  - servir les fichiers du dossier static/ (page, style, script, décor) ;
  - gérer les salles (une salle = une Partie du moteur game.py) ;
  - relayer les actions des joueurs et diffuser à chacun sa vue personnalisée.

Lien d'invitation : http://<adresse>:<port>/salle/<CODE>
"""
import os
import random
import socket
import threading
import time

from flask import Flask, request, send_from_directory
from flask_socketio import SocketIO

from game import Partie

PORT = int(os.environ.get("PORT", 3000))
DOSSIER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# static_url_path="" : /style.css, /app.js... sont servis depuis le dossier static/
app = Flask(__name__, static_folder=DOSSIER, static_url_path="")
socketio = SocketIO(app, async_mode="threading")

# Toutes les salles actives : code -> Partie
salles = {}
# Connexions en cours : sid Socket.IO -> {"code": ..., "token": ...}
connexions = {}


@app.route("/")
@app.route("/salle/<code>")
def page(code=None):
    """Un lien /salle/ABCDE ouvre la même page : le client lit le code dans l'URL."""
    return send_from_directory(DOSSIER, "index.html")


def generer_code():
    """Génère un code de salle de 5 lettres (sans caractères ambigus)."""
    lettres = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    while True:
        code = "".join(random.choice(lettres) for _ in range(5))
        if code not in salles:
            return code


def diffuser(salle):
    """Envoie à chaque joueur connecté sa vue personnalisée de la salle."""
    for j in list(salle.joueurs):
        if j.sid:
            socketio.emit("etat", salle.vue(j.token), to=j.sid)


def lier(salle, joueur):
    """Attache la connexion courante à un joueur de la salle."""
    with salle.verrou:
        joueur.sid = request.sid
        joueur.connecte = True
    connexions[request.sid] = {"code": salle.code, "token": joueur.token}


# ---------------------------------------------------------------------------
# Événements Socket.IO (la valeur renvoyée par un gestionnaire sert d'accusé de réception)
# ---------------------------------------------------------------------------
@socketio.on("creer")
def creer(data=None):
    """Création d'une salle : le créateur en devient l'hôte."""
    code = generer_code()
    salle = Partie(code, sur_changement=lambda: diffuser(salle))
    r = salle.ajouter_joueur((data or {}).get("nom"))
    if "erreur" in r:
        return r
    salles[code] = salle
    lier(salle, r["joueur"])
    diffuser(salle)
    return {"ok": True, "code": code, "token": r["joueur"].token}


@socketio.on("rejoindre")
def rejoindre(data=None):
    """Rejoindre une salle (ou s'y reconnecter avec son jeton secret)."""
    data = data or {}
    salle = salles.get(str(data.get("code") or "").upper())
    if not salle:
        return {"erreur": "Salle introuvable. Vérifie le lien ou le code."}
    jeton = data.get("token")
    joueur = salle.reconnecter(jeton, request.sid) if jeton else None
    if not joueur:
        r = salle.ajouter_joueur(data.get("nom"))
        if "erreur" in r:
            return r
        joueur = r["joueur"]
    lier(salle, joueur)
    diffuser(salle)
    return {"ok": True, "code": salle.code, "token": joueur.token}


@socketio.on("action")
def action(msg=None):
    """Actions de jeu : on les transmet au moteur."""
    info = connexions.get(request.sid)
    salle = salles.get(info["code"]) if info else None
    if not salle:
        return {"erreur": "Tu n'es dans aucune salle."}
    t, msg = info["token"], msg or {}
    genre, cible = msg.get("type"), msg.get("cibleId")
    if genre == "demarrer":
        return salle.demarrer(t)
    if genre == "rejouer":
        return salle.rejouer(t)
    if genre == "configurer":
        return salle.configurer(t, msg.get("debat"))
    if genre == "voteLoups":
        return salle.vote_loups(t, cible)
    if genre == "sonder":
        return salle.sonder(t, cible)
    if genre == "sorciere":
        return salle.sorciere(t, msg.get("action"), cible)
    if genre == "tirer":
        return salle.tirer(t, cible)
    if genre == "passerDebat":
        return salle.passer_debat(t)
    if genre == "voter":
        return salle.voter(t, cible)
    if genre == "chat":
        return salle.chat(t, msg.get("texte"))
    return {"erreur": "Action inconnue."}


@socketio.on("disconnect")
def deconnexion(*_):
    """Le joueur peut revenir (rafraîchissement de page, coupure réseau)."""
    info = connexions.pop(request.sid, None)
    salle = salles.get(info["code"]) if info else None
    if not salle:
        return
    salle.deconnecter(info["token"], request.sid)

    def retirer_si_absent():
        j = salle.joueur(info["token"])
        if j and not j.connecte:
            salle.retirer(info["token"])   # n'agit qu'au salon ou en fin de partie

    threading.Timer(15, retirer_si_absent).start()


def nettoyer():
    """Supprime les salles vides ou inactives depuis 10 minutes."""
    while True:
        time.sleep(60)
        for code, salle in list(salles.items()):
            personne = not any(j.connecte for j in salle.joueurs)
            if personne and time.time() - salle.derniere_activite > 600:
                salle.arreter_timer()
                salles.pop(code, None)


def adresse_reseau():
    """Adresse IP locale (pour inviter des joueurs du même réseau)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None


if __name__ == "__main__":
    threading.Thread(target=nettoyer, daemon=True).start()
    print("\n  LOUP-GAROU - serveur lancé sur le port %d" % PORT)
    print("  Local   : http://localhost:%d" % PORT)
    ip = adresse_reseau()
    if ip:
        print("  Réseau  : http://%s:%d" % (ip, PORT))
    print()
    socketio.run(app, host="0.0.0.0", port=PORT, allow_unsafe_werkzeug=True)
