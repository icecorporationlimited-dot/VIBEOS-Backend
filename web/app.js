(() => {
  'use strict';
  // VIBEOS web studio. Served by the backend at /app (same origin), so the login
  // cookies are first-party. Everything here talks to /web/api/*:
  //   GET  /web/api/me          who is signed in + Instagram connection
  //   GET  /web/api/state       photos, counts, autopilot settings, recent posts
  //   POST /web/api/media       upload photos (multipart) straight to the server buffer
  //   POST /web/api/media/delete
//   POST /web/api/media/caption  save an edited caption (ready photos)
//   POST /web/api/media/post     post one ready photo right now
//   POST /web/api/media/skip     skip one photo (cancels its pending job)
//   POST /web/api/story/preview  render a story template around one photo
  //   POST /web/api/autopilot   switch on/off, daily limit, times, quiet hours, reels, trend stories, comment replies
  const $ = id => document.getElementById(id);
  const escapeHTML = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const ic = n => '<svg class="ic" aria-hidden="true"><use href="#i-' + n + '"/></svg>';

  let me = {signed_in: false};
  let state = null;                 // last /web/api/state
  let currentFilter = 'all';
  let toastTimer, pollTimer;

  // ------------------------------------------------------------------ helpers
  function toast(msg) {
    try { if (navigator.vibrate) navigator.vibrate(12); } catch (e) {}   // light haptic on every notice
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
  function firstName() { return ((me.name || '').trim().split(/\s+/)[0]) || ''; }

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
    toast('Sign in first.');
    setTimeout(() => { location.href = '/login'; }, 700);
    return false;
  }

  // --------------------------------------------------------------- navigation
  function navigate(view) {
    document.querySelectorAll('.view').forEach(e => e.classList.toggle('active', e.id === 'view-' + view));
    document.querySelectorAll('.nav-item[data-view], .tab-item[data-view]').forEach(e => {
      const on = e.dataset.view === view;
      e.classList.toggle('active', on);
      if (on) e.setAttribute('aria-current', 'page'); else e.removeAttribute('aria-current');
    });
    window.location.hash = view;
    window.scrollTo(0, 0);
    if (view === 'occasions') loadOccasions();
  }
  document.querySelectorAll('[data-view]').forEach(e => e.addEventListener('click', () => navigate(e.dataset.view)));
  $('tipButton').addEventListener('click', e => { e.currentTarget.closest('.tip').style.display = 'none'; });

  // ------------------------------------------------------------------- upload
  function openPicker() { if (requireLogin()) $('filePicker').click(); }
  ['sideAdd', 'tabAdd', 'heroAdd', 'emptyUpload', 'mediaUpload', 'emptyMediaUpload'].forEach(id => { const el = $(id); if (el) el.addEventListener('click', openPicker); });

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
    toast(parts.join(', ') || 'Nothing was uploaded');
  });

  async function deleteMedia(id) {
    if (!confirm('Delete this photo? A post scheduled with it will be cancelled.')) return false;
    try { await postJSON('/web/api/media/delete', {id}); await loadState(); toast('Photo deleted'); return true; }
    catch (err) { toast(err.message); return false; }
  }

  // ---------------------------------------------------------------- rendering
  function chip(m) { return '<span class="chip chip-' + escapeHTML(m.state) + '">' + escapeHTML(m.label) + '</span>'; }

  function mediaCard(m) {
    const when = m.when ? dateTimeLabel(m.when) : '';
    const title = m.name + ', ' + sizeLabel(m.size) + ', added ' + dateLabel(m.created_at);
    return '<article class="mtile" data-media="' + escapeHTML(m.id) + '" title="' + escapeHTML(title) + '"><img loading="lazy" src="' + escapeHTML(m.thumb) + '" alt="' + escapeHTML(m.name) + '">' + (m.badge ? '<span class="score-badge tone-' + escapeHTML(m.badge.tone) + '">' + escapeHTML(m.badge.label) + '</span>' : '') +
      '<button class="del" data-delete-media="' + escapeHTML(m.id) + '" aria-label="Delete photo">' + ic('trash') + '</button>' +
      '<div class="mtile-info">' + chip(m) + (when ? '<time>' + escapeHTML(when) + '</time>' : '') + '</div></article>';
  }

  function renderMedia() {
    const all = (state && state.media) || [];
    let rows = all.slice();
    if (currentFilter !== 'all') rows = rows.filter(m => m.state === currentFilter);
    rows.sort((a, b) => $('mediaSort').value === 'oldest' ? new Date(a.created_at) - new Date(b.created_at) : new Date(b.created_at) - new Date(a.created_at));
    $('mediaGrid').innerHTML = rows.length ? rows.map(mediaCard).join('') :
      '<div class="empty big"><span class="empty-ic">' + ic('image') + '</span><h3>' + (currentFilter === 'all' ? 'No photos yet' : 'Nothing in this tab') +
      '</h3><p>' + (currentFilter === 'all' ? 'Add some and autopilot takes it from there.' : 'Photos show up here as they move along.') + '</p><button class="btn" id="emptyMediaUpload2">Add photos</button></div>';
    const b = $('emptyMediaUpload2'); if (b) b.addEventListener('click', openPicker);
    document.querySelectorAll('[data-filter]').forEach(t => t.classList.toggle('active', t.dataset.filter === currentFilter));
    document.querySelectorAll('[data-delete-media]').forEach(btn => btn.addEventListener('click', () => deleteMedia(btn.dataset.deleteMedia)));
    document.querySelectorAll('.mtile[data-media]').forEach(el => el.addEventListener('click', e => { if (e.target.closest('.del')) return; openSheet(el.dataset.media); }));
    $('allCount').textContent = all.length;
    $('mediaSummary').textContent = rows.length + ' item' + (rows.length === 1 ? '' : 's');
  }

  function renderRecent() {
    const host = $('recentMedia'), items = ((state && state.media) || []).slice(0, 6);
    if (!items.length) {
      host.className = 'empty';
      host.innerHTML = '<span class="empty-ic">' + ic('image') + '</span><strong>Nothing here yet</strong><p>Add a few photos and they’ll show up here.</p><button class="btn ghost sm" id="emptyUpload2">Add photos</button>';
      $('emptyUpload2').addEventListener('click', openPicker); return;
    }
    host.className = 'tiles';
    host.innerHTML = items.map(m => '<button class="tile" title="' + escapeHTML(m.name) + '" data-recent="' + escapeHTML(m.id) + '">' +
      '<img src="' + escapeHTML(m.thumb) + '" alt="' + escapeHTML(m.name) + '" loading="lazy">' + chip(m) + '</button>').join('');
    host.querySelectorAll('[data-recent]').forEach(b => b.addEventListener('click', () => openSheet(b.dataset.recent)));
  }

  function fillSelect(sel, values, label) {
    if (sel.options.length) return;
    values.forEach(v => { const o = document.createElement('option'); o.value = v; o.textContent = label(v); sel.appendChild(o); });
  }
  const hourLabel = h => String(h).padStart(2, '0') + ':00';

  function renderHero() {
    const t = $('heroTitle'), s = $('heroSub');
    const c = (state && state.counts) || {}, ap = state && state.autopilot;
    if (!me.signed_in) {
      t.textContent = 'Drop the pics. We’ll handle the posting.';
      s.textContent = 'Sign in, link your Instagram, and autopilot does the rest.'; return;
    }
    t.textContent = firstName() ? 'Hey, ' + firstName() + '.' : 'Hey you.';
    if (!me.connected) s.textContent = 'Link your Instagram and you’re set.';
    else if (ap && ap.active) {
      const n = c.scheduled || 0;
      s.textContent = n ? 'Autopilot’s on. ' + n + ' post' + (n === 1 ? ' is' : 's are') + ' lined up.' : 'Autopilot’s on. Add photos and it’ll line up posts.';
    } else s.textContent = 'Autopilot’s off. Your photos wait in the library until you switch it on.';
  }

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
    if (ap) {
      $('reelsToggle').checked = !!ap.reels; $('trendToggle').checked = !!ap.trend_stories;
      $('repliesToggle').checked = !!ap.auto_replies; $('commentsToggle').checked = !!ap.auto_comments;
    }
    ['reelsToggle', 'trendToggle', 'repliesToggle', 'commentsToggle'].forEach(id => { $(id).disabled = !me.signed_in; });
    document.querySelectorAll('.vibe-chip').forEach(c => {
      c.classList.toggle('active', !!ap && c.dataset.vibe === (ap.vibe || 'auto'));
      c.disabled = !me.signed_in;
    });
    $('autoStat').textContent = on ? 'On' : 'Off';
    $('autoFoot').textContent = ap ? 'Up to ' + ap.daily_limit + ' post' + (ap.daily_limit === 1 ? '' : 's') + ' a day' : 'Sign in to use autopilot';

    const c = (state && state.counts) || {};
    const lines = [];
    if (!me.signed_in) lines.push(['clock', 'Sign in to use autopilot', '']);
    else {
      lines.push([on ? 'bolt' : 'pause', on ? 'Autopilot is on' : 'Autopilot is off',
        on ? 'New photos get edited, captioned and scheduled.' : (c.waiting ? c.waiting + (c.waiting === 1 ? ' photo is' : ' photos are') + ' waiting in your buffer.' : 'Turn it on, then add photos.')]);
      if (on || c.ready || c.scheduled) lines.push(['image', (c.processing || 0) + ' preparing, ' + (c.ready || 0) + ' ready', (c.scheduled || 0) + ' scheduled, ' + (c.posted || 0) + ' posted']);
      if (ap && ap.next_post_at) lines.push(['clock', 'Next post', dateTimeLabel(ap.next_post_at)]);
    }
    $('autopilotStatus').innerHTML = lines.map(l => '<div class="line"><span class="line-ic">' + ic(l[0]) + '</span><div><strong>' + escapeHTML(l[1]) + '</strong>' + (l[2] ? '<small>' + escapeHTML(l[2]) + '</small>' : '') + '</div></div>').join('');

    let note = 'Add photos and VIBEOS handles edits, captions and timing.';
    if (me.signed_in && !me.connected) note = 'Connect your Instagram account so autopilot can post.';
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
    document.body.classList.toggle('busy', (c.processing || 0) > 0);   // story ring spins while photos are prepared
  }

  function renderActivity() {
    const rows = (state && state.recent) || [];
    const look = {
      published: ['check', 'ok', 'Posted'], queued: ['clock', 'info', 'Scheduled'], retry: ['retry', 'warn', 'Retrying'],
      failed: ['x', 'bad', 'Failed'], held: ['pause', 'mute', 'On hold'], cancelled: ['ban', 'mute', 'Cancelled'],
      running: ['loader', 'info', 'Posting now'], needs_reauth: ['key', 'warn', 'Reconnect Instagram'], needs_attention: ['alert', 'warn', 'Needs attention']
    };
    $('notificationList').innerHTML = rows.length ? rows.map(r => {
      const l = look[r.status] || ['spark', 'mute', r.status];
      const live = r.permalink && /^https:\/\//.test(r.permalink);
      const detail = r.error ? escapeHTML(r.error) : live ? '<a href="' + escapeHTML(r.permalink) + '" target="_blank" rel="noopener">See it on Instagram</a>' : 'Autopilot handled it';
      return '<article class="act"><span class="act-ic tone-' + l[1] + '">' + ic(l[0] === 'spark' ? 'bolt' : l[0]) + '</span><div class="act-main"><strong>' + escapeHTML(l[2]) +
        ' <span class="fmt">' + escapeHTML(r.format) + '</span></strong><p>' + detail + '</p></div><time>' + escapeHTML(dateTimeLabel(r.at)) + '</time></article>';
    }).join('') :
      '<div class="empty big"><span class="empty-ic">' + ic('heart') + '</span><h3>Quiet for now</h3><p>Posts and schedule updates land here.</p></div>';
  }

  function renderAll() { renderCounts(); renderMedia(); renderRecent(); renderAutopilot(); renderActivity(); renderHero(); renderStreak(); schedulePoll(); }

  // ------------------------------------------------------------------ account
  function postConnect(csrf) {
    const f = document.createElement('form'); f.method = 'POST'; f.action = '/web/instagram/connect';
    const i = document.createElement('input'); i.type = 'hidden'; i.name = 'csrf_token'; i.value = csrf;
    f.appendChild(i); document.body.appendChild(f); f.submit();
  }
  function setAvatar(letter) { ['userAvatar', 'heroAvatar', 'topAvatar'].forEach(id => { $(id).textContent = letter; }); }
  function setChip(text, cls) { const c = $('igStatusChip'); c.textContent = text; c.className = 'chip ' + cls; }
  function applyAccount(m) {
    me = m || {signed_in: false};
    const btns = [$('connectButton'), $('settingsConnect')];
    const status = $('connectionStatus'), copy = $('connectCopy'), card = $('connectCard');
    const set = (label, href, onclick, disabled) => btns.forEach(b => { if (!b) return;
      b.textContent = label; b.setAttribute('href', href || '#'); b.onclick = onclick || null;
      b.style.pointerEvents = disabled ? 'none' : ''; b.style.opacity = disabled ? '.6' : ''; });
    document.body.classList.toggle('ig-on', !!(me.signed_in && me.connected));
    card.dataset.state = (me.signed_in && me.connected) ? 'on' : 'off';
    if (!me.signed_in) {
      $('userName').textContent = 'Guest'; $('userSub').textContent = 'Not signed in'; setAvatar('?');
      $('connectTitle').textContent = 'Link your Instagram';
      status.innerHTML = '<i></i>Signed out'; copy.textContent = 'Sign in, then connect your own Instagram professional account.';
      $('igStatusText').textContent = 'Sign in to connect Instagram.'; setChip('Signed out', 'chip-waiting');
      set('Sign in', '/login'); renderHero(); return;
    }
    $('userName').textContent = me.name || 'Creator';
    setAvatar((me.name || 'C').trim().charAt(0).toUpperCase() || 'C');
    if (me.connected) {
      $('userSub').textContent = '@' + me.username;
      $('connectTitle').textContent = 'Instagram linked';
      status.innerHTML = '<i></i>Connected as @' + escapeHTML(me.username);
      copy.textContent = 'Your account @' + me.username + ' is connected to VIBEOS.';
      $('igStatusText').textContent = 'Connected as @' + me.username; setChip('Connected', 'chip-ready');
      set('Connected', '#', e => e.preventDefault(), true); renderHero(); return;
    }
    $('userSub').textContent = 'Instagram not linked';
    $('connectTitle').textContent = me.account_status === 'needs_reauth' ? 'Reconnect your Instagram' : 'Link your Instagram';
    status.innerHTML = '<i></i>' + (me.account_status === 'needs_reauth' ? 'Reconnect needed' : 'Not connected');
    copy.textContent = 'Connect your own Instagram professional account to VIBEOS.';
    $('igStatusText').textContent = me.account_status === 'needs_reauth' ? 'Login expired. Reconnect to keep posting.' : 'No Instagram account connected yet.';
    setChip(me.account_status === 'needs_reauth' ? 'Reconnect' : 'Not linked', me.account_status === 'needs_reauth' ? 'chip-scheduled' : 'chip-waiting');
    set(me.account_status === 'needs_reauth' ? 'Reconnect Instagram' : 'Connect Instagram', '#',
      e => { e.preventDefault(); if (me.csrf) postConnect(me.csrf); else location.href = '/login'; }, false);
    renderHero();
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
    saveAutopilot(body, on ? 'Autopilot on. You’re set.' : 'Autopilot off. Photos will wait for you.');
  }));
  ['dailyLimit', 'dailyLimitDash'].forEach(id => $(id).addEventListener('change', e => saveAutopilot({daily_limit: Number(e.target.value)}, 'Daily limit saved')));
  $('reelsToggle').addEventListener('change', e => saveAutopilot(
    {formats: e.target.checked ? ['photo', 'story', 'carousel', 'reel'] : ['photo', 'story', 'carousel']},
    e.target.checked ? 'Reels on' : 'Reels off'));
  $('trendToggle').addEventListener('change', e => saveAutopilot(
    {trend_stories: e.target.checked ? 'auto' : 'off'}, e.target.checked ? 'Trend Stories on' : 'Trend Stories off'));
  $('repliesToggle').addEventListener('change', e => saveAutopilot(
    {auto_replies: e.target.checked}, e.target.checked ? 'Comment replies on' : 'Comment replies off'));
  $('commentsToggle').addEventListener('change', e => saveAutopilot(
    {auto_comments: e.target.checked}, e.target.checked ? 'First comment on' : 'First comment off'));
  $('saveAutopilot').addEventListener('click', () => {
    const slots = $('slotsInput').value.split(/[\s,]+/).filter(Boolean);
    const body = {slots, quiet_start: Number($('quietStart').value), quiet_end: Number($('quietEnd').value)};
    if (browserTZ()) body.timezone = browserTZ();
    saveAutopilot(body, 'Settings saved');
  });

  // ---------------------------------------------------------------- occasions
  let occ = null;
  const OCC_CATS = {festival: 'Festivals', national: 'National days', social: 'Social days', seasonal: 'Seasonal', awareness: 'Awareness days'};
  const OCC_STATE = {
    planned: ['Planned', 'chip-ready'],
    auto_greeting: ['Greeting with your best pic', 'chip-processing'],
    quiet: ['Quiet day', 'chip-waiting'],
    watching: ['Waiting for a pic', 'chip-waiting']
  };
  const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  function occWhen(days) { return days === 0 ? 'Today' : days === 1 ? 'Tomorrow' : 'In ' + days + ' days'; }
  function renderOccasions() {
    if (!occ) return;
    if (!occ.available) { $('occList').innerHTML = '<div class="empty"><strong>Not available yet</strong><p>The context engine isn’t switched on for this server.</p></div>'; return; }
    const p = occ.prefs;
    $('occEnabled').checked = !!p.enabled;
    $('occGreeting').checked = !!p.auto_greeting;
    $('occPriority').value = p.min_priority >= 85 ? '85' : p.min_priority >= 70 ? '70' : '50';
    $('occSolemn').value = p.solemn;
    $('occCats').innerHTML = Object.keys(OCC_CATS).map(k =>
      '<label class="occ-cat"><input type="checkbox" data-cat="' + k + '"' + (p.categories.includes(k) ? ' checked' : '') + '>' + OCC_CATS[k] + '</label>').join('');
    $('occCats').querySelectorAll('[data-cat]').forEach(b => b.addEventListener('change', () => {
      const cats = Array.from($('occCats').querySelectorAll('[data-cat]')).filter(x => x.checked).map(x => x.dataset.cat);
      saveOccasions({categories: cats});
    }));
    const cal = occ.calendar || {};
    $('occCalendarWarn').hidden = cal.ok !== false;
    if (cal.ok === false) $('occCalendarWarnText').textContent = cal.covered_until
      ? 'Lunar festival dates (Diwali, Holi, Eid) are covered until ' + cal.covered_until + '. Ask the admin to extend event_calendar.json.'
      : 'No lunar festival dates are loaded. Fixed-date days still work.';
    $('occList').innerHTML = occ.occasions.length ? occ.occasions.map(o => {
      const s = OCC_STATE[o.state] || OCC_STATE.watching;
      let mon = '', day = '';
      try { const dt = new Date(o.date + 'T00:00:00'); mon = dt.toLocaleDateString(undefined, {month: 'short'}); day = dt.getDate(); } catch (_) {}
      return '<div class="occ-row" title="' + escapeHTML(o.tone) + ' tone"><div class="cal"><span>' + escapeHTML(mon) + '</span><b>' + escapeHTML(day) + '</b></div>' +
        '<div class="occ-main"><strong>' + escapeHTML(o.name) + '</strong><small>' + occWhen(o.days_left) + ', ' + escapeHTML(o.category) + '</small></div>' +
        '<span class="chip ' + s[1] + '">' + escapeHTML(s[0]) + '</span></div>';
    }).join('') : '<div class="empty"><strong>Nothing coming up</strong><p>No occasions match your rules in the next 45 days.</p></div>';
    $('occPersonal').innerHTML = p.personal.length ? p.personal.map((x, i) =>
      '<div class="occ-row"><div class="occ-main"><strong>' + escapeHTML(x.name) + '</strong><small>' + MONTHS[x.month - 1] + ' ' + x.day + ', every year</small></div>' +
      '<button class="mini-action" data-occdel="' + i + '">Remove</button></div>').join('') : '';
    $('occPersonal').querySelectorAll('[data-occdel]').forEach(b => b.addEventListener('click', () =>
      saveOccasions({personal: p.personal.filter((_, i) => i !== Number(b.dataset.occdel))})));
  }
  async function loadOccasions() {
    if (!me.signed_in) return;
    try { occ = await call('/web/api/occasions'); renderOccasions(); }
    catch (err) { toast(err.message); }
  }
  async function saveOccasions(body) {
    if (!requireLogin()) { renderOccasions(); return; }
    try { occ = await postJSON('/web/api/occasions', body); renderOccasions(); toast('Occasion rules saved'); }
    catch (err) { toast(err.message); renderOccasions(); }
  }
  $('occEnabled').addEventListener('change', e => saveOccasions({enabled: e.target.checked}));
  $('occGreeting').addEventListener('change', e => saveOccasions({auto_greeting: e.target.checked}));
  $('occPriority').addEventListener('change', e => saveOccasions({min_priority: Number(e.target.value)}));
  $('occSolemn').addEventListener('change', e => saveOccasions({solemn: e.target.value}));
  $('occMonth').innerHTML = MONTHS.map((m, i) => '<option value="' + (i + 1) + '">' + m + '</option>').join('');
  $('occDay').innerHTML = Array.from({length: 31}, (_, i) => '<option value="' + (i + 1) + '">' + (i + 1) + '</option>').join('');
  $('occAdd').addEventListener('click', () => {
    const name = $('occName').value.trim();
    if (!name) { toast('Give it a name first.'); return; }
    const list = ((occ && occ.prefs && occ.prefs.personal) || []).concat([{name, month: Number($('occMonth').value), day: Number($('occDay').value)}]);
    $('occName').value = '';
    saveOccasions({personal: list});
  });

  // -------------------------------------------------------- photo detail sheet
  let sheetMedia = null;

  function openSheet(id) {
    const m = ((state && state.media) || []).find(x => x.id === id);
    if (!m) return;
    sheetMedia = m;
    $('sheetImage').src = m.thumb;
    $('sheetChip').innerHTML = chip(m);
    $('sheetWhen').textContent = m.when ? dateTimeLabel(m.when) : 'Time picked by autopilot';
    const cap = $('sheetCaption');
    cap.value = m.caption || '';
    cap.disabled = !(m.state === 'ready' || m.state === 'scheduled');
    $('sheetTags').textContent = (m.hashtags || []).map(h => '#' + String(h).replace(/^#+/, '')).join(' ');
    $('sheetSaveCaption').hidden = true;
    const canPost = !!(state && state.posting_engine) && (m.state === 'ready' || m.state === 'scheduled');
    $('sheetPost').style.display = canPost ? '' : 'none';
    $('sheetSkip').style.display = (m.state === 'posted' || m.state === 'skipped') ? 'none' : '';
    $('sheetWrap').hidden = false;
    requestAnimationFrame(() => $('sheetWrap').classList.add('open'));
  }

  function closeSheet() {
    $('sheetWrap').classList.remove('open');
    setTimeout(() => { $('sheetWrap').hidden = true; $('sheetImage').src = ''; }, 250);
    sheetMedia = null;
  }

  async function sheetAction(path, okMsg) {
    if (!sheetMedia) return;
    const id = sheetMedia.id;
    try {
      state = await postJSON(path, {id});
      if (path === '/web/api/media/post') confetti();
      renderAll(); toast(okMsg); closeSheet();
    } catch (err) { toast(err.message); }
  }

  $('sheetOverlay').addEventListener('click', closeSheet);
  $('sheetClose').addEventListener('click', closeSheet);
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('sheetWrap').hidden) closeSheet(); });
  $('sheetCaption').addEventListener('input', () => { $('sheetSaveCaption').hidden = false; });
  $('sheetSaveCaption').addEventListener('click', async () => {
    if (!sheetMedia) return;
    try {
      state = await postJSON('/web/api/media/caption', {id: sheetMedia.id, caption: $('sheetCaption').value});
      renderAll(); $('sheetSaveCaption').hidden = true; toast('Caption saved');
    } catch (err) { toast(err.message); }
  });
  $('sheetPost').addEventListener('click', () => sheetAction('/web/api/media/post', 'Posting now. It should be live in a minute.'));
  $('sheetSkip').addEventListener('click', () => sheetAction('/web/api/media/skip', 'Photo skipped'));
  $('sheetDelete').addEventListener('click', async () => { if (sheetMedia && await deleteMedia(sheetMedia.id)) closeSheet(); });

  // ------------------------------------------------------------------ streak
  function computeStreak(days) {
    const set = new Set(days || []);
    const d = new Date();
    if (!set.has(d.toISOString().slice(0, 10))) d.setDate(d.getDate() - 1);   // today not posted yet: count from yesterday
    let n = 0;
    while (set.has(d.toISOString().slice(0, 10))) { n++; d.setDate(d.getDate() - 1); }
    return n;
  }

  function renderStreak() {
    const n = computeStreak(state && state.posted_days);
    const card = $('streakCard');
    card.hidden = !(me.signed_in && n > 0);
    if (n > 0) {
      $('streakNum').textContent = n + '-day streak';
      $('streakText').textContent = n === 1 ? 'You posted today. Come back tomorrow to keep it going.'
        : n >= 5 ? 'You have posted ' + n + ' days in a row. Keep it up!' : 'Keep posting daily to grow your streak.';
    }
  }

  // ------------------------------------------------------------------ vibes
  document.querySelectorAll('.vibe-chip').forEach(c => c.addEventListener('click', () => {
    saveAutopilot({vibe: c.dataset.vibe}, 'Vibe set to ' + c.textContent.trim());
  }));

  // --------------------------------------------------------- story preview
  async function loadStoryPreview(layoutId) {
    if (!sheetMedia) { toast('Pick a photo first.'); return; }
    try {
      const r = await postJSON('/web/api/story/preview', {id: sheetMedia.id, layout: layoutId || ''});
      $('mockupChips').innerHTML = r.layouts.map(l => {
        const cls = 'vibe-chip' + (l.id === r.layout ? ' active' : '');
        const label = l.description || l.id.replace(/_/g, ' ');
        return '<button class="' + cls + '" data-layout="' + escapeHTML(l.id) + '">' + escapeHTML(label) + '</button>';
      }).join('');
      $('mockupChips').querySelectorAll('[data-layout]').forEach(b => b.addEventListener('click', () => loadStoryPreview(b.dataset.layout)));
      $('mockupFrame').srcdoc = r.html;
      $('mockupWrap').hidden = false;
      requestAnimationFrame(() => $('mockupWrap').classList.add('open'));
    } catch (err) { toast(err.message); }
  }

  function closeMockup() {
    $('mockupWrap').classList.remove('open');
    setTimeout(() => { $('mockupWrap').hidden = true; $('mockupFrame').srcdoc = ''; }, 250);
  }

  $('sheetStory').addEventListener('click', () => loadStoryPreview());
  $('mockupOverlay').addEventListener('click', closeMockup);
  $('mockupClose').addEventListener('click', closeMockup);

  // ------------------------------------------------------------------ polish
  // Skeleton loaders: grey shimmer tiles while the first state loads.
  function showSkeletons() {
    const grid = document.querySelector('.media-grid');
    if (!grid) return;
    grid.innerHTML = Array.from({length: 8}, () =>
      '<article class="mtile skeleton"><div class="sk-img"></div><div class="sk-line"></div><div class="sk-line short"></div></article>'
    ).join('');
  }

  // Pull-to-refresh: drag down from the top on a phone to reload state.
  const ptr = document.createElement('div');
  ptr.className = 'ptr';
  ptr.innerHTML = '<span class="ptr-spinner"></span>';
  document.body.appendChild(ptr);
  let ptrStartY = null, ptrPull = 0;
  document.addEventListener('touchstart', e => {
    ptrStartY = (window.scrollY <= 0) ? e.touches[0].clientY : null;
  }, {passive: true});
  document.addEventListener('touchmove', e => {
    if (ptrStartY === null) return;
    ptrPull = Math.max(0, e.touches[0].clientY - ptrStartY);
    ptr.style.transform = 'translate(-50%, ' + Math.min(ptrPull * 0.4, 64) + 'px)';
    ptr.classList.toggle('show', ptrPull > 12);
  }, {passive: true});
  document.addEventListener('touchend', async () => {
    if (ptrStartY !== null && ptrPull > 80) {
      ptr.classList.add('spin');
      try { await loadState(); } catch (err) {}
    }
    ptrPull = 0; ptrStartY = null;
    ptr.classList.remove('show', 'spin');
    ptr.style.transform = 'translate(-50%, -48px)';
  });

  // Confetti: a small CSS-only burst when a post goes out.
  function confetti() {
    const colors = ['#FEDA75', '#FA7E1E', '#D62976', '#962FBF', '#4F5BD5'];
    const wrap = document.createElement('div');
    wrap.className = 'confetti';
    for (let i = 0; i < 26; i++) {
      const p = wrap.appendChild(document.createElement('i'));
      p.style.left = (Math.random() * 100).toFixed(1) + '%';
      p.style.background = colors[i % colors.length];
      p.style.animationDelay = (Math.random() * 0.3).toFixed(2) + 's';
      p.style.animationDuration = (1.1 + Math.random() * 0.9).toFixed(2) + 's';
    }
    document.body.appendChild(wrap);
    setTimeout(() => wrap.remove(), 2600);
  }

  // ------------------------------------------------------------------- events
  document.querySelectorAll('[data-filter]').forEach(b => b.addEventListener('click', () => { currentFilter = b.dataset.filter; renderMedia(); }));
  $('mediaSort').addEventListener('change', renderMedia);

  async function boot() {
    await loadMe();
    showSkeletons();
    await loadState();
    const hash = (location.hash || '#dashboard').slice(1);
    if (['dashboard', 'media', 'occasions', 'notifications', 'settings'].includes(hash)) navigate(hash);
  }
  boot();
})();
