"""
test_game.py - Tests du moteur de jeu (lancer avec : python test_game.py).
Des "robots" jouent des parties complètes avec des durées minuscules.
"""
import random
import time

from game import (Partie, composer_roles, LOUP, VOYANTE, SORCIERE, CHASSEUR)

DUREES_TEST = {"loups": 0.015, "voyante": 0.015, "sorciere": 0.015, "revelation": 0.002,
               "fantome": 0.003, "debat": 0.015, "vote": 0.015, "chasseur": 0.015}


def creer_salle(n):
    """Crée une salle avec n joueurs connectés."""
    salle = Partie("TEST", durees=DUREES_TEST)
    for i in range(n):
        salle.ajouter_joueur("Joueur%d" % i)
    return salle


# --- 1. Distribution des rôles ---
for n in range(5, 13):
    roles = composer_roles(n)
    assert len(roles) == n
    assert LOUP in roles and VOYANTE in roles and SORCIERE in roles
    assert (CHASSEUR in roles) == (n >= 7)

# --- 2. Règles du salon ---
s = creer_salle(4)
assert "erreur" in s.demarrer(s.joueurs[0].token), "pas de lancement à moins de 5 joueurs"
assert "erreur" in s.ajouter_joueur("joueur1"), "pseudo en double refusé"
s2 = creer_salle(5)
assert "erreur" in s2.demarrer(s2.joueurs[1].token), "seul l'hôte lance la partie"
assert s2.demarrer(s2.joueurs[0].token).get("ok")
assert "erreur" in s2.ajouter_joueur("Retardataire"), "pas de nouvel arrivant en cours de partie"
s2.arreter_timer()

# --- 3. Confidentialité des rôles et du chat ---
s = creer_salle(8)
s.demarrer(s.joueurs[0].token)
loups = [j for j in s.joueurs if j.role == LOUP]
villageois = next(j for j in s.joueurs if j.role != LOUP)
vue_v = s.vue(villageois.token)
for j in vue_v["joueurs"]:
    if j["pid"] != villageois.pid:
        assert j["role"] is None, "rôle caché"
vue_l = s.vue(loups[0].token)
for l in loups:
    assert next(j for j in vue_l["joueurs"] if j["pid"] == l.pid)["role"] == LOUP, "loups alliés visibles"
assert s.phase == "nuit_loups"
assert "erreur" in s.chat(villageois.token, "coucou"), "les dormeurs ne parlent pas"
assert s.chat(loups[0].token, "on mange qui ?").get("ok"), "les loups chuchotent"
assert not any(l["texte"] == "on mange qui ?" for l in s.vue(villageois.token)["fil"]), "chat des loups invisible"
assert any(l["texte"] == "on mange qui ?" for l in s.vue(loups[0].token)["fil"])
s.arreter_timer()


# --- 4. Parties complètes jouées par des robots ---
def partie_robots(n):
    s = creer_salle(n)
    s.demarrer(s.joueurs[0].token)
    for _ in range(4000):
        if s.phase == "fin":
            break
        for j in list(s.joueurs):
            a = s.vue(j.token)["action"]
            if not a:
                continue
            if a["type"] == "loups":
                s.vote_loups(j.token, random.choice(a["cibles"]))
            elif a["type"] == "voyante" and not a["fait"] and a["cibles"]:
                s.sonder(j.token, random.choice(a["cibles"]))
            elif a["type"] == "sorciere" and not a["fait"]:
                r = random.random()
                if r < 0.3 and a["potionVie"]:
                    s.sorciere(j.token, "sauver")
                elif r < 0.5 and a["potionMort"]:
                    s.sorciere(j.token, "poison", random.choice(a["cibles"]))
                else:
                    s.sorciere(j.token, "rien")
            elif a["type"] == "chasseur":
                s.tirer(j.token, random.choice(a["cibles"]))
            elif a["type"] == "debat":
                s.passer_debat(j.token)
            elif a["type"] == "vote":
                s.voter(j.token, random.choice(a["cibles"]) if random.random() < 0.9 else "")
        time.sleep(0.002)
    assert s.phase == "fin", "la partie à %d joueurs doit se terminer" % n
    assert s.vainqueur in ("village", "loups")
    vainqueur, avant = s.vainqueur, s.premier
    # Rejouer : retour au salon, le premier à parler change
    assert s.rejouer(s.joueurs[0].token).get("ok")
    assert s.phase == "lobby"
    assert s.premier != avant, "le premier à parler alterne"
    return vainqueur


bilan = {"village": 0, "loups": 0}
for n in range(5, 13):
    for _ in range(10):
        bilan[partie_robots(n)] += 1
print("Tous les tests passent. Parties robots :", bilan)
