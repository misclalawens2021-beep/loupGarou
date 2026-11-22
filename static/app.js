/* app.js - Client du Loup-Garou (navigateur).
 * Le serveur envoie à chaque changement une "vue" personnalisée ('etat') ;
 * ce fichier se contente de l'afficher et d'envoyer les actions du joueur. */

const socket = io();
const $ = (sel) => document.querySelector(sel);
// Protège l'affichage contre l'injection de HTML dans les pseudos et messages
const esc = (t) => String(t).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const LOUP = 'Loup-Garou';
const DESCRIPTIONS = {
  'Loup-Garou': "Chaque nuit, ta meute dévore un villageois. Le jour, fais-toi passer pour innocent !",
  'Villageois': "Aucun pouvoir, mais ton vote compte. Démasque les loups !",
  'Voyante': "Chaque nuit, tu découvres le vrai rôle d'un joueur. Utilise ce savoir avec prudence.",
  'Sorcière': "Tu possèdes une potion de vie et une potion de mort, à usage unique (une seule par nuit).",
  'Chasseur': "Si tu meurs, tu emportes un joueur avec toi.",
};
const PHASES = {
  lobby: "SALON D'ATTENTE", nuit_loups: 'NUIT · LES LOUPS CHASSENT', nuit_voyante: 'NUIT · LA VOYANTE SONDE',
  nuit_sorciere: 'NUIT · LA SORCIÈRE AGIT', tir_chasseur: 'LE CHASSEUR TIRE', debat: 'JOUR · DÉBAT',
  vote: 'JOUR · VOTE', fin: 'FIN DE PARTIE',
};

// --- État local ---
let session = JSON.parse(sessionStorage.getItem('lg_session') || 'null');   // propre à chaque onglet
let etat = null;           // dernière vue reçue du serveur
let finLocal = null;       // instant local de fin de la phase (pour le compte à rebours)
let vueSalle = false;      // la structure de la salle est-elle construite ?
let dernierHtml = {};      // évite de redessiner un panneau inchangé
let dernierIdFil = 0;
let epoqueFil = -1;
let poisonOuvert = false;  // la Sorcière choisit-elle une cible de poison ?

// Code de salle présent dans le lien d'invitation (/salle/ABCDE ou ?salle=ABCDE)
const m = location.pathname.match(/^\/salle\/([A-Za-z]+)/);
let codeUrl = m ? m[1].toUpperCase() : (new URLSearchParams(location.search).get('salle') || '').toUpperCase();

// ---------------------------------------------------------------------
// Décor : étoiles et notifications
// ---------------------------------------------------------------------
(function creerEtoiles() {
  const g = $('#etoiles');
  for (let i = 0; i < 110; i++) {
    const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    c.setAttribute('class', 'etoile');
    c.setAttribute('cx', Math.random() * 1600);
    c.setAttribute('cy', Math.random() * 520);
    c.setAttribute('r', 0.6 + Math.random() * 1.4);
    c.style.animationDelay = (Math.random() * 3).toFixed(2) + 's';
    g.appendChild(c);
  }
})();

let minuteurToast;
function toast(texte) {
  const t = $('#toast');
  t.textContent = texte;
  t.classList.add('visible');
  clearTimeout(minuteurToast);
  minuteurToast = setTimeout(() => t.classList.remove('visible'), 2500);
}

function envoyer(type, donnees = {}) {
  socket.emit('action', { type, ...donnees }, (r) => { if (r && r.erreur) toast(r.erreur); });
}

// ---------------------------------------------------------------------
// Écran d'accueil
// ---------------------------------------------------------------------
function montrerAccueil(message) {
  vueSalle = false;
  dernierHtml = {};
  document.body.className = 'nuit';
  const pseudoMemo = (session && session.nom) || '';
  $('#app').innerHTML = `
    <section class="panneau centre">
      <h1 class="logo">LOUP<br>GAROU</h1>
      <p class="sous">LA NUIT, LES LOUPS CHASSENT.<br>LE JOUR, ON VOTE.</p>
      <input id="pseudo" maxlength="16" placeholder="TON PSEUDO" value="${esc(pseudoMemo)}" autocomplete="off">
      ${codeUrl
        ? `<p class="gros-texte">Invitation pour la salle <b class="rouge">${esc(codeUrl)}</b></p>
           <button id="btn-rejoindre" class="gros">REJOINDRE LA PARTIE</button>`
        : `<button id="btn-creer" class="gros">CRÉER UNE PARTIE</button>
           <div class="ou">— OU —</div>
           <input id="code" maxlength="5" placeholder="CODE DE LA SALLE" autocomplete="off">
           <button id="btn-rejoindre">REJOINDRE</button>`}
      <p class="erreur">${esc(message || '')}</p>
    </section>`;

  const pseudo = () => $('#pseudo').value.trim();
  const apres = (nom) => (r) => {
    if (r.erreur) return montrerAccueil(r.erreur);
    session = { code: r.code, token: r.token, nom };
    sessionStorage.setItem('lg_session', JSON.stringify(session));
    codeUrl = r.code;
    history.replaceState(null, '', '/salle/' + r.code);   // le lien de la barre d'adresse devient le lien d'invitation
  };
  if ($('#btn-creer')) $('#btn-creer').onclick = () => socket.emit('creer', { nom: pseudo() }, apres(pseudo()));
  $('#btn-rejoindre').onclick = () => {
    const code = codeUrl || ($('#code') ? $('#code').value.trim().toUpperCase() : '');
    socket.emit('rejoindre', { code, nom: pseudo() }, apres(pseudo()));
  };
}

// ---------------------------------------------------------------------
// Connexion / reconnexion automatique
// ---------------------------------------------------------------------
socket.on('connect', () => {
  if (session) {
    // On rentre dans la salle avec son jeton secret (rafraîchissement, coupure réseau...)
    socket.emit('rejoindre', { code: session.code, nom: session.nom, token: session.token }, (r) => {
      if (r.erreur) {
        session = null;
        sessionStorage.removeItem('lg_session');
        montrerAccueil(r.erreur);
      }
    });
  } else if (!vueSalle) {
    montrerAccueil();
  }
});

socket.on('etat', (e) => {
  etat = e;
  if (!vueSalle) construireSalle();
  majSalle();
});

// ---------------------------------------------------------------------
// Structure de la salle (construite une seule fois)
// ---------------------------------------------------------------------
function construireSalle() {
  vueSalle = true;
  dernierHtml = {};
  dernierIdFil = 0;
  epoqueFil = -1;
  $('#app').innerHTML = `
    <header class="panneau barre">
      <span>SALLE <b id="code-salle"></b></span><span id="phase"></span><span id="compte"></span>
    </header>
    <div class="grille">
      <aside class="panneau" id="joueurs"></aside>
      <section class="panneau" id="action"></section>
      <section class="panneau chat">
        <div id="fil"></div>
        <form id="form-chat" autocomplete="off">
          <input id="chat-input" maxlength="300"><button>OK</button>
        </form>
        <div id="canal-info"></div>
      </section>
    </div>`;

  $('#form-chat').onsubmit = (ev) => {
    ev.preventDefault();
    const champ = $('#chat-input');
    if (!champ.value.trim()) return;
    envoyer('chat', { texte: champ.value });
    champ.value = '';
  };

  // Un seul écouteur pour tous les boutons du panneau d'action (délégation)
  $('#action').addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-act]');
    if (!b || b.disabled) return;
    const act = b.dataset.act;
    if (act === 'copier') {
      const champ = $('#lien');
      navigator.clipboard.writeText(champ.value)
        .then(() => toast('Lien copié !'))
        .catch(() => { champ.select(); document.execCommand('copy'); toast('Lien copié !'); });
    } else if (act === 'poison-ouvrir') {
      poisonOuvert = !poisonOuvert;
      majAction();
    } else if (act === 'sorciere') {
      poisonOuvert = false;
      envoyer('sorciere', { action: b.dataset.sous, cibleId: b.dataset.cible });
    } else {
      envoyer(act, { cibleId: b.dataset.cible });
    }
  });
  $('#action').addEventListener('change', (ev) => {
    if (ev.target.id === 'duree-debat') envoyer('configurer', { debat: Number(ev.target.value) });
  });
}

// ---------------------------------------------------------------------
// Mise à jour de l'affichage à chaque nouvel état
// ---------------------------------------------------------------------
function estJour(e) {
  if (e.phase === 'fin') return e.vainqueur === 'village';
  return ['debat', 'vote', 'tir_chasseur'].includes(e.phase);
}

function majSalle() {
  document.body.className = estJour(etat) ? 'jour' : 'nuit';
  $('#code-salle').textContent = etat.code;
  $('#phase').textContent = PHASES[etat.phase] + (etat.nuit && etat.phase !== 'lobby' ? ` (N${etat.nuit})` : '');
  finLocal = etat.reste != null ? Date.now() + etat.reste : null;
  majJoueurs();
  majAction();
  majFil();

  const champ = $('#chat-input');
  const infos = {
    public: 'Message à tout le village', loups: '🐺 Tu chuchotes à ta meute',
    cimetiere: '👻 Tu parles aux fantômes', null: 'Chut... tu dors.',
  };
  champ.disabled = !etat.canalChat;
  champ.placeholder = etat.canalChat ? 'Écris un message...' : 'Chat verrouillé';
  $('#canal-info').textContent = infos[etat.canalChat];
}

// Compte à rebours de la phase
setInterval(() => {
  const el = $('#compte');
  if (!el) return;
  if (finLocal) {
    const s = Math.max(0, Math.ceil((finLocal - Date.now()) / 1000));
    el.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  } else {
    el.textContent = '';
  }
}, 300);

function majJoueurs() {
  const html = `<h2>JOUEURS (${etat.joueurs.length})</h2>` + etat.joueurs.map((j) => {
    const tags = [];
    if (j.pid === etat.moi.pid) tags.push('toi');
    if (j.hote && etat.phase === 'lobby') tags.push('hôte');
    if (!j.connecte) tags.push('hors ligne');
    if (etat.phase === 'debat' && etat.ouvreur === j.pid) tags.push('ouvre');
    if (etat.phase === 'vote' && etat.action && etat.action.aVote && etat.action.aVote.includes(j.pid)) tags.push('a voté');
    const role = j.role ? `<span class="role ${j.role === LOUP ? 'rouge' : 'vert-txt'}">${esc(j.role)}</span>` : '';
    const mort = !j.vivant && etat.phase !== 'lobby';
    const score = etat.phase === 'lobby' || etat.phase === 'fin' ? ` <small>★${j.victoires}</small>` : '';
    return `<div class="joueur ${mort ? 'mort' : ''}">${mort ? '✝' : '♥'} <span>${esc(j.nom)}</span>${score}
      ${tags.map((t) => `<span class="tag">${t}</span>`).join('')} ${role}</div>`;
  }).join('');
  if (html !== dernierHtml.joueurs) { $('#joueurs').innerHTML = html; dernierHtml.joueurs = html; }
}

/** Boutons de choix d'une cible. */
function boutonsCibles(cibles, act, extra = '', classe = '', choisi = null) {
  return `<div class="cibles">` + cibles.map((pid) => {
    const j = etat.joueurs.find((x) => x.pid === pid);
    return `<button class="${classe} ${choisi === pid ? 'choisi' : ''}" data-act="${act}" ${extra} data-cible="${pid}">${esc(j.nom)}</button>`;
  }).join('') + `</div>`;
}

function majAction() {
  const e = etat;
  const a = e.action;
  let h = '';

  if (e.phase === 'lobby') {
    const lien = `${location.origin}/salle/${e.code}`;
    h += `<h2>INVITE TES AMIS</h2>
      <div class="lien"><input id="lien" readonly value="${esc(lien)}"><button data-act="copier">COPIER</button></div>
      <p class="gros-texte">Code de la salle : <b class="rouge">${esc(e.code)}</b><br>
      ${e.joueurs.length}/${e.maxJoueurs} joueurs (minimum ${e.minJoueurs})</p>
      <p>${esc(e.premierNom || '')} ouvrira le premier débat.</p>`;
    if (e.moi.hote) {
      h += `<label>DURÉE DU DÉBAT
        <select id="duree-debat">${[60, 120, 180, 300].map((s) =>
          `<option value="${s}" ${e.dureeDebat === s ? 'selected' : ''}>${s / 60} min</option>`).join('')}</select></label>
        <button class="gros vert" data-act="demarrer" ${e.joueurs.length < e.minJoueurs ? 'disabled' : ''}>LANCER LA PARTIE</button>`;
    } else {
      h += `<p class="gros-texte">En attente de l'hôte...</p>`;
    }
  } else if (e.phase === 'fin') {
    const gagnent = e.vainqueur === 'loups';
    const moiLoup = e.joueurs.find((j) => j.pid === e.moi.pid).role === LOUP;
    const victoire = gagnent === moiLoup;
    h += `<div class="vainqueur ${gagnent ? 'rouge' : 'vert-txt'}">${gagnent ? 'LES LOUPS-GAROUS GAGNENT !' : 'LE VILLAGE GAGNE !'}</div>
      <p class="gros-texte" style="text-align:center">${victoire ? '★ VICTOIRE ! ★' : '☠ DÉFAITE... ☠'}</p>
      <p class="gros-texte">Parties : ${e.scores.parties} · Village ${e.scores.village} · Loups ${e.scores.loups}</p>`;
    h += e.moi.hote
      ? `<button class="gros vert" data-act="rejouer">REJOUER</button>`
      : `<p class="gros-texte">L'hôte peut lancer une nouvelle partie.</p>`;
  } else {
    // En partie : rappel du rôle, puis action de la phase
    if (e.moi.role) {
      h += `<div class="carte-role">TON RÔLE : <b class="${e.moi.role === LOUP ? 'rouge' : 'vert-txt'}">${esc(e.moi.role)}</b>
        <small>${esc(DESCRIPTIONS[e.moi.role])}</small></div>`;
    }
    if (!e.moi.vivant && !(a && a.type === 'chasseur')) {
      h += `<p class="gros-texte">☠ Tu es éliminé(e). Observe la partie et discute au cimetière.</p>`;
    } else if (!a) {
      h += e.phase.startsWith('nuit')
        ? `<p class="gros-texte">🌙 Tu dors profondément... Ne fais pas de bruit, les loups rôdent.</p>`
        : `<p class="gros-texte">Le Chasseur va tirer...</p>`;
    } else if (a.type === 'loups') {
      const choisi = a.votes[e.moi.pid];
      const resume = Object.entries(a.votes).map(([w, c]) => {
        const nomW = e.joueurs.find((j) => j.pid === w).nom, nomC = e.joueurs.find((j) => j.pid === c).nom;
        return `${esc(nomW)} → ${esc(nomC)}`;
      }).join('<br>');
      h += `<h2>CHOISIS LA VICTIME</h2>${boutonsCibles(a.cibles, 'voteLoups', '', 'danger', choisi)}<p>${resume}</p>`;
    } else if (a.type === 'voyante') {
      h += `<h2>QUI SONDES-TU ?</h2>` + (a.fait
        ? `<p class="gros-texte">Regarde le journal : ta révélation est arrivée.</p>`
        : boutonsCibles(a.cibles, 'sonder'));
    } else if (a.type === 'sorciere') {
      const victime = a.victime ? e.joueurs.find((j) => j.pid === a.victime).nom : null;
      h += `<h2>TES POTIONS</h2><p class="gros-texte">${victime ? `Les loups ont attaqué <b class="rouge">${esc(victime)}</b>.` : 'Personne n\'a été attaqué.'}</p>`;
      if (a.fait) {
        h += `<p class="gros-texte">C'est fait. Tu te rendors...</p>`;
      } else {
        if (victime && a.potionVie) h += `<button class="vert" data-act="sorciere" data-sous="sauver">SAUVER ${esc(victime)}</button> `;
        if (a.potionMort) h += `<button class="danger" data-act="poison-ouvrir">${poisonOuvert ? 'ANNULER' : 'EMPOISONNER...'}</button> `;
        h += `<button data-act="sorciere" data-sous="rien">NE RIEN FAIRE</button>`;
        if (poisonOuvert) h += boutonsCibles(a.cibles, 'sorciere', 'data-sous="poison"', 'danger');
      }
    } else if (a.type === 'chasseur') {
      h += `<h2>DERNIER TIR !</h2>${boutonsCibles(a.cibles, 'tirer', '', 'danger')}`;
    } else if (a.type === 'debat') {
      h += `<h2>DÉBAT</h2><p class="gros-texte">Discutez dans le chat, accusez, défendez-vous !
        ${e.ouvreur ? `<br><b>${esc(e.joueurs.find((j) => j.pid === e.ouvreur).nom)}</b> ouvre le débat.` : ''}</p>
        <button class="gros" data-act="passerDebat" ${a.aPasse ? 'disabled' : ''}>PASSER AU VOTE (${a.nb}/${a.requis})</button>`;
    } else if (a.type === 'vote') {
      h += `<h2>QUI CONDAMNER ?</h2>${boutonsCibles(a.cibles, 'voter', '', 'danger', a.monVote)}
        <button data-act="voter" data-cible="" class="${a.monVote === null ? 'choisi' : ''}">VOTE BLANC</button>
        <p>Tu peux changer ton vote tant que tout le monde n'a pas voté.</p>`;
    }
  }
  if (h !== dernierHtml.action) { $('#action').innerHTML = h; dernierHtml.action = h; }
}

/** Journal + chat : on n'ajoute que les nouvelles lignes. */
function majFil() {
  const fil = $('#fil');
  if (etat.epoque !== epoqueFil) { fil.innerHTML = ''; dernierIdFil = 0; epoqueFil = etat.epoque; }
  const colle = fil.scrollHeight - fil.scrollTop - fil.clientHeight < 60;
  const prefixes = { loups: '[LOUPS] ', cimetiere: '[CIMETIÈRE] ', prive: '[SECRET] ' };
  for (const l of etat.fil) {
    if (l.id <= dernierIdFil) continue;
    const div = document.createElement('div');
    div.className = `msg ${l.type} ${l.canal}`;
    div.innerHTML = (prefixes[l.canal] || '') + (l.type === 'chat' ? `<b>${esc(l.auteur)}</b> : ${esc(l.texte)}` : esc(l.texte));
    fil.appendChild(div);
    dernierIdFil = l.id;
  }
  if (colle) fil.scrollTop = fil.scrollHeight;
}
