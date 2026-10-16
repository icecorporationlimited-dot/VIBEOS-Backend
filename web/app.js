(() => {
  'use strict';
  // VIBEOS web studio. Served by the backend at /app (same origin), so the login
  // cookies are first-party. Everything here talks to /web/api/*:
  //   GET  /web/api/me          who is signed in + Instagram connection
  //   GET  /web/api/state       photos, counts, autopilot settings, recent posts
  //   POST /web/api/media       upload photos (multipart) straight to the server buffer
  //   POST /web/api/media/delete
  //   POST /web/api/autopilot   switch on/off, daily limit, times, quiet hours
  const $ = id => document.getElementById(id);
  const escapeHTML = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  let me = {signed_in: false};
  let state = null;                 // last /web/api/state
  let currentFilter = 'all';
  let toastTimer, pollTimer;

  // ------------------------------------------------------------------ helpers
  function toast(msg) {
    const el = $('toast'); el.textContent = msg; el.classList.add('show');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('show'), 3500);
  }
  function sizeLabel(n) { return n < 1048576 ? Math.max(1, Math.round(n / 1024)) + ' KB' : (n / 1048576).toFixed(1) + ' MB'; }
  function dateLabel(v) { try { return new Date(v).toLocaleDateString(undefined, {month:'short', day:'numeric'}); } catch (_) { return ''; } }
  function dateTimeLabel(v) {
    if (!v) return '';
    try { return new Date(v).toLocaleString(undefined, {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'}); } catch (_) { return ''; }
  }
  function browserTZ() { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (_) { return ''; } }

  async function call(path, options = {}) {
    const headers = Object.assign({}, options.headers || {});
    if (options.method && options.method !== 'GET') {
      if (!me.csrf) await loadMe();
      if (me.csrf) headers['X-CSRF-Token'] = me.csrf;
    }
    const res = await fetch(path, Object.assign({credentials: 'same-origin', cache: 'no-store'}, options, {headers}));
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
    if (res.status === 401) { applyAccount({signed_in: false}); }
    if (!res.ok) throw new Error(data.error || 'Something went wrong (' + res.status + ')');
    return data;
  }
  const postJSON = (path, body) => call(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});

  function requireLogin() {
    if (me.signed_in) return true;
    toast('Please sign in first.');
    setTimeout(() => { location.href = '/login'; }, 700);
    return false;
  }

  // --------------------------------------------------------------- navigation
  function navigate(view) {
    document.querySelectorAll('.view').forEach(e => e.classList.toggle('active', e.id === 'view-' + view));
    document.querySelectorAll('[data-view]').forEach(e => e.classList.toggle('active', e.classList.contains('nav-item') && e.dataset.view === view));
    const names = {dashboard:'Overview', media:'Media library', notifications:'Activity', settings:'Settings'};
    $('crumbTitle').textContent = names[view] || 'Overview';
    $('sidebar').classList.remove('open');
    window.location.hash = view;
  }
  document.querySelectorAll('[data-view]').forEach(e => e.addEventListener('click', () => navigate(e.dataset.view)));
  $('mobileMenu').addEventListener('click', () => $('sidebar').classList.toggle('open'));
  $('tipButton').addEventListener('click', e => { e.currentTarget.closest('.help-card').style.display = 'none'; });

  // ------------------------------------------------------------------- upload
  function openPicker() { if (requireLogin()) $('filePicker').click(); }
  ['topUpload', 'emptyUpload', 'mediaUpload', 'emptyMediaUpload'].forEach(id => { const el = $(id); if (el) el.addEventListener('click', openPicker); });

  $('filePicker').addEventListener('change', async e => {
    const picked = [...(e.target.files || [])].filter(f => f.type.startsWith('image/'));
    e.target.value = '';
    if (!picked.length) return;
    let added = 0, dup = 0, failed = 0, lastError = '';
    // 10 photos per request keeps each upload small and quick to process.
    for (let i = 0; i < picked.length; i += 10) {
      const chunk = picked.slice(i, i + 10);
      const form = new FormData();
      chunk.forEach(f => form.append('files', f, f.name));
      toast('Uploading ' + Math.min(i + 10, picked.length) + ' of ' + picked.length + '…');
      try {
        const r = await call('/web/api/media', {method: 'POST', body: form});
        added += r.added; dup += r.duplicates; failed += r.invalid + r.too_large + r.quota_blocked;
      } catch (err) { failed += chunk.length; lastError = err.message; }
    }
    await loadState();
    const parts = [];
    if (added) parts.push(added + ' photo' + (added === 1 ? '' : 's') + ' added');
    if (dup) parts.push(dup + ' already in your buffer');
    if (failed) parts.push(failed + ' failed' + (lastError ? ' (' + lastError + ')' : ''));
    toast(parts.join(' · ') || 'Nothing was uploaded');
  });

  async function deleteMedia(id) {
    if (!confirm('Delete this photo? A post scheduled with it will be cancelled.')) return;
    try { await postJSON('/web/api/media/delete', {id}); await loadState(); toast('Photo deleted'); }
    catch (err) { toast(err.message); }
  }

  // ---------------------------------------------------------------- rendering
  function chip(m) { return '<span class="draft-chip chip-' + escapeHTML(m.state) + '">' + escapeHTML(m.label) + '</span>'; }

  function mediaCard(m) {
    const when = m.when ? ' · ' + escapeHTML(dateTimeLabel(m.when)) : '';
    return '<article class="media-card"><img class="media-thumb" loading="lazy" src="' + escapeHTML(m.thumb) + '" alt="' + escapeHTML(m.name) + '">' +
      '<div class="media-card-info"><span class="media-card-title" title="' + escapeHTML(m.name) + '">' + escapeHTML(m.name) + '</span>' +
      '<div class="media-card-meta"><span>' + sizeLabel(m.size) + '</span><span>' + dateLabel(m.created_at) + '</span></div>' +
      '<div class="media-card-meta">' + chip(m) + '<small>' + when + '</small></div>' +
      '<div class="media-card-actions"><button class="mini-action delete" data-delete-media="' + escapeHTML(m.id) + '">Delete</button></div></div></article>';
  }

  function renderMedia() {
    const all = (state && state.media) || [];
    let rows = all.slice();
    if (currentFilter !== 'all') rows = rows.filter(m => m.state === currentFilter);
    rows.sort((a, b) => $('mediaSort').value === 'oldest' ? new Date(a.created_at) - new Date(b.created_at) : new Date(b.created_at) - new Date(a.created_at));
    $('mediaGrid').innerHTML = rows.length ? rows.map(mediaCard).join('') :
      '<div class="empty-large"><div class="empty-icon big">▧</div><h3>' + (currentFilter === 'all' ? 'Your library is waiting for its first upload' : 'No photos here yet') +
      '</h3><p>Upload photos and VIBEOS takes it from there.</p><button class="primary-button" id="emptyMediaUpload2">＋ Upload photos</button></div>';
    const b = $('emptyMediaUpload2'); if (b) b.addEventListener('click', openPicker);
    document.querySelectorAll('[data-filter]').forEach(t => t.classList.toggle('active', t.dataset.filter === currentFilter));
    document.querySelectorAll('[data-delete-media]').forEach(btn => btn.addEventListener('click', () => deleteMedia(btn.dataset.deleteMedia)));
    $('allCount').textContent = all.length;
    $('mediaSummary').textContent = rows.length + ' item' + (rows.length === 1 ? '' : 's');
  }

  function renderRecent() {
    const host = $('recentMedia'), items = ((state && state.media) || []).slice(0, 4);
    if (!items.length) {
      host.className = 'recent-media empty-state'; host.removeAttribute('style');
      host.innerHTML = '<div class="empty-icon">▧</div><strong>Your next great post starts here</strong><p>Upload photos to start building your media library.</p><button class="soft-button" id="emptyUpload2">＋ Add photos</button>';
      $('emptyUpload2').addEventListener('click', openPicker); return;
    }
    host.className = 'recent-media'; host.style.display = 'grid'; host.style.gridTemplateColumns = 'repeat(4,minmax(0,1fr))'; host.style.gap = '8px';
    host.innerHTML = items.map(m => '<button title="' + escapeHTML(m.name) + '" data-recent="' + escapeHTML(m.id) + '" style="padding:0;border:0;background:none;min-width:0;text-align:left">' +
      '<img src="' + escapeHTML(m.thumb) + '" alt="' + escapeHTML(m.name) + '" style="display:block;width:100%;aspect-ratio:1;object-fit:cover;border-radius:10px">' +
      '<span style="display:block;font-size:11px;margin-top:4px;opacity:.75">' + escapeHTML(m.label) + '</span></button>').join('');
    host.querySelectorAll('[data-recent]').forEach(b => b.addEventListener('click', () => navigate('media')));
  }

  function fillSelect(sel, values, label) {
    if (sel.options.length) return;
    values.forEach(v => { const o = document.createElement('option'); o.value = v; o.textContent = label(v); sel.appendChild(o); });
  }
  const hourLabel = h => String(h).padStart(2, '0') + ':00';

  function renderAutopilot() {
    const ap = state && state.autopilot;
    fillSelect($('dailyLimit'), [1,2,3,4,5,6,7,8,9,10], n => n + (n === 1 ? ' post' : ' posts'));
    fillSelect($('dailyLimitDash'), [1,2,3,4,5,6,7,8,9,10], n => n + (n === 1 ? ' post' : ' posts'));
    fillSelect($('quietStart'), [...Array(24).keys()], hourLabel);
    fillSelect($('quietEnd'), [...Array(24).keys()], hourLabel);
    const on = !!(ap && ap.active);
    ['autopilotToggle', 'autopilotToggleDash'].forEach(id => { $(id).checked = on; $(id).disabled = !me.signed_in; });
    if (ap) {
      $('dailyLimit').value = $('dailyLimitDash').value = ap.daily_limit;
      $('quietStart').value = ap.quiet_start; $('quietEnd').value = ap.quiet_end;
      if (document.activeElement !== $('slotsInput')) $('slotsInput').value = ap.slots.join(', ');
      $('tzText').textContent = 'Using ' + (ap.timezone || browserTZ() || 'your default zone') + (ap.timezone ? '' : ' (saved when you save settings)');
    }
    $('autoStat').textContent = on ? 'On' : 'Off';
    $('autoFoot').textContent = ap ? 'Up to ' + ap.daily_limit + ' post' + (ap.daily_limit === 1 ? '' : 's') + ' a day' : 'Sign in to use Autopilot';

    const c = (state && state.counts) || {};
    const lines = [];
    if (!me.signed_in) lines.push(['◷', 'Sign in to use Autopilot', '']);
    else {
      lines.push([on ? '🟢' : '⚪', on ? 'Autopilot is ON' : 'Autopilot is OFF', on ? 'New photos are edited, captioned and scheduled for you.' : (c.waiting ? c.waiting + ' photo(s) waiting in your buffer.' : 'Turn it on and upload photos.')]);
      if (on || c.ready || c.scheduled) lines.push(['▧', (c.processing || 0) + ' preparing · ' + (c.ready || 0) + ' ready', (c.scheduled || 0) + ' scheduled · ' + (c.posted || 0) + ' posted']);
      if (ap && ap.next_post_at) lines.push(['◷', 'Next post', dateTimeLabel(ap.next_post_at)]);
    }
    $('autopilotStatus').innerHTML = lines.map(l => '<div class="empty-inline"><span>' + l[0] + '</span><div><strong>' + escapeHTML(l[1]) + '</strong>' + (l[2] ? '<small>' + escapeHTML(l[2]) + '</small>' : '') + '</div></div>').join('');

    let note = 'Add photos and VIBEOS handles edits, captions and timing.';
    if (me.signed_in && !me.connected) note = 'Connect your Instagram account so Autopilot can post.';
    else if (state && !state.posting_engine) note = 'Photos are prepared now. Posting starts when the hosted posting engine is enabled.';
    $('autopilotNote').textContent = note;
    $('engineNotice').hidden = !(me.signed_in && state && !state.posting_engine);
  }

  function renderCounts() {
    const c = (state && state.counts) || {};
    const total = ((state && state.media) || []).length;
    $('mediaCount').textContent = total; $('statMedia').textContent = total;
    $('statQueue').textContent = c.scheduled || 0; $('statPosted').textContent = c.posted || 0;
    $('statQueueFoot').textContent = c.ready ? c.ready + ' ready for the next slot' : 'Autopilot picks the times';
  }

  function renderActivity() {
    const rows = (state && state.recent) || [];
    const icon = {published:'✅', queued:'🕒', retry:'🔁', failed:'❌', held:'⏸', cancelled:'🚫', running:'⏳', needs_reauth:'🔑', needs_attention:'⚠️'};
    const text = {published:'Posted', queued:'Scheduled', retry:'Retrying', failed:'Failed', held:'On hold', cancelled:'Cancelled', running:'Posting now', needs_reauth:'Reconnect Instagram', needs_attention:'Needs attention'};
    $('notificationList').innerHTML = rows.length ? rows.map(r => '<article class="notification-item"><div class="notification-icon">' + (icon[r.status] || '✦') + '</div><div><strong>' +
      escapeHTML((text[r.status] || r.status) + ' · ' + r.format) + '</strong><p>' + escapeHTML(r.error || (r.permalink ? 'Live on Instagram' : 'Handled by Autopilot')) +
      '</p><time>' + escapeHTML(dateTimeLabel(r.at)) + '</time></div></article>').join('') :
      '<div class="empty-large"><div class="empty-icon big">♧</div><h3>No activity yet</h3><p>Posts and scheduling events will show up here.</p></div>';
  }

  function renderAll() { renderCounts(); renderMedia(); renderRecent(); renderAutopilot(); renderActivity(); schedulePoll(); }

  // ------------------------------------------------------------------ account
  function postConnect(csrf) {
    const f = document.createElement('form'); f.method = 'POST'; f.action = '/web/instagram/connect';
    const i = document.createElement('input'); i.type = 'hidden'; i.name = 'csrf_token'; i.value = csrf;
    f.appendChild(i); document.body.appendChild(f); f.submit();
  }
  function applyAccount(m) {
    me = m || {signed_in: false};
    const btns = [$('connectButton'), $('settingsConnect')];
    const status = $('connectionStatus'), copy = $('connectCopy');
    const set = (label, href, onclick, disabled) => btns.forEach(b => { if (!b) return;
      b.textContent = label; b.setAttribute('href', href || '#'); b.onclick = onclick || null;
      b.style.pointerEvents = disabled ? 'none' : ''; b.style.opacity = disabled ? '.7' : ''; });
    if (!me.signed_in) {
      $('userName').textContent = 'Guest'; $('userSub').textContent = 'Not signed in'; $('userAvatar').textContent = '?';
      status.innerHTML = '<i></i> Signed out'; copy.textContent = 'Sign in, then connect your own Instagram professional account.';
      $('igStatusText').textContent = 'Sign in to connect Instagram.';
      set('Sign in', '/login'); return;
    }
    $('userName').textContent = me.name || 'Creator';
    $('userAvatar').textContent = (me.name || 'C').trim().charAt(0).toUpperCase() || 'C';
    if (me.connected) {
      $('userSub').textContent = '@' + me.username;
      status.innerHTML = '<i></i> Instagram connected: @' + escapeHTML(me.username);
      copy.textContent = 'Your Instagram account @' + me.username + ' is connected to VIBEOS.';
      $('igStatusText').textContent = 'Connected as @' + me.username;
      set('Connected ✓', '#', e => e.preventDefault(), true); return;
    }
    $('userSub').textContent = 'Instagram not connected';
    status.innerHTML = '<i></i> ' + (me.account_status === 'needs_reauth' ? 'Reconnect needed' : 'Not connected');
    copy.textContent = 'Connect your own Instagram professional account to VIBEOS.';
    $('igStatusText').textContent = me.account_status === 'needs_reauth' ? 'Login expired. Reconnect to keep posting.' : 'No Instagram account connected yet.';
    set(me.account_status === 'needs_reauth' ? 'Reconnect Instagram' : 'Connect Instagram', '#',
      e => { e.preventDefault(); if (me.csrf) postConnect(me.csrf); else location.href = '/login'; }, false);
  }
  async function loadMe() {
    try { const r = await fetch('/web/api/me', {credentials: 'same-origin', cache: 'no-store'}); applyAccount(r.ok ? await r.json() : {signed_in: false}); }
    catch (_) { applyAccount({signed_in: false}); }
  }

  // -------------------------------------------------------------------- state
  async function loadState(quiet) {
    if (!me.signed_in) { state = null; renderAll(); return; }
    try { state = await call('/web/api/state'); renderAll(); }
    catch (err) { if (!quiet) toast(err.message); }
  }
  // While photos are being prepared, refresh every few seconds.
  function schedulePoll() {
    clearTimeout(pollTimer);
    const busy = state && state.counts && state.counts.processing > 0;
    if (busy) pollTimer = setTimeout(() => { if (!document.hidden) loadState(true); else schedulePoll(); }, 4000);
  }

  // ---------------------------------------------------------------- autopilot
  async function saveAutopilot(body, okMsg) {
    if (!requireLogin()) { renderAutopilot(); return; }
    try { state = await postJSON('/web/api/autopilot', body); renderAll(); if (okMsg) toast(okMsg); }
    catch (err) { toast(err.message); renderAutopilot(); }
  }
  ['autopilotToggle', 'autopilotToggleDash'].forEach(id => $(id).addEventListener('change', e => {
    const on = e.target.checked;
    const body = {enabled: on};
    if (on && state && !state.autopilot.timezone && browserTZ()) body.timezone = browserTZ();
    saveAutopilot(body, on ? 'Autopilot is on' : 'Autopilot is off');
  }));
  ['dailyLimit', 'dailyLimitDash'].forEach(id => $(id).addEventListener('change', e => saveAutopilot({daily_limit: Number(e.target.value)}, 'Daily limit saved')));
  $('saveAutopilot').addEventListener('click', () => {
    const slots = $('slotsInput').value.split(/[\s,]+/).filter(Boolean);
    const body = {slots, quiet_start: Number($('quietStart').value), quiet_end: Number($('quietEnd').value)};
    if (browserTZ()) body.timezone = browserTZ();
    saveAutopilot(body, 'Autopilot settings saved');
  });

  // ------------------------------------------------------------------- events
  document.querySelectorAll('[data-filter]').forEach(b => b.addEventListener('click', () => { currentFilter = b.dataset.filter; renderMedia(); }));
  $('mediaSort').addEventListener('change', renderMedia);

  function initDate() {
    const n = new Date();
    $('dateText').textContent = n.toLocaleDateString(undefined, {weekday:'short', month:'short', day:'numeric'});
    $('todayLabel').textContent = n.toLocaleDateString(undefined, {month:'short', day:'numeric', year:'numeric'}).toUpperCase();
  }

  async function boot() {
    initDate();
    await loadMe();
    await loadState();
    const hash = (location.hash || '#dashboard').slice(1);
    if (['dashboard', 'media', 'notifications', 'settings'].includes(hash)) navigate(hash);
  }
  boot();
})();
