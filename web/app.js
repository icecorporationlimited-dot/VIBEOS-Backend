(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let state = {media:[], queue:[], activity:[], settings:{autopilot:false,story:false,caption:true}};
  let currentFilter='all', currentQueueFilter='all', toastTimer;
  const escapeHTML = v => String(v ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  // Netlify serves static files; it cannot run run_webapp.py or the local /api/* routes.
  // Keep the dashboard fully usable on a static host with IndexedDB-backed workspace data.
  // Live Instagram publishing remains a separate backend/Meta integration.
  const DB_NAME='vibeos-studio';
  const DB_STORE='workspace';
  let dbPromise;
  function openWorkspaceDB(){
    if(dbPromise) return dbPromise;
    dbPromise=new Promise((resolve,reject)=>{
      if(!('indexedDB' in window)){reject(new Error('This browser does not support persistent workspace storage.'));return;}
      const req=indexedDB.open(DB_NAME,1);
      req.onupgradeneeded=()=>{if(!req.result.objectStoreNames.contains(DB_STORE))req.result.createObjectStore(DB_STORE);};
      req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error||new Error('Could not open workspace storage.'));
    });
    return dbPromise;
  }
  async function readWorkspace(){
    const db=await openWorkspaceDB();
    return new Promise((resolve,reject)=>{const tx=db.transaction(DB_STORE,'readonly');const req=tx.objectStore(DB_STORE).get('state');req.onsuccess=()=>resolve(req.result||{media:[],queue:[],activity:[],settings:{autopilot:false,story:false,caption:true},instagramConnectUrl:(window.VIBEOS_API||'https://vibeos-backend.onrender.com').replace(/\/$/,'')+'/login'});req.onerror=()=>reject(req.error);});
  }
  async function writeWorkspace(next){
    const db=await openWorkspaceDB();
    return new Promise((resolve,reject)=>{const tx=db.transaction(DB_STORE,'readwrite');tx.objectStore(DB_STORE).put(next,'state');tx.oncomplete=()=>resolve(next);tx.onerror=()=>reject(tx.error||new Error('Could not save workspace.'));});
  }
  const stamp=()=>new Date().toISOString();
  const makeId=()=>window.crypto&&crypto.randomUUID?crypto.randomUUID():Date.now().toString(36)+Math.random().toString(36).slice(2);
  async function localApi(path,options={}){
    const method=(options.method||'GET').toUpperCase();
    let s=await readWorkspace();
    const activity=(title,description,icon='✦')=>{s.activity.unshift({id:makeId(),title,description,icon,at:stamp()});s.activity=s.activity.slice(0,100);};
    if(path==='/api/state'&&method==='GET')return s;
    if(path==='/api/media'&&method==='POST'){
      const form=options.body;const files=form&&form.getAll?form.getAll('files'):[];const valid=files.filter(f=>f instanceof File&&f.type.startsWith('image/'));
      if(!valid.length)throw new Error('Choose one or more image files.');
      const added=[];
      for(const f of valid){if(f.size>10*1024*1024)throw new Error(`${f.name} is larger than 10 MB.`);const url=await new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(r.result);r.onerror=()=>rej(new Error('Could not read '+f.name));r.readAsDataURL(f);});const m={id:makeId(),name:f.name,size:f.size,type:f.type,createdAt:stamp(),url};s.media.unshift(m);added.push(m);}
      activity('Photos added to media library',`${added.length} photo(s) saved in this browser workspace.`,'▧');await writeWorkspace(s);return {ok:true,added:added.length,state:s};
    }
    if(path==='/api/queue'&&method==='POST'){
      const b=JSON.parse(options.body||'{}');if(!s.media.some(m=>m.id===b.mediaId))throw new Error('Photo not found. Upload a photo first.');if(!['Story','Post','Reel'].includes(b.format))throw new Error('Choose Story, Post or Reel.');
      const q={id:makeId(),mediaId:b.mediaId,caption:String(b.caption||'').slice(0,2200),format:b.format,when:b.when||'',status:b.when?'scheduled':'draft',createdAt:stamp()};s.queue.push(q);activity('Added to publishing queue',`${q.format} saved as ${q.status}. This is a plan, not a live Instagram post.`,'◷');await writeWorkspace(s);return {ok:true,state:s};
    }
    if(path.startsWith('/api/queue/')&&method==='PUT'){
      const id=decodeURIComponent(path.split('/').pop()),b=JSON.parse(options.body||'{}'),q=s.queue.find(x=>x.id===id);if(!q)throw new Error('Queue item not found.');if(!s.media.some(m=>m.id===b.mediaId))throw new Error('Choose an existing photo.');Object.assign(q,{mediaId:b.mediaId,caption:String(b.caption||'').slice(0,2200),format:b.format,when:b.when||'',status:b.when?'scheduled':'draft'});activity('Queue item updated',`${q.format} plan updated.`,'◷');await writeWorkspace(s);return {ok:true,state:s};
    }
    if(path.startsWith('/api/queue/')&&method==='DELETE'){
      const id=decodeURIComponent(path.split('/').pop());s.queue=s.queue.filter(q=>q.id!==id);activity('Queue item removed','The draft was removed from your queue.','⌫');await writeWorkspace(s);return {ok:true,state:s};
    }
    if(path.startsWith('/api/media/')&&method==='DELETE'){
      const id=decodeURIComponent(path.split('/').pop()),m=s.media.find(x=>x.id===id);if(!m)throw new Error('Photo not found.');s.media=s.media.filter(x=>x.id!==id);s.queue=s.queue.filter(q=>q.mediaId!==id);activity('Photo deleted',`${m.name} and its queue drafts were removed.`,'⌫');await writeWorkspace(s);return {ok:true,state:s};
    }
    if(path==='/api/settings'&&method==='POST'){
      const b=JSON.parse(options.body||'{}');s.settings={...s.settings,autopilot:!!b.autopilot,story:!!b.story,caption:!!b.caption};activity('Studio settings saved','Preferences saved in this browser. These switches do not activate hosted publishing.','⚙');await writeWorkspace(s);return {ok:true,state:s};
    }
    if(path==='/api/notifications/clear'&&method==='POST'){s.activity=[];await writeWorkspace(s);return {ok:true,state:s};}
    if(path==='/api/reset'&&method==='POST'){s={media:[],queue:[],activity:[],settings:{autopilot:false,story:false,caption:true},instagramConnectUrl:(window.VIBEOS_API||'https://vibeos-backend.onrender.com').replace(/\/$/,'')+'/login'};activity('Workspace reset','Browser media, queue drafts and activity were cleared.','⌫');await writeWorkspace(s);return {ok:true,state:s};}
    throw new Error('This action is not supported in the browser workspace.');
  }
  async function api(path, options={}) { return localApi(path,options); }
  function toast(msg){const el=$('toast');el.textContent=msg;el.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('show'),3000);}
  function navigate(view){document.querySelectorAll('.view').forEach(e=>e.classList.toggle('active',e.id==='view-'+view));document.querySelectorAll('[data-view]').forEach(e=>e.classList.toggle('active',e.classList.contains('nav-item')&&e.dataset.view===view));const names={dashboard:'Overview',media:'Media library',queue:'Publishing queue',notifications:'Notifications',settings:'Settings'};$('crumbTitle').textContent=names[view]||'Overview';$('sidebar').classList.remove('open');window.location.hash=view;}
  document.querySelectorAll('[data-view]').forEach(e=>e.addEventListener('click',()=>navigate(e.dataset.view)));
  $('mobileMenu').addEventListener('click',()=>$('sidebar').classList.toggle('open'));
  $('tipButton').addEventListener('click',e=>{e.currentTarget.closest('.help-card').style.display='none';toast('Tip tucked away');});
  function openPicker(){$('filePicker').click();}
  ['topUpload','emptyUpload','mediaUpload','emptyMediaUpload'].forEach(id=>$(id).addEventListener('click',openPicker));
  $('filePicker').addEventListener('change',async e=>{
    const files=[...e.target.files||[]].filter(f=>f.type.startsWith('image/')); if(!files.length)return;
    const form=new FormData(); files.forEach(f=>form.append('files',f,f.name));
    try { const result=await api('/api/media',{method:'POST',body:form}); state=result.state; renderAll(); toast(`${result.added} photo(s) uploaded to local VIBEOS storage`); }
    catch(err){toast(err.message||'Upload failed');} finally{e.target.value='';}
  });
  function sizeLabel(n){return n<1048576?Math.max(1,Math.round(n/1024))+' KB':(n/1048576).toFixed(1)+' MB';}
  function dateLabel(v){try{return new Date(v).toLocaleDateString(undefined,{month:'short',day:'numeric'});}catch(_){return 'Recently';}}
  function dateTimeLabel(v){if(!v)return 'Draft · no schedule';try{return new Date(v).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});}catch(_){return v;}}
  function renderCounts(){ $('mediaCount').textContent=state.media.length;$('queueCount').textContent=state.queue.length;$('statMedia').textContent=state.media.length;$('statQueue').textContent=state.queue.length;$('allCount').textContent=state.media.length;$('mediaSummary').textContent=`${state.media.length} item${state.media.length===1?'':'s'}`;$('qAllCount').textContent=state.queue.length;$('autoStat').textContent=state.settings.autopilot?'On':'Off'; }
  function mediaCard(m){return `<article class="media-card"><img class="media-thumb" src="${m.url}" alt="${escapeHTML(m.name)}"><div class="media-card-info"><span class="media-card-title" title="${escapeHTML(m.name)}">${escapeHTML(m.name)}</span><div class="media-card-meta"><span>${sizeLabel(m.size)}</span><span>${dateLabel(m.createdAt)}</span></div><div class="media-card-actions"><button class="mini-action" data-addqueue="${m.id}">＋ Queue</button><button class="mini-action delete" data-delete-media="${m.id}">Delete</button></div></div></article>`;}
  function renderMedia(){let rows=[...state.media];if(currentFilter==='photo')rows=rows.filter(m=>m.type&&m.type.startsWith('image/'));rows.sort((a,b)=>$('mediaSort').value==='oldest'?new Date(a.createdAt)-new Date(b.createdAt):new Date(b.createdAt)-new Date(a.createdAt));$('mediaGrid').innerHTML=rows.length?rows.map(mediaCard).join(''):`<div class="empty-large"><div class="empty-icon big">▧</div><h3>${currentFilter==='all'?'Your library is waiting for its first upload':'No photos in this filter'}</h3><p>Upload a photo to start planning your content.</p><button class="primary-button" id="emptyMediaUpload2">＋ Upload photos</button></div>`;const btn=$('emptyMediaUpload2');if(btn)btn.addEventListener('click',openPicker);document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b.dataset.filter===currentFilter));document.querySelectorAll('[data-addqueue]').forEach(b=>b.addEventListener('click',()=>openQueueModal(b.dataset.addqueue)));document.querySelectorAll('[data-delete-media]').forEach(b=>b.addEventListener('click',()=>deleteMedia(b.dataset.deleteMedia)));}
  function renderRecent(){const host=$('recentMedia'),items=state.media.slice(0,4);if(!items.length){host.className='recent-media empty-state';host.innerHTML='<div class="empty-icon">▧</div><strong>Your next great post starts here</strong><p>Upload photos to start building your media library.</p><button class="soft-button" id="emptyUpload2">＋ Add photos</button>';$('emptyUpload2').addEventListener('click',openPicker);return;}host.className='recent-media';host.style.display='grid';host.style.gridTemplateColumns='repeat(4,minmax(0,1fr))';host.style.gap='8px';host.innerHTML=items.map(m=>`<button title="${escapeHTML(m.name)}" data-recent-media="${m.id}" style="padding:0;border:0;background:none;min-width:0;text-align:left"><img src="${m.url}" alt="${escapeHTML(m.name)}" style="display:block;width:100%;aspect-ratio:1;object-fit:cover;border-radius:10px"><span style="display:block;font-size:9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:6px;color:#8e8497">${escapeHTML(m.name)}</span></button>`).join('');host.querySelectorAll('[data-recent-media]').forEach(b=>b.addEventListener('click',()=>navigate('media')));}
  function queueRow(q,compact=false){const m=state.media.find(x=>x.id===q.mediaId),thumb=m?m.url:'';const status=q.status==='scheduled'?'SCHEDULED PLAN':'DRAFT';return `<div class="${compact?'empty-inline':'queue-row'}">${compact?`<span>◷</span><div><strong>${escapeHTML(q.format)} · ${escapeHTML(m?.name||'Missing media')}</strong><small>${escapeHTML(dateTimeLabel(q.when))}</small></div>`:`<img class="queue-thumb" src="${thumb}" alt=""><div class="queue-row-main"><strong>${escapeHTML(q.format)} · ${escapeHTML(status)}</strong><p>${escapeHTML(q.caption||'No caption added yet')}</p><div class="queue-meta"><span>◷ ${escapeHTML(dateTimeLabel(q.when))}</span><span>▧ ${escapeHTML(m?.name||'Missing media')}</span></div></div><span class="draft-chip">${status}</span><div class="queue-row-actions"><button data-edit-queue="${q.id}">Edit</button><button data-delete-queue="${q.id}">Remove</button></div>`}</div>`;}
  function renderQueue(){let rows=[...state.queue];if(currentQueueFilter==='scheduled')rows=rows.filter(q=>q.when);if(currentQueueFilter==='draft')rows=rows.filter(q=>!q.when);$('recentQueue').innerHTML=state.queue.length?state.queue.slice(0,3).map(q=>queueRow(q,true)).join(''):'<div class="empty-inline"><span>◷</span><div><strong>Nothing queued yet</strong><small>Add a photo to prepare your first plan.</small></div></div>';$('queueFullList').innerHTML=rows.length?rows.map(q=>queueRow(q)).join(''):`<div class="empty-large"><div class="empty-icon big">◷</div><h3>Your queue is clear</h3><p>Add media from your library to prepare a post.</p><button class="soft-button" id="goMedia">Go to media library →</button></div>`;const gm=$('goMedia');if(gm)gm.addEventListener('click',()=>navigate('media'));document.querySelectorAll('[data-qfilter]').forEach(b=>b.classList.toggle('active',b.dataset.qfilter===currentQueueFilter));document.querySelectorAll('[data-delete-queue]').forEach(b=>b.addEventListener('click',()=>deleteQueue(b.dataset.deleteQueue)));document.querySelectorAll('[data-edit-queue]').forEach(b=>b.addEventListener('click',()=>openQueueModal(null,b.dataset.editQueue)));}
  function openQueueModal(mediaId,editId){if(!state.media.length){toast('Upload a photo before adding to the queue');navigate('media');return;}const q=editId?state.queue.find(x=>x.id===editId):null;$('queueAsset').innerHTML=state.media.map(m=>`<option value="${m.id}" ${(q?q.mediaId===m.id:mediaId===m.id)?'selected':''}>${escapeHTML(m.name)}</option>`).join('');$('queueCaption').value=q?.caption||'';$('queueFormat').value=q?.format||'Story';$('queueWhen').value=q?.when?new Date(q.when).toISOString().slice(0,16):'';$('saveQueue').dataset.editId=editId||'';$('queueModal').hidden=false;}
  function closeQueueModal(){$('queueModal').hidden=true;}
  $('queueAdd').addEventListener('click',()=>openQueueModal());$('closeModal').addEventListener('click',closeQueueModal);$('queueModal').addEventListener('click',e=>{if(e.target===$('queueModal'))closeQueueModal();});
  $('saveQueue').addEventListener('click',async()=>{const mediaId=$('queueAsset').value;if(!mediaId)return;const when=$('queueWhen').value?new Date($('queueWhen').value).toISOString():'';const payload={mediaId,caption:$('queueCaption').value.trim(),format:$('queueFormat').value,when};const editId=$('saveQueue').dataset.editId;try{const result=editId?await api('/api/queue/'+encodeURIComponent(editId),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}):await api('/api/queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});state=result.state;closeQueueModal();renderAll();toast(editId?'Queue item updated':'Added to publishing queue');}catch(err){toast(err.message||'Could not save queue item');}});
  async function deleteMedia(id){const q=state.queue.some(x=>x.mediaId===id);if(!confirm('Delete this photo from local VIBEOS storage?'+(q?' Queue items using it will also be removed.':'')))return;try{const r=await api('/api/media/'+encodeURIComponent(id),{method:'DELETE'});state=r.state;renderAll();toast('Photo deleted');}catch(e){toast(e.message);}}
  async function deleteQueue(id){if(!confirm('Remove this item from the publishing plan?'))return;try{const r=await api('/api/queue/'+encodeURIComponent(id),{method:'DELETE'});state=r.state;renderAll();toast('Queue item removed');}catch(e){toast(e.message);}}
  function renderNotifications(){const host=$('notificationList');host.innerHTML=state.activity.length?state.activity.map(a=>`<article class="notification-item"><div class="notification-icon">${escapeHTML(a.icon||'✦')}</div><div><strong>${escapeHTML(a.title)}</strong><p>${escapeHTML(a.description)}</p><time>${escapeHTML(new Date(a.at).toLocaleString())}</time></div></article>`).join(''):'<div class="empty-large"><div class="empty-icon big">♧</div><h3>No activity yet</h3><p>Your workspace actions will show up here.</p></div>';}
  document.querySelectorAll('[data-filter]').forEach(b=>b.addEventListener('click',()=>{currentFilter=b.dataset.filter;renderMedia();}));document.querySelectorAll('[data-qfilter]').forEach(b=>b.addEventListener('click',()=>{currentQueueFilter=b.dataset.qfilter;renderQueue();}));$('mediaSort').addEventListener('change',renderMedia);
  $('clearNotifications').addEventListener('click',async()=>{try{await api('/api/notifications/clear',{method:'POST'});state.activity=[];renderNotifications();toast('Notifications cleared');}catch(e){toast(e.message);}});
  async function savePrefs(){const p={autopilot:$('autopilotToggle').checked,story:$('storyToggle').checked,caption:$('captionToggle').checked};try{const r=await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});state=r.state;renderAll();toast('Settings saved on this device');}catch(e){toast(e.message);}}
  ['autopilotToggle','storyToggle','captionToggle'].forEach(id=>$(id).addEventListener('change',savePrefs));
  $('resetWorkspace').addEventListener('click',async()=>{if(!confirm('Reset this workspace? This permanently deletes locally stored photos, queue items and activity.'))return;try{const r=await api('/api/reset',{method:'POST'});state=r.state;renderAll();toast('Workspace reset');}catch(e){toast(e.message);}});
  function renderAll(){renderCounts();renderMedia();renderRecent();renderQueue();renderNotifications();$('autopilotToggle').checked=!!state.settings.autopilot;$('storyToggle').checked=!!state.settings.story;$('captionToggle').checked=state.settings.caption!==false;}
  function initDate(){const n=new Date();$('dateText').textContent=n.toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric'});$('todayLabel').textContent=n.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'}).toUpperCase();}
  async function boot(){initDate();try{state=await api('/api/state');renderAll();const hash=(location.hash||'#dashboard').slice(1);if(['dashboard','media','queue','notifications','settings'].includes(hash))navigate(hash);const url=state.instagramConnectUrl||'/login';$('connectButton').href=url;$('settingsConnect').href=url;}catch(e){toast('Could not open browser workspace storage. Check browser storage permissions and refresh.');}}
  boot();
})();
