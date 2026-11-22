"""
game.py - Moteur du Loup-Garou (logique pure, sans réseau).

Une "Partie" représente une salle : un salon d'attente, puis des cycles
nuit -> jour -> vote jusqu'à la victoire d'un camp.
Le serveur (app.py) relaie les actions des joueurs vers ce moteur et envoie à
chacun SA vue personnalisée : les rôles cachés ne sont jamais révélés.

Le moteur utilise des minuteries (threading.Timer) : tout est protégé par un
verrou (RLock) pour que les actions des joueurs et les minuteries ne se marchent
pas dessus.
"""
import functools
import random
import secrets
import threading
import time

# --- Rôles -----------------------------------------------------------------
LOUP = "Loup-Garou"
VILLAGEOIS = "Villageois"
VOYANTE = "Voyante"
SORCIERE = "Sorcière"
CHASSEUR = "Chasseur"

MIN_JOUEURS = 5
MAX_JOUEURS = 12

# Durées des phases en secondes (modifiables pour les tests)
DUREES = {
    "loups": 60,       # vote des loups
    "voyante": 40,     # sondage de la Voyante
    "sorciere": 45,    # décision de la Sorcière
    "revelation": 4,   # petit délai après une action nocturne
    "fantome": 6,      # faux délai quand le rôle appelé est mort (on ne révèle rien)
    "debat": 120,      # débat du jour
    "vote": 45,        # vote du village
    "chasseur": 30,    # tir du Chasseur
}


def erreur(message):
    """Réponse d'erreur renvoyée au client."""
    return {"erreur": message}


def ok():
    """Réponse de succès renvoyée au client."""
    return {"ok": True}


def composer_roles(n):
    """Compose la liste (mélangée) des rôles selon le nombre de joueurs."""
    nb_loups = 1 if n <= 7 else (2 if n <= 11 else 3)
    roles = [LOUP] * nb_loups + [VOYANTE, SORCIERE]
    if n >= 7:
        roles.append(CHASSEUR)
    roles += [VILLAGEOIS] * (n - len(roles))
    random.shuffle(roles)
    return roles


def synchronise(methode):
    """Décorateur : exécute la méthode en tenant le verrou de la partie."""
    @functools.wraps(methode)
    def enveloppe(self, *args, **kwargs):
        with self.verrou:
            return methode(self, *args, **kwargs)
    return enveloppe


class Joueur:
    """Un joueur humain de la salle."""

    def __init__(self, pid, nom, hote):
        self.pid = pid                        # identifiant public
        self.token = secrets.token_hex(12)    # identifiant secret (reconnexion)
        self.nom = nom
        self.sid = None                       # identifiant de connexion Socket.IO
        self.connecte = True
        self.hote = hote
        self.role = None
        self.vivant = True
        self.connus = {}                      # rôles découverts par la Voyante : pid -> rôle
        self.victoires = 0
        self.dernier_message = 0.0


class Partie:
    def __init__(self, code, durees=None, sur_changement=None):
        """
        code : code de la salle (ex. "K7QXM")
        durees : dictionnaire pour surcharger les durées par défaut
        sur_changement : fonction appelée à chaque changement d'état (pour diffuser)
        """
        self.code = code
        self.durees = {**DUREES, **(durees or {})}
        self.sur_changement = sur_changement or (lambda: None)
        self.verrou = threading.RLock()

        self.joueurs = []            # joueurs dans l'ordre des places
        self.phase = "lobby"         # lobby, nuit_loups, nuit_voyante, nuit_sorciere,
                                     # tir_chasseur, debat, vote, fin
        self.fil = []                # journal + messages de chat
        self.compteur_fil = 0
        self.epoque = 0              # change quand le fil est vidé (nouvelle partie)
        self.compteur_joueurs = 0
        self.scores = {"parties": 0, "village": 0, "loups": 0}

        self.premier = 0             # place du premier à parler (alterne à chaque partie)
        self.curseur = 0
        self.timer = None
        self.generation = 0          # invalide les minuteries périmées
        self.fin_phase = None
        self.derniere_activite = time.time()

        self._reinitialiser_etat()

    def _reinitialiser_etat(self):
        """Remet à zéro tout ce qui concerne la partie en cours."""
        self.nuit = 0
        self.vainqueur = None
        self.potion_vie = True
        self.potion_mort = True
        self.victime = None
        self.sauvee = False
        self.empoisonne = None
        self.etape_faite = False
        self.votes_loups = {}
        self.votes = {}
        self.votes_passer = set()
        self.chasseur_en_attente = None
        self.suite = "nuit"
        self.ouvreur = None

    # ---------------------------------------------------------------------
    # Utilitaires internes
    # ---------------------------------------------------------------------
    def joueur(self, token):
        return next((j for j in self.joueurs if j.token == token), None)

    def par_pid(self, pid):
        return next((j for j in self.joueurs if j.pid == pid), None)

    def vivants(self):
        return [j for j in self.joueurs if j.vivant]

    def _par_role(self, role):
        return next((j for j in self.vivants() if j.role == role), None)

    def _loups_vivants(self):
        return [j for j in self.vivants() if j.role == LOUP]

    def _notifier(self):
        self.derniere_activite = time.time()
        self.sur_changement()

    def _log(self, texte, type_="systeme", canal="public", pour=None, auteur=None):
        """Ajoute une ligne au fil. canal : public | loups | cimetiere | prive"""
        self.compteur_fil += 1
        self.fil.append({"id": self.compteur_fil, "type": type_, "canal": canal,
                         "texte": texte, "pour": pour, "auteur": auteur})
        if len(self.fil) > 400:
            self.fil.pop(0)

    def arreter_timer(self):
        """Annule la minuterie en cours (et invalide celles déjà déclenchées)."""
        self.generation += 1
        if self.timer:
            self.timer.cancel()
        self.timer = None
        self.fin_phase = None

    def _planifier(self, secondes, fonction):
        """Programme la suite de la partie dans `secondes` secondes."""
        self.arreter_timer()
        gen = self.generation

        def declencher():
            with self.verrou:
                if gen != self.generation:      # minuterie périmée : on ignore
                    return
                self.timer = None
                self.fin_phase = None
                fonction()
                self._notifier()

        self.fin_phase = time.time() + secondes
        self.timer = threading.Timer(secondes, declencher)
        self.timer.daemon = True
        self.timer.start()

    # ---------------------------------------------------------------------
    # Salon d'attente
    # ---------------------------------------------------------------------
    @synchronise
    def ajouter_joueur(self, nom):
        if self.phase != "lobby":
            return erreur("La partie a déjà commencé.")
        if len(self.joueurs) >= MAX_JOUEURS:
            return erreur("La salle est pleine.")
        nom = str(nom or "").strip()[:16]
        if not nom:
            return erreur("Choisis un pseudo.")
        if any(j.nom.lower() == nom.lower() for j in self.joueurs):
            return erreur("Ce pseudo est déjà pris dans la salle.")
        self.compteur_joueurs += 1
        joueur = Joueur("j%d" % self.compteur_joueurs, nom, hote=not self.joueurs)
        self.joueurs.append(joueur)
        self._log("%s a rejoint la salle." % nom)
        self._notifier()
        return {"ok": True, "joueur": joueur}

    @synchronise
    def reconnecter(self, token, sid):
        j = self.joueur(token)
        if not j:
            return None
        j.connecte = True
        j.sid = sid
        self._notifier()
        return j

    @synchronise
    def deconnecter(self, token, sid):
        j = self.joueur(token)
        if j and j.sid == sid:
            j.connecte = False
            j.sid = None
            self._notifier()

    @synchronise
    def retirer(self, token):
        """Retire un joueur (salon ou fin de partie uniquement)."""
        if self.phase not in ("lobby", "fin"):
            return
        j = self.joueur(token)
        if not j:
            return
        self.joueurs.remove(j)
        if j.hote and self.joueurs:
            self.joueurs[0].hote = True
        self._log("%s a quitté la salle." % j.nom)
        self._notifier()

    @synchronise
    def configurer(self, token, debat):
        h = self.joueur(token)
        if not h or not h.hote or self.phase != "lobby":
            return erreur("Action impossible.")
        try:
            debat = int(debat)
        except (TypeError, ValueError):
            return erreur("Durée invalide.")
        if debat not in (60, 120, 180, 300):
            return erreur("Durée invalide.")
        self.durees["debat"] = debat
        self._notifier()
        return ok()

    # ---------------------------------------------------------------------
    # Lancement et fin de partie
    # ---------------------------------------------------------------------
    @synchronise
    def demarrer(self, token):
        h = self.joueur(token)
        if not h or not h.hote:
            return erreur("Seul l'hôte peut lancer la partie.")
        if self.phase != "lobby":
            return erreur("La partie est déjà lancée.")
        self.joueurs = [j for j in self.joueurs if j.connecte]
        if len(self.joueurs) < MIN_JOUEURS:
            return erreur("Il faut au moins %d joueurs connectés." % MIN_JOUEURS)
        roles = composer_roles(len(self.joueurs))
        for j, role in zip(self.joueurs, roles):
            j.role, j.vivant, j.connus = role, True, {}

        self._reinitialiser_etat()
        self.fil = []
        self.epoque += 1
        self.curseur = self.premier % len(self.joueurs)
        self._log("La partie commence ! Découvre ton rôle...")
        for j in self.joueurs:
            self._log("Tu es %s." % j.role, "systeme", "prive", j.pid)
        self._debut_nuit()
        self._notifier()
        return ok()

    def _victoire(self):
        loups = len(self._loups_vivants())
        autres = len(self.vivants()) - loups
        if loups == 0:
            return "village"
        if loups >= autres:
            return "loups"
        return None

    def _finir(self, vainqueur):
        self.arreter_timer()
        self.phase = "fin"
        self.vainqueur = vainqueur
        self.scores["parties"] += 1
        self.scores[vainqueur] += 1
        for j in self.joueurs:
            if ("loups" if j.role == LOUP else "village") == vainqueur:
                j.victoires += 1
        self._log("LES LOUPS-GAROUS GAGNENT !" if vainqueur == "loups" else "LE VILLAGE GAGNE !", "fin")
        self._log("Rôles : " + ", ".join("%s (%s)" % (j.nom, j.role) for j in self.joueurs))

    @synchronise
    def rejouer(self, token):
        """Nouvelle partie : retour au salon, le premier à parler change."""
        h = self.joueur(token)
        if not h or not h.hote or self.phase != "fin":
            return erreur("Action impossible.")
        self.joueurs = [j for j in self.joueurs if j.connecte]
        for j in self.joueurs:
            j.role, j.vivant, j.connus = None, True, {}
        self._reinitialiser_etat()
        self.premier = (self.premier + 1) % len(self.joueurs)
        self.phase = "lobby"
        self.fil = []
        self.epoque += 1
        self._log("Nouvelle partie ! %s ouvrira le premier débat." % self.joueurs[self.premier].nom)
        self._notifier()
        return ok()

    # ---------------------------------------------------------------------
    # La nuit
    # ---------------------------------------------------------------------
    def _debut_nuit(self):
        self.nuit += 1
        self.victime = None
        self.sauvee = False
        self.empoisonne = None
        self.votes_loups = {}
        self.etape_faite = False
        self.phase = "nuit_loups"
        self._log("Nuit %d : le village s'endort." % self.nuit)
        self._log("Les Loups-Garous se réveillent...")
        self._planifier(self.durees["loups"], self._resoudre_loups)

    @synchronise
    def vote_loups(self, token, cible_id):
        j = self.joueur(token)
        if not j or not j.vivant or j.role != LOUP or self.phase != "nuit_loups":
            return erreur("Action impossible.")
        c = self.par_pid(cible_id)
        if not c or not c.vivant or c.role == LOUP:
            return erreur("Cible invalide.")
        self.votes_loups[j.pid] = c.pid
        self._log("%s propose de dévorer %s." % (j.nom, c.nom), "systeme", "loups")
        if all(l.pid in self.votes_loups for l in self._loups_vivants()):
            self._resoudre_loups()
        self._notifier()
        return ok()

    def _resoudre_loups(self):
        """Les loups s'accordent : cible la plus votée (égalité = tirage au sort)."""
        decompte = {}
        for pid in self.votes_loups.values():
            decompte[pid] = decompte.get(pid, 0) + 1
        maximum = max(decompte.values(), default=0)
        if maximum > 0:
            pid = random.choice([p for p, n in decompte.items() if n == maximum])
        else:   # personne n'a voté
            pid = random.choice([j for j in self.vivants() if j.role != LOUP]).pid
        self.victime = self.par_pid(pid)
        self._etape_voyante()

    def _etape_voyante(self):
        self.phase = "nuit_voyante"
        self.etape_faite = False
        self._log("Les Loups se rendorment. La Voyante se réveille...")
        # Si la Voyante est morte, on simule un délai pour ne rien révéler
        duree = self.durees["voyante"] if self._par_role(VOYANTE) else self.durees["fantome"]
        self._planifier(duree, self._etape_sorciere)

    @synchronise
    def sonder(self, token, cible_id):
        j = self.joueur(token)
        if (not j or not j.vivant or j.role != VOYANTE
                or self.phase != "nuit_voyante" or self.etape_faite):
            return erreur("Action impossible.")
        c = self.par_pid(cible_id)
        if not c or not c.vivant or c.pid == j.pid or c.pid in j.connus:
            return erreur("Cible invalide.")
        j.connus[c.pid] = c.role
        self._log("Révélation : %s est %s !" % (c.nom, c.role), "systeme", "prive", j.pid)
        self.etape_faite = True
        self._planifier(self.durees["revelation"], self._etape_sorciere)
        self._notifier()
        return ok()

    def _etape_sorciere(self):
        self.phase = "nuit_sorciere"
        self.etape_faite = False
        self._log("La Voyante se rendort. La Sorcière se réveille...")
        s = self._par_role(SORCIERE)
        if s and self.victime:
            self._log("Les loups ont attaqué %s." % self.victime.nom, "systeme", "prive", s.pid)
        self._planifier(self.durees["sorciere"] if s else self.durees["fantome"], self._aube)

    @synchronise
    def sorciere(self, token, action, cible_id=None):
        j = self.joueur(token)
        if (not j or not j.vivant or j.role != SORCIERE
                or self.phase != "nuit_sorciere" or self.etape_faite):
            return erreur("Action impossible.")
        if action == "sauver":
            if not self.potion_vie or not self.victime:
                return erreur("Potion de vie indisponible.")
            self.potion_vie = False
            self.sauvee = True
            self._log("Tu as sauvé %s." % self.victime.nom, "systeme", "prive", j.pid)
        elif action == "poison":
            c = self.par_pid(cible_id)
            if not self.potion_mort or not c or not c.vivant or c.pid == j.pid:
                return erreur("Potion de mort indisponible.")
            self.potion_mort = False
            self.empoisonne = c
            self._log("Tu as empoisonné %s." % c.nom, "systeme", "prive", j.pid)
        elif action != "rien":
            return erreur("Action inconnue.")
        self.etape_faite = True
        self._planifier(self.durees["revelation"], self._aube)
        self._notifier()
        return ok()

    # ---------------------------------------------------------------------
    # Le jour
    # ---------------------------------------------------------------------
    def _aube(self):
        morts = []
        if self.victime and not self.sauvee:
            morts.append(self.victime)
        if self.empoisonne and self.empoisonne not in morts:
            morts.append(self.empoisonne)
        self._log("Le jour se lève sur le village.")
        if not morts:
            self._log("Miracle : personne n'est mort cette nuit !")
        for m in morts:
            self._tuer(m, "a été retrouvé(e) mort(e) au petit matin")
        self._apres_morts("debat")

    def _tuer(self, joueur, cause):
        """Tue un joueur et révèle son rôle."""
        joueur.vivant = False
        self._log("%s %s. Son rôle était : %s." % (joueur.nom, cause, joueur.role), "mort")
        if joueur.role == CHASSEUR:
            self.chasseur_en_attente = joueur

    def _apres_morts(self, suite):
        """Après des morts : tir du Chasseur éventuel, test de victoire, puis 'debat' ou 'nuit'."""
        if self.chasseur_en_attente and self._loups_vivants():
            c = self.chasseur_en_attente
            self.suite = suite
            self.phase = "tir_chasseur"
            self._log("%s épaule son fusil... il peut emporter quelqu'un avec lui !" % c.nom)

            def trop_tard():
                self._log("%s hésite trop longtemps et ne tire pas." % c.nom)
                self.chasseur_en_attente = None
                self._apres_morts(suite)

            self._planifier(self.durees["chasseur"], trop_tard)
            return
        self.chasseur_en_attente = None
        v = self._victoire()
        if v:
            self._finir(v)
        elif suite == "debat":
            self._debut_debat()
        else:
            self._debut_nuit()

    @synchronise
    def tirer(self, token, cible_id):
        j = self.joueur(token)
        if not j or self.phase != "tir_chasseur" or self.chasseur_en_attente is not j:
            return erreur("Action impossible.")
        c = self.par_pid(cible_id)
        if not c or not c.vivant:
            return erreur("Cible invalide.")
        self.chasseur_en_attente = None
        self._tuer(c, "est abattu(e) par le Chasseur")
        self._apres_morts(self.suite)
        self._notifier()
        return ok()

    def _debut_debat(self):
        self.phase = "debat"
        self.votes_passer = set()
        # L'ouvreur tourne : on repart de la place suivante à chaque jour
        n = len(self.joueurs)
        for i in range(n):
            idx = (self.curseur + i) % n
            if self.joueurs[idx].vivant:
                self.ouvreur = self.joueurs[idx].pid
                self.curseur = (idx + 1) % n
                break
        self._log("Le débat commence. %s prend la parole en premier." % self.par_pid(self.ouvreur).nom)
        self._planifier(self.durees["debat"], self._debut_vote)

    @synchronise
    def passer_debat(self, token):
        """Vote 'passer au vote' : la majorité des vivants suffit."""
        j = self.joueur(token)
        if not j or not j.vivant or self.phase != "debat":
            return erreur("Action impossible.")
        self.votes_passer.add(j.pid)
        if len(self.votes_passer) >= len(self.vivants()) // 2 + 1:
            self._debut_vote()
        self._notifier()
        return ok()

    def _debut_vote(self):
        self.phase = "vote"
        self.votes = {}
        self._log("Le vote commence ! Qui sera condamné ?")
        self._planifier(self.durees["vote"], self._resoudre_vote)

    @synchronise
    def voter(self, token, cible_id):
        """cible_id vide/None = vote blanc."""
        j = self.joueur(token)
        if not j or not j.vivant or self.phase != "vote":
            return erreur("Action impossible.")
        if cible_id:
            c = self.par_pid(cible_id)
            if not c or not c.vivant or c.pid == j.pid:
                return erreur("Cible invalide.")
            self.votes[j.pid] = c.pid
        else:
            self.votes[j.pid] = None
        if all(v.pid in self.votes for v in self.vivants()):
            self._resoudre_vote()
        self._notifier()
        return ok()

    def _resoudre_vote(self):
        decompte = {}
        detail = []
        for v in self.vivants():
            cible = self.par_pid(self.votes[v.pid]) if self.votes.get(v.pid) else None
            if cible:
                decompte[cible.pid] = decompte.get(cible.pid, 0) + 1
            detail.append("%s → %s" % (v.nom, cible.nom if cible else "blanc"))
        self._log("Votes : " + " | ".join(detail))
        classes = sorted(decompte.items(), key=lambda kv: kv[1], reverse=True)
        if not classes or (len(classes) > 1 and classes[0][1] == classes[1][1]):
            self._log("Égalité (ou aucun vote) : personne n'est éliminé.")
        else:
            condamne = self.par_pid(classes[0][0])
            self._log("Le village condamne %s (%d voix)." % (condamne.nom, classes[0][1]))
            self._tuer(condamne, "est condamné(e) par le village")
        self._apres_morts("nuit")

    # ---------------------------------------------------------------------
    # Chat
    # ---------------------------------------------------------------------
    def canal_chat(self, j):
        """Canal dans lequel le joueur peut écrire (None = muet)."""
        if self.phase in ("lobby", "fin"):
            return "public"
        if not j.vivant:
            return "cimetiere"
        if self.phase in ("debat", "vote", "tir_chasseur"):
            return "public"
        if self.phase.startswith("nuit") and j.role == LOUP:
            return "loups"
        return None   # la nuit, les non-loups dorment

    @synchronise
    def chat(self, token, texte):
        j = self.joueur(token)
        if not j:
            return erreur("Joueur inconnu.")
        texte = str(texte or "").strip()[:300]
        if not texte:
            return erreur("Message vide.")
        if time.time() - j.dernier_message < 0.4:
            return erreur("Doucement !")
        canal = self.canal_chat(j)
        if not canal:
            return erreur("Chut... tu dors.")
        j.dernier_message = time.time()
        self._log(texte, "chat", canal, None, j.nom)
        self._notifier()
        return ok()

    # ---------------------------------------------------------------------
    # Vue personnalisée envoyée à chaque joueur
    # (les clés sont en camelCase : c'est ce qu'attend le code JavaScript du navigateur)
    # ---------------------------------------------------------------------
    def _action_pour(self, moi):
        # Un mort n'agit pas (sauf le Chasseur qui tire)
        if not moi.vivant and not (self.phase == "tir_chasseur" and self.chasseur_en_attente is moi):
            return None
        vivants = self.vivants()
        if self.phase == "nuit_loups" and moi.role == LOUP:
            return {"type": "loups", "cibles": [j.pid for j in vivants if j.role != LOUP],
                    "votes": dict(self.votes_loups)}
        if self.phase == "nuit_voyante" and moi.role == VOYANTE:
            return {"type": "voyante", "fait": self.etape_faite,
                    "cibles": [j.pid for j in vivants if j.pid != moi.pid and j.pid not in moi.connus]}
        if self.phase == "nuit_sorciere" and moi.role == SORCIERE:
            return {"type": "sorciere", "fait": self.etape_faite,
                    "victime": self.victime.pid if self.victime else None,
                    "potionVie": self.potion_vie, "potionMort": self.potion_mort,
                    "cibles": [j.pid for j in vivants if j.pid != moi.pid]}
        if self.phase == "tir_chasseur" and self.chasseur_en_attente is moi:
            return {"type": "chasseur", "cibles": [j.pid for j in vivants]}
        if self.phase == "debat":
            return {"type": "debat", "aPasse": moi.pid in self.votes_passer,
                    "nb": len(self.votes_passer), "requis": len(vivants) // 2 + 1}
        if self.phase == "vote":
            action = {"type": "vote", "cibles": [j.pid for j in vivants if j.pid != moi.pid],
                      "aVote": list(self.votes.keys())}
            if moi.pid in self.votes:            # absent tant qu'il n'a pas voté
                action["monVote"] = self.votes[moi.pid]
            return action
        return None

    @synchronise
    def vue(self, token):
        moi = self.joueur(token)
        if not moi:
            return None
        fin = self.phase == "fin"

        def role_visible(j):
            """Rôle visible : le sien, celui des morts, les alliés loups, les rôles sondés."""
            if j.pid == moi.pid:
                return j.role
            if fin or (not j.vivant and self.phase != "lobby"):
                return j.role
            if moi.role == LOUP and j.role == LOUP:
                return LOUP
            return moi.connus.get(j.pid)

        def visible(e):
            """Visibilité d'une ligne du fil selon son canal."""
            c = e["canal"]
            return (c == "public"
                    or (c == "prive" and e["pour"] == moi.pid)
                    or (c == "loups" and (moi.role == LOUP or fin))
                    or (c == "cimetiere" and (not moi.vivant or fin)))

        return {
            "code": self.code,
            "phase": self.phase,
            "nuit": self.nuit,
            "epoque": self.epoque,
            "reste": max(0, int((self.fin_phase - time.time()) * 1000)) if self.fin_phase else None,
            "dureeDebat": self.durees["debat"],
            "minJoueurs": MIN_JOUEURS,
            "maxJoueurs": MAX_JOUEURS,
            "vainqueur": self.vainqueur,
            "scores": dict(self.scores),
            "ouvreur": self.ouvreur,
            "premierNom": self.joueurs[self.premier % len(self.joueurs)].nom if self.joueurs else None,
            "moi": {"pid": moi.pid, "nom": moi.nom, "role": moi.role,
                    "vivant": moi.vivant, "hote": moi.hote},
            "joueurs": [{"pid": j.pid, "nom": j.nom, "vivant": j.vivant, "connecte": j.connecte,
                         "hote": j.hote, "victoires": j.victoires, "role": role_visible(j)}
                        for j in self.joueurs],
            "action": self._action_pour(moi),
            "canalChat": self.canal_chat(moi),
            "fil": [{"id": e["id"], "type": e["type"], "canal": e["canal"],
                     "texte": e["texte"], "auteur": e["auteur"]}
                    for e in self.fil if visible(e)],
        }
