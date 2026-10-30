// web/business.js - Business Mode UI (Phase 1b).
// Served by the backend at /app/business.html (same origin), so the session
// cookies are first-party. Talks only to /web/api/business* (Phase 1 APIs):
//   GET/POST  /web/api/business            list / create workspaces
//   GET/PATCH/DELETE /web/api/business/{wid}
//   POST /web/api/business/{wid}/activate  finish onboarding
//   GET  /web/api/business/{wid}/dashboard
//   GET  /web/api/business/{wid}/events
//   GET/POST /web/api/business/{wid}/products  PATCH/DELETE .../products/{pid}
//   GET/PATCH /web/api/business/{wid}/settings
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const escapeHTML = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  const CATEGORIES = [
    ['restaurant_cafe', 'Restaurant / Cafe'], ['clothing_fashion', 'Clothing / Fashion'],
    ['ecommerce', 'E-commerce'], ['retail', 'Retail store'], ['beauty_skincare', 'Beauty / Skincare'],
    ['fitness_gym', 'Fitness / Gym'], ['photography', 'Photography'], ['freelancing', 'Freelancing'],
    ['personal_brand', 'Personal brand'], ['digital_products', 'Digital products'],
    ['education', 'Education / Coaching'], ['travel', 'Travel'], ['real_estate', 'Real estate'],
    ['technology', 'Technology / SaaS'], ['other', 'Other'],
  ];
  const GOALS = [
    ['awareness', 'Brand awareness'], ['audience_growth', 'Grow the audience'], ['engagement', 'More engagement'],
    ['website_traffic', 'Website traffic'], ['enquiries', 'Enquiries / messages'], ['lead_generation', 'Lead generation'],
    ['product_sales', 'Product sales'], ['service_promotion', 'Promote services'], ['local_discovery', 'Local discovery'],
  ];
  const AGE_RANGES = ['under_18', '18_24', '25_34', '35_44', '45_54', '55_plus'];
  const AGE_LABELS = {under_18: 'Under 18', 18_24: '18-24', 25_34: '25-34', 35_44: '35-44', 45_54: '45-54', 55_plus: '55+'};
  const OB_STEPS = [
    ['basics', 'Basics', 'Name, category and where you are. This is what the engine knows about you.'],
    ['brand', 'Brand', 'Colors, personality and voice. Campaign copy is written to match this.'],
    ['audience', 'Audience', 'Who you are talking to. Content is tuned to them.'],
    ['goals', 'Goals', 'Pick up to 6. Campaigns optimise towards these.'],
    ['review', 'Review', 'Check everything over, then activate.'],
  ];

  let me = {signed_in: false};
  let workspaces = [];
  let ws = null;         // current workspace doc
  let dash = null;       // dashboard payload
  let campaigns = [];    // current workspace campaigns
  let obIndex = 0;
  let toastTimer;

  // ------------------------------------------------------------------ helpers
  function toast(msg) {
    const el = $('toast'); el.textContent = msg; el.classList.add('show');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('show'), 3200);
  }
  function when(v) {
    if (!v) return '';
    try { return new Date(v).toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}); } catch (e) { return ''; }
  }
  const hourLabel = h => String(h).padStart(2, '0') + ':00';

  async function call(path, options = {}) {
    const headers = Object.assign({}, options.headers || {});
    if (options.method && options.method !== 'GET') {
      if (!me.csrf) await loadMe();
      if (me.csrf) headers['X-CSRF-Token'] = me.csrf;
    }
    const res = await fetch(path, Object.assign({credentials: 'same-origin', cache: 'no-store'}, options, {headers}));
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON error page */ }
    if (res.status === 401) { me = {signed_in: false}; renderAuth(); }
    if (!res.ok) throw new Error(data.error || 'Something went wrong (' + res.status + ')');
    return data;
  }
  const sendJSON = (path, method, body) => call(path, {
    method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})
  });
  const api = (suffix, method, body) => {
    const p = '/web/api/business' + (suffix || '');
    return method ? sendJSON(p, method, body) : call(p);
  };

  async function loadMe() {
    try {
      const res = await fetch('/web/api/me', {credentials: 'same-origin', cache: 'no-store'});
      me = res.ok ? await res.json() : {signed_in: false};
    } catch (e) { me = {signed_in: false}; }
  }

  function renderAuth() {
    const on = !!me.signed_in;
    $('signedOut').classList.toggle('hidden', on);
    $('main').classList.toggle('hidden', !on);
  }

  // -------------------------------------------------------------- workspaces
  function statusTag(s) {
    return s === 'active'
      ? '<span class="tag active">Active</span>'
      : '<span class="tag onboarding">Onboarding</span>';
  }
  function renderList() {
    const host = $('wsList');
    if (!workspaces.length) {
      host.innerHTML = '<div class="empty">No workspaces yet. Create your first business above.</div>';
      return;
    }
    host.innerHTML = workspaces.map(w => {
      const letter = (w.name || 'B').trim().charAt(0).toUpperCase() || 'B';
      const hue = (w._id || '').split('').reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 360, 7);
      return '<button class="ws-card" data-ws="' + escapeHTML(w._id) + '">' +
        '<span class="ws-av" style="background:hsl(' + hue + ',62%,45%)">' + escapeHTML(letter) + '</span>' +
        '<span class="ws-main"><strong>' + escapeHTML(w.name) + '</strong>' +
        '<small>' + escapeHTML(w.category || 'other') + (w.location ? ' &middot; ' + escapeHTML(w.location) : '') + '</small></span>' +
        statusTag(w.status) + '</button>';
    }).join('');
    host.querySelectorAll('[data-ws]').forEach(b => b.addEventListener('click', () => openWorkspace(b.dataset.ws)));
  }

  async function loadWorkspaces() {
    const r = await api();
    workspaces = r.workspaces || [];
    renderList();
  }

  async function openWorkspace(id) {
    try {
      const r = await api('/' + id);
      ws = r.workspace;
      obIndex = Math.max(0, OB_STEPS.findIndex(s => s[0] === (ws.onboarding && ws.onboarding.step)));
      if (obIndex < 0) obIndex = 0;
      showWorkspace();
    } catch (err) { toast(err.message); }
  }

  function showWorkspace() {
    $('listSection').classList.add('hidden');
    $('wsSection').classList.remove('hidden');
    $('wsBadge').innerHTML = statusTag(ws.status) + ' <strong style="margin-left:8px">' + escapeHTML(ws.name) + '</strong>';
    const onboarding = ws.status !== 'active';
    $('onboarding').classList.toggle('hidden', !onboarding);
    $('dashboard').classList.toggle('hidden', onboarding);
    if (onboarding) renderWizard(); else loadDashboard();
  }

  function backToList() {
    ws = null; dash = null;
    $('wsSection').classList.add('hidden');
    $('listSection').classList.remove('hidden');
    loadWorkspaces().catch(err => toast(err.message));
  }

  // -------------------------------------------------------------- onboarding
  function renderWizard() {
    const step = OB_STEPS[obIndex];
    $('steps').innerHTML = OB_STEPS.map((s, i) =>
      '<span class="step ' + (i === obIndex ? 'on' : i < obIndex ? 'done' : '') + '">' + (i + 1) + '. ' + s[1] + '</span>').join('');
    $('obTitle').textContent = step[1];
    $('obHint').textContent = step[2];
    $('obPrev').style.visibility = obIndex === 0 ? 'hidden' : 'visible';
    $('obFinish').style.display = obIndex === OB_STEPS.length - 1 ? '' : 'none';
    $('obSave').style.display = obIndex === OB_STEPS.length - 1 ? 'none' : '';
    renderStepBody(step[0]);
  }

  function renderStepBody(step) {
    const host = $('obBody');
    const b = ws.brand || {}, a = ws.audience || {};
    if (step === 'basics') {
      host.innerHTML =
        '<label for="fName">Business name</label><input type="text" id="fName" maxlength="120" value="' + escapeHTML(ws.name || '') + '">' +
        '<label for="fCategory">Category</label><select id="fCategory">' +
        CATEGORIES.map(c => '<option value="' + c[0] + '"' + (ws.category === c[0] ? ' selected' : '') + '>' + c[1] + '</option>').join('') +
        '</select>' +
        '<label for="fDesc">What does your business do?</label><textarea id="fDesc" maxlength="2000" placeholder="e.g. A small cafe serving single-origin coffee and fresh bakes">' + escapeHTML(ws.description || '') + '</textarea>' +
        '<div class="row"><div><label for="fLoc">Location</label><input type="text" id="fLoc" maxlength="200" value="' + escapeHTML(ws.location || '') + '" placeholder="City / area"></div>' +
        '<div><label for="fWeb">Website</label><input type="url" id="fWeb" maxlength="300" value="' + escapeHTML(ws.website || '') + '" placeholder="https://"></div></div>' +
        '<label for="fContact">Contact (shown in posts when relevant)</label><input type="text" id="fContact" maxlength="200" value="' + escapeHTML(ws.contact || '') + '" placeholder="Phone / address / handle">';
      return;
    }
    if (step === 'brand') {
      const colors = b.colors && b.colors.length ? b.colors : [];
      host.innerHTML =
        '<label>Brand colors (click a swatch to change)</label><div class="swatches" id="swatches">' +
        [0, 1, 2, 3, 4, 5].map(i => {
          const c = colors[i] || '#D62976';
          return '<span class="swatch" style="background:' + escapeHTML(c) + '"><input type="color" data-color="' + i + '" value="' + escapeHTML(c) + '" aria-label="Brand color ' + (i + 1) + '"></span>';
        }).join('') + '</div>' +
        '<div class="row"><div><label for="fPers">Brand personality</label><input type="text" id="fPers" maxlength="120" value="' + escapeHTML(b.personality || '') + '" placeholder="e.g. warm, playful, honest"></div>' +
        '<div><label for="fVoice">Voice</label><input type="text" id="fVoice" maxlength="120" value="' + escapeHTML(b.voice || '') + '" placeholder="e.g. friendly expert, no slang"></div></div>' +
        '<div class="row"><div><label for="fLang">Content language</label><input type="text" id="fLang" maxlength="40" value="' + escapeHTML(b.language || 'english') + '"></div>' +
        '<div><label for="fTypo">Typography feel</label><input type="text" id="fTypo" maxlength="120" value="' + escapeHTML(b.typography || '') + '" placeholder="e.g. clean modern sans"></div></div>' +
        '<label for="fGuide">Anything the AI must always / never do</label><textarea id="fGuide" maxlength="2000" placeholder="e.g. never use the word cheap; always mention vegan options">' + escapeHTML(b.guidelines || '') + '</textarea>';
      return;
    }
    if (step === 'audience') {
      const ages = a.age_ranges || [];
      host.innerHTML =
        '<label for="fAudDesc">Who are they?</label><textarea id="fAudDesc" maxlength="1000" placeholder="e.g. college students and young professionals nearby who love trying new food spots">' + escapeHTML(a.description || '') + '</textarea>' +
        '<label>Age ranges</label><div class="chips" id="ageChips">' +
        AGE_RANGES.map(r => '<button type="button" class="chipbtn' + (ages.includes(r) ? ' on' : '') + '" data-age="' + r + '">' + AGE_LABELS[r] + '</button>').join('') + '</div>' +
        '<div class="row"><div><label for="fLocs">Locations</label><input type="text" id="fLocs" maxlength="200" value="' + escapeHTML((a.locations || []).join(', ')) + '" placeholder="comma separated"></div>' +
        '<div><label for="fInterests">Interests</label><input type="text" id="fInterests" maxlength="200" value="' + escapeHTML((a.interests || []).join(', ')) + '" placeholder="comma separated"></div></div>' +
        '<div class="row"><div><label for="fAudLang">Their preferred language</label><input type="text" id="fAudLang" maxlength="40" value="' + escapeHTML(a.language || '') + '" placeholder="same as content language if empty"></div></div>' +
        '<label for="fNeeds">What do they need from you?</label><textarea id="fNeeds" maxlength="1000" placeholder="e.g. quick affordable meals, gift options, event bookings">' + escapeHTML(a.needs || '') + '</textarea>';
      host.querySelectorAll('[data-age]').forEach(c => c.addEventListener('click', () => c.classList.toggle('on')));
      return;
    }
    if (step === 'goals') {
      const goals = ws.goals || [];
      host.innerHTML =
        '<label for="fGoals">Pick up to 6 goals</label><div class="chips" id="goalChips">' +
        GOALS.map(g => '<button type="button" class="chipbtn' + (goals.includes(g[0]) ? ' on' : '') + '" data-goal="' + g[0] + '">' + g[1] + '</button>').join('') + '</div>';
      host.querySelectorAll('[data-goal]').forEach(c => c.addEventListener('click', () => {
        const sel = Array.from(host.querySelectorAll('[data-goal].on')).map(x => x.dataset.goal);
        if (!c.classList.contains('on') && sel.length >= 6) { toast('Up to 6 goals.'); return; }
        c.classList.toggle('on');
      }));
      return;
    }
    // review
    const b2 = ws.brand || {}, a2 = ws.audience || {};
    const li = (k, v) => v ? '<div class="item"><strong>' + escapeHTML(k) + '</strong><small>' + escapeHTML(v) + '</small></div>' : '';
    host.innerHTML =
      '<div class="item"><strong>' + escapeHTML(ws.name || '') + '</strong><small>' + escapeHTML(ws.category || '') + (ws.location ? ' &middot; ' + escapeHTML(ws.location) : '') + '</small></div>' +
      li('Description', ws.description) +
      li('Brand colors', (b2.colors || []).join(' ')) +
      li('Personality', b2.personality) + li('Voice', b2.voice) + li('Language', b2.language) +
      li('Audience', a2.description) + li('Audience locations', (a2.locations || []).join(', ')) +
      li('Goals', (ws.goals || []).map(g => (GOALS.find(x => x[0] === g) || [g, g])[1]).join(', ')) +
      '<p class="note">After activating, automation still starts OFF. You switch each capability on yourself.</p>' +
      '<button class="btn ghost sm" id="editProfileBtn">Edit anything first</button>';
    $('editProfileBtn').addEventListener('click', () => { obIndex = 0; renderWizard(); });
  }

  function collectStep(step) {
    if (step === 'basics') {
      return {
        name: $('fName').value.trim(), category: $('fCategory').value,
        description: $('fDesc').value.trim(), location: $('fLoc').value.trim(),
        website: $('fWeb').value.trim(), contact: $('fContact').value.trim(),
      };
    }
    if (step === 'brand') {
      const colors = Array.from(document.querySelectorAll('[data-color]'))
        .map(i => i.value).filter((v, i, arr) => HEXOK.test(v) && arr.indexOf(v) === i).slice(0, 6);
      return {
        brand: {
          colors, personality: $('fPers').value.trim(), voice: $('fVoice').value.trim(),
          language: $('fLang').value.trim() || 'english', typography: $('fTypo').value.trim(),
          guidelines: $('fGuide').value.trim(),
        },
      };
    }
    if (step === 'audience') {
      const split = v => v.split(/[,
]/).map(s => s.trim()).filter(Boolean);
      return {
        audience: {
          description: $('fAudDesc').value.trim(),
          age_ranges: Array.from(document.querySelectorAll('[data-age].on')).map(x => x.dataset.age).slice(0, 5),
          locations: split($('fLocs').value).slice(0, 6),
          interests: split($('fInterests').value).slice(0, 10),
          language: $('fAudLang').value.trim(),
          needs: $('fNeeds').value.trim(),
        },
      };
    }
    if (step === 'goals') {
      return {goals: Array.from(document.querySelectorAll('[data-goal].on')).map(x => x.dataset.goal).slice(0, 6)};
    }
    return {};
  }
  const HEXOK = /^#[0-9a-fA-F]{6}$/;

  async function saveStep(advance) {
    const step = OB_STEPS[obIndex][0];
    const body = collectStep(step);
    body.onboarding = {step: OB_STEPS[Math.min(obIndex + 1, OB_STEPS.length - 1)][0]};
    if (step === 'basics' && !body.name) { toast('Business name is required.'); return false; }
    try {
      const r = await api('/' + ws._id, 'PATCH', body);
      ws = r.workspace;
      if (advance) { obIndex = Math.min(obIndex + 1, OB_STEPS.length - 1); renderWizard(); }
      return true;
    } catch (err) { toast(err.message); return false; }
  }

  async function activate() {
    try {
      const r = await api('/' + ws._id + '/activate', 'POST', {});
      ws = r.workspace;
      toast('Business Mode activated for ' + ws.name + '. Automation starts off.');
      showWorkspace();
    } catch (err) { toast(err.message); }
  }

  // -------------------------------------------------------------- dashboard
  async function loadDashboard() {
    try {
      dash = await api('/' + ws._id + '/dashboard');
      renderDashboard();
    } catch (err) { toast(err.message); }
  }

  function renderDashboard() {
    const c = (dash && dash.counts) || {};
    $('stProducts').textContent = c.products || 0;
    $('stCampaigns').textContent = c.campaigns || 0;
    $('stLeads').textContent = c.leads || 0;
    renderProducts();
    renderSettings();
    renderEvents();
    loadCampaigns();
  }

  // products
  function renderProducts() {
    const list = (dash && dash.products) || [];
    const host = $('productList');
    if (!list.length) { host.innerHTML = '<div class="empty">No products yet. Add what you sell so posts stay accurate.</div>'; return; }
    host.innerHTML = list.map(p => {
      const price = p.price ? '<span class="money">' + escapeHTML(p.price) + (p.currency ? ' ' + escapeHTML(p.currency) : '') + '</span>' : '';
      return '<div class="item"><div style="flex:1;min-width:0"><strong>' + escapeHTML(p.name) + ' ' + price +
        (p.availability === 'unavailable' ? ' <span class="tag onboarding">Unavailable</span>' : '') +
        (p.promotional ? ' <span class="tag active">Promo</span>' : '') + '</strong>' +
        '<small>' + escapeHTML(p.description || '') + (p.url ? ' &middot; ' + escapeHTML(p.url) : '') + '</small></div>' +
        '<button class="mini mut" data-edit="' + escapeHTML(p._id) + '">Edit</button>' +
        '<button class="mini" data-del="' + escapeHTML(p._id) + '">Delete</button></div>';
    }).join('');
    host.querySelectorAll('[data-del]').forEach(b => b.addEventListener('click', async () => {
      if (!confirm('Delete this product?')) return;
      try { await api('/' + ws._id + '/products/' + b.dataset.del, 'DELETE'); toast('Product deleted'); loadDashboard(); }
      catch (err) { toast(err.message); }
    }));
    host.querySelectorAll('[data-edit]').forEach(b => b.addEventListener('click', () => {
      const p = ((dash && dash.products) || []).find(x => x._id === b.dataset.edit);
      if (p) openProductForm(p);
    }));
  }

  function openProductForm(p) {
    $('prodId').value = p ? p._id : '';
    $('prodName').value = p ? p.name : '';
    $('prodPrice').value = p ? (p.price || '') : '';
    $('prodCurrency').value = p ? (p.currency || '') : 'INR';
    $('prodDesc').value = p ? (p.description || '') : '';
    $('prodCat').value = p ? (p.category || '') : '';
    $('prodUrl').value = p ? (p.url || '') : '';
    $('prodAvail').checked = p ? p.availability !== 'unavailable' : true;
    $('prodPromo').checked = p ? !!p.promotional : false;
    $('productForm').classList.remove('hidden');
  }

  async function saveProduct() {
    const body = {
      name: $('prodName').value.trim(),
      description: $('prodDesc').value.trim(),
      price: $('prodPrice').value.trim(),
      currency: $('prodCurrency').value.trim(),
      category: $('prodCat').value.trim(),
      url: $('prodUrl').value.trim(),
      availability: $('prodAvail').checked ? 'available' : 'unavailable',
      promotional: $('prodPromo').checked,
    };
    if (!body.name) { toast('Product name is required.'); return; }
    const id = $('prodId').value;
    try {
      if (id) await api('/' + ws._id + '/products/' + id, 'PATCH', body);
      else await api('/' + ws._id + '/products', 'POST', body);
      $('productForm').classList.add('hidden');
      toast(id ? 'Product updated' : 'Product added');
      loadDashboard();
    } catch (err) { toast(err.message); }
  }

  // settings
  function renderSettings() {
    const s = (dash && dash.settings) || {};
    if (!$('setDaily').options.length) {
      $('setDaily').innerHTML = Array.from({length: 12}, (_, i) => '<option value="' + (i + 1) + '">' + (i + 1) + (i ? ' posts' : ' post') + ' / day</option>').join('');
      $('setQuietStart').innerHTML = $('setQuietEnd').innerHTML =
        Array.from({length: 24}, (_, i) => '<option value="' + i + '">' + hourLabel(i) + '</option>').join('');
    }
    $('setAutoPublish').checked = !!s.auto_publish;
    $('setAutoComments').checked = !!s.auto_comments;
    $('setAutoReplies').checked = !!s.auto_replies;
    $('setDaily').value = s.daily_posts || 2;
    $('setQuietStart').value = s.quiet_start == null ? 22 : s.quiet_start;
    $('setQuietEnd').value = s.quiet_end == null ? 8 : s.quiet_end;
  }

  async function saveSettings(body, okMsg) {
    try {
      const r = await api('/' + ws._id + '/settings', 'PATCH', body);
      if (dash) dash.settings = r.settings;
      if (okMsg) toast(okMsg);
    } catch (err) { toast(err.message); renderSettings(); }
  }

  function renderEvents() {
    const rows = (dash && dash.events) || [];
    const host = $('eventList');
    if (!rows.length) { host.innerHTML = '<div class="empty">Nothing logged yet.</div>'; return; }
    host.innerHTML = rows.map(e =>
      '<div class="item"><div style="flex:1;min-width:0"><strong>' + escapeHTML(e.message) + '</strong>' +
      '<small>' + escapeHTML(e.kind) + '</small></div><span class="time">' + escapeHTML(when(e.created_at)) + '</span></div>').join('');
  }

  // -------------------------------------------------------------- campaigns
  function campaignTag(s) {
    const cls = s === 'active' ? 'active' : (s === 'stopped' ? 'stopped' : 'onboarding');
    return '<span class="tag ' + cls + '">' + escapeHTML(s || 'draft') + '</span>';
  }
  function goalLabel(g) { return (GOALS.find(x => x[0] === g) || [g, g || ''])[1]; }

  async function loadCampaigns() {
    try {
      const r = await api('/' + ws._id + '/campaigns');
      campaigns = r.campaigns || [];
      renderCampaigns();
    } catch (err) { toast(err.message); }
  }

  function renderCampaigns() {
    const host = $('campaignList');
    if (!campaigns.length) {
      host.innerHTML = '<div class="empty">No campaigns yet. Create one, generate a brief and a plan, then start it.</div>';
      return;
    }
    host.innerHTML = campaigns.map(c => {
      const s = c.status || 'draft';
      const briefOk = !!(c.brief && c.brief.positioning);
      const planOk = !!(c.plan && c.plan.length);
      let acts = '';
      const act = (a, label) => '<button class="mini mut" data-act="' + a + '" data-cid="' + escapeHTML(c._id) + '">' + label + '</button>';
      if (s !== 'stopped') {
        acts += act('brief', briefOk ? 'Re-run brief' : 'Strategy brief');
        if (briefOk) acts += act('plan', planOk ? 'Re-plan' : 'Content plan');
      }
      if (s === 'draft' || s === 'paused') acts += act('start', 'Start');
      if (s === 'active') acts += act('pause', 'Pause');
      if (s === 'paused') acts += act('resume', 'Resume');
      if (s !== 'stopped') acts += act('stop', 'Stop');
      return '<div class="item"><div style="flex:1;min-width:0">' +
        '<strong>' + escapeHTML(c.name) + ' ' + campaignTag(s) + '</strong>' +
        '<small>' + escapeHTML(goalLabel(c.objective)) + ' &middot; ' + (c.duration_days || 14) + ' days' +
        (briefOk ? ' &middot; brief ready' : '') +
        (planOk ? ' &middot; ' + c.plan.length + ' planned posts' : '') + '</small></div>' +
        '<span style="white-space:nowrap">' + acts + '</span></div>' +
        '<details data-detail="' + escapeHTML(c._id) + '"><summary style="cursor:pointer;color:var(--mut);font-size:12.5px">Brief &amp; plan</summary><div class="detailBody" style="padding:8px 0"></div></details>';
    }).join('');
    host.querySelectorAll('[data-act]').forEach(b => b.addEventListener('click', () => campaignAction(b)));
    host.querySelectorAll('[data-detail]').forEach(d => d.addEventListener('toggle', () => {
      if (d.open) loadCampaignDetail(d.dataset.detail, d.querySelector('.detailBody'));
    }));
  }

  async function loadCampaignDetail(cid, body) {
    body.textContent = 'Loading...';
    try {
      const r = await api('/' + ws._id + '/campaigns/' + cid);
      const c = r.campaign || {};
      const b = c.brief || {};
      const blk = (k, arr) => (arr && arr.length)
        ? '<div class="item"><strong>' + escapeHTML(k) + '</strong><small>' + arr.map(x => '&bull; ' + escapeHTML(x)).join('<br>') + '</small></div>'
        : '';
      const plan = (c.plan || []).map(p =>
        '<div class="item"><strong>Day ' + (p.day || '?') + ' &middot; ' + escapeHTML(p.format || 'feed') + '</strong>' +
        '<small>' + escapeHTML(p.angle || '') + (p.note ? ' &mdash; ' + escapeHTML(p.note) : '') + '</small></div>').join('');
      body.innerHTML =
        blk('Positioning', b.positioning ? [b.positioning] : []) +
        blk('Key messages', b.key_messages) +
        blk('Content angles', b.content_angles) +
        blk('Never say', b.do_not_say) +
        (plan || '<div class="empty">No plan yet.</div>');
    } catch (err) { body.textContent = err.message; }
  }

  async function campaignAction(btn) {
    const cid = btn.dataset.cid, act = btn.dataset.act;
    const label = btn.textContent;
    btn.disabled = true; btn.textContent = 'Working...';
    try {
      const r = await api('/' + ws._id + '/campaigns/' + cid + '/' + act, 'POST', {});
      toast('Campaign ' + ((r.campaign && r.campaign.status) || act));
      await loadCampaigns();
    } catch (err) {
      toast(err.message);
      btn.disabled = false; btn.textContent = label;
    }
  }

  // ------------------------------------------------------------------ events
  $('newWsBtn').addEventListener('click', () => { $('newWsForm').classList.remove('hidden'); $('newName').focus(); });
  $('cancelNewWs').addEventListener('click', () => { $('newWsForm').classList.add('hidden'); $('newName').value = ''; });
  $('createWsBtn').addEventListener('click', async () => {
    const name = $('newName').value.trim();
    if (!name) { toast('Give your business a name.'); return; }
    try {
      const r = await api('', 'POST', {name});
      toast('Workspace created. Let\u2019s set it up.');
      $('newName').value = ''; $('newWsForm').classList.add('hidden');
      ws = r.workspace; obIndex = 0;
      showWorkspace();
    } catch (err) { toast(err.message); }
  });
  $('backToList').addEventListener('click', backToList);
  $('obPrev').addEventListener('click', () => { if (obIndex > 0) { obIndex--; renderWizard(); } });
  $('obSave').addEventListener('click', () => saveStep(true));
  $('obFinish').addEventListener('click', async () => {
    const ok = await saveStep(false);
    if (ok) activate();
  });
  $('addCampaignBtn').addEventListener('click', () => {
    if (!$('campObjective').options.length) {
      $('campObjective').innerHTML = GOALS.map(g => '<option value="' + g[0] + '">' + g[1] + '</option>').join('');
    }
    $('campaignForm').classList.remove('hidden');
    $('campName').focus();
  });
  $('campCancel').addEventListener('click', () => { $('campaignForm').classList.add('hidden'); $('campName').value = ''; });
  $('campSave').addEventListener('click', async () => {
    const name = $('campName').value.trim();
    if (!name) { toast('Give the campaign a name.'); return; }
    try {
      await api('/' + ws._id + '/campaigns', 'POST', {
        name, objective: $('campObjective').value,
        duration_days: Number($('campDuration').value),
      });
      $('campName').value = '';
      $('campaignForm').classList.add('hidden');
      toast('Campaign created as draft');
      loadCampaigns();
    } catch (err) { toast(err.message); }
  });
  $('addProductBtn').addEventListener('click', () => openProductForm(null));
  $('prodSave').addEventListener('click', saveProduct);
  $('prodCancel').addEventListener('click', () => $('productForm').classList.add('hidden'));
  $('resumeSetup').addEventListener('click', () => {
    obIndex = 0;
    $('dashboard').classList.add('hidden');
    $('onboarding').classList.remove('hidden');
    renderWizard();
    $('onboarding').scrollIntoView({behavior: 'smooth'});
  });
  $('setAutoPublish').addEventListener('change', e =>
    saveSettings({auto_publish: e.target.checked}, e.target.checked ? 'Auto-publish on' : 'Auto-publish off'));
  $('setAutoComments').addEventListener('change', e =>
    saveSettings({auto_comments: e.target.checked}, e.target.checked ? 'First comments on' : 'First comments off'));
  $('setAutoReplies').addEventListener('change', e =>
    saveSettings({auto_replies: e.target.checked}, e.target.checked ? 'Comment replies on' : 'Comment replies off'));
  $('setDaily').addEventListener('change', e => saveSettings({daily_posts: Number(e.target.value)}, 'Daily cap saved'));
  $('setQuietStart').addEventListener('change', e => saveSettings({quiet_start: Number(e.target.value)}));
  $('setQuietEnd').addEventListener('change', e => saveSettings({quiet_end: Number(e.target.value)}));

  // -------------------------------------------------------------------- boot
  async function boot() {
    await loadMe();
    renderAuth();
    if (!me.signed_in) return;
    try { await loadWorkspaces(); } catch (err) { toast(err.message); }
  }
  boot();
})();
