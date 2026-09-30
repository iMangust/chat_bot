
/* ================= DATA VIEWS (users/pets/ach/merch/events/subs/notifs) ================= */
let _dashToken="";
async function ensureToken(){if(_dashToken)return _dashToken;
  try{const r=await fetch("/api/token");if(r.ok){const d=await r.json();_dashToken=d.token||"";}}catch(e){}
  return _dashToken;}
async function api(path,opts={}){await ensureToken();
  opts.headers=Object.assign({"X-Dashboard-Token":_dashToken},opts.headers||{});
  const r=await fetch(path,opts);const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||("HTTP "+r.status));return d;}
function bar(v,color){v=Math.max(0,Math.min(100,v||0));return `<div class="mbar"><i style="width:${v}%;background:${color}"></i></div>`;}
function statCard(label,value,sub){return `<div class="card"><div class="label">${label}</div><div class="value">${value}</div>${sub?`<div class="sub">${sub}</div>`:""}</div>`;}
async function loadDbStats(){
  try{
    const d=await api("/api/stats/overview");
    $("#dbStats").innerHTML=[
      statCard("Пользователей",d.usersTotal,"+"+d.usersWeek+" за неделю"),
      statCard("Забанено",d.banned),
      statCard("Питомцев",d.petsTotal),
      statCard("Достижений выдано",d.unlocks,"из "+d.achievements+" видов"),
      statCard("Подписчиков",d.subscribers),
      statCard("Мероприятий",d.events),
      `<div class="card wide"><div class="label">Топ по опыту</div>`+(d.topUsers||[]).map(u=>
        `<div class="job-row"><span>${escapeHtml(String(u.name))} (${u.tgId})</span><span>ур.${u.level} · ${u.xp} XP · 🪙${u.coins}</span></div>`).join("")+`</div>`,
    ].join("");
  }catch(e){$("#dbStats").innerHTML=`<div class="card"><div class="label">Статистика недоступна</div><div class="sub">${escapeHtml(e.message)}</div></div>`;}
}
const PAGE=50;let usersOff=0,petsOff=0;
function pager(el,total,off,cb){
  el.innerHTML="";
  if(total<=PAGE)return;
  const mk=(txt,to,dis)=>{const b=document.createElement("button");b.className="btn";b.textContent=txt;b.disabled=dis;b.onclick=()=>cb(to);return b;};
  el.append(mk("← раньше",off-PAGE,off<=0),document.createTextNode(` ${Math.min(off+1,total)}–${Math.min(off+PAGE,total)} из ${total} `),mk("позже →",off+PAGE,off+PAGE>=total));
}
async function loadUsers(){
  const q=$("#userSearch").value.trim(),sort=$("#userSort").value,ban=$("#userBanned").value;
  try{
    const d=await api(`/api/users?q=${encodeURIComponent(q)}&sort=${sort}&banned=${ban}&limit=${PAGE}&offset=${usersOff}`);
    $("#usersTotal").textContent="всего: "+d.total;
    if(!d.items.length){$("#usersTable").innerHTML='<div class="empty">нет данных</div>';$("#usersPager").innerHTML="";return;}
    $("#usersTable").innerHTML=`<table class="tbl"><tr><th>ID</th><th>Имя</th><th>Ур.</th><th>XP</th><th>🪙</th><th>Серия</th><th>Сообщ.</th><th>Питомец</th><th>Статус</th><th>Создан</th><th></th></tr>`+
      d.items.map(u=>`<tr><td>${u.tgId}</td><td>${escapeHtml(u.firstName||"")} ${u.username?"@"+escapeHtml(u.username):""}</td>
        <td>${u.level}</td><td>${u.xp}</td><td>${u.coins}</td><td>${u.streak}🔥</td><td>${u.messages}</td>
        <td>${escapeHtml(u.petName||"—")}</td>
        <td>${u.banned?'<span class="bad">бан</span>':(u.onboarded?'<span class="ok">активен</span>':'<span class="warn">онбординг</span>')}</td>
        <td>${u.createdAt||""}</td>
        <td class="rowbtns"><button class="mini" data-act="show-user" data-id="${u.tgId}">👁</button>
        <button class="mini" data-act="toggle-ban" data-id="${u.tgId}" data-val="${!u.banned}">${u.banned?"↩ разбан":"⛔ бан"}</button></td></tr>`).join("")+"</table>";
    pager($("#usersPager"),d.total,usersOff,o=>{usersOff=o;loadUsers();});
  }catch(e){toast("Пользователи: "+e.message,"err");}
}
async function showUser(id){
  try{
    const d=await api("/api/users/"+id);const u=d.user;
    let html=`<h3>${escapeHtml(u.firstName||"")} ${u.username?"@"+escapeHtml(u.username):""} <small>ID ${u.tgId}</small></h3>
      <div class="kv"><span>Уровень</span><b>${u.level}</b><span>Опыт</span><b>${u.xp} XP</b><span>Монеты</span><b>🪙 ${u.coins}</b>
      <span>Серия / рекорд</span><b>${u.streak} / ${u.bestStreak} дн.</b><span>Сообщений</span><b>${u.messages}</b>
      <span>Реакций дано / получено</span><b>${u.reactionsGiven} / ${u.reactionsReceived}</b>
      <span>Приглашено</span><b>${d.counters.invited}</b><span>Онбординг</span><b>${u.onboarded?"пройден":"не завершён"}</b>
      <span>Забанен</span><b>${u.banned?"да":"нет"}</b><span>Реферер</span><b>${u.referrerId||"—"}</b>
      <span>Регистрация</span><b>${u.createdAt}</b><span>Обновлён</span><b>${u.updatedAt}</b></div>`;
    if(d.pet){const p=d.pet;html+=`<h4>🐾 Питомец: ${escapeHtml(p.name)} (${p.species}, ${p.stage}, ур.${p.level})</h4>
      <div class="needs">${[["Голод",p.hunger,"#ffb347"],["Счастье",p.happiness,"#3ecf8e"],["Энергия",p.energy,"#7c5cff"],["Гигиена",p.hygiene,"#3ecfb2"],["Здоровье",p.health,"#ff5c7a"]].map(n=>`<div class="need"><span>${n[0]} ${n[1]}</span>${bar(n[1],n[2])}</div>`).join("")}</div>
      <div class="kv"><span>Характеристики</span><b>💪${p.strength} 🏃${p.agility} 🧠${p.intellect}</b><span>Поколение</span><b>${p.generation}</b><span>Спит</span><b>${p.sleeping?"да":"нет"}</b><span>Родился</span><b>${p.bornAt}</b></div>`;}
    else html+="<h4>Питомца нет</h4>";
    if(d.achievements&&d.achievements.length)html+=`<h4>Достижения (${d.achievements.filter(a=>a.unlockedAt).length} открыто)</h4><div class="achips">`+
      d.achievements.map(a=>`<span class="achip ${a.unlockedAt?"got":""}" title="${escapeHtml(a.title)}: прогресс ${a.progress}">${a.icon} ${escapeHtml(a.title)}${a.unlockedAt?"":" · "+a.progress}</span>`).join("")+"</div>";
    if(d.stats&&Object.keys(d.stats).length)html+=`<h4>Счётчики</h4><div class="kv">`+Object.entries(d.stats).map(([k,v])=>`<span>${escapeHtml(k)}</span><b>${v}</b>`).join("")+"</div>";
    html+=`<h4>Корректировка (осторожно)</h4><div class="editrow">
      <label>XP <input type="number" id="edXp" value="${u.xp}"></label>
      <label>Монеты <input type="number" id="edCoins" value="${u.coins}"></label>
      <label>Уровень <input type="number" id="edLevel" value="${u.level}"></label>
      <button class="btn primary" id="btnSaveUser" data-id="${u.tgId}">💾 Сохранить</button></div>`;
    modal("Карточка пользователя",html);
  }catch(e){toast("Ошибка: "+e.message,"err");}
}
async function loadPets(){
  const q=$("#petSearch").value.trim();
  try{
    const d=await api(`/api/pets?q=${encodeURIComponent(q)}&limit=${PAGE}&offset=${petsOff}`);
    $("#petsTotal").textContent="всего: "+d.total;
    if(!d.items.length){$("#petsTable").innerHTML='<div class="empty">нет данных</div>';$("#petsPager").innerHTML="";return;}
    $("#petsTable").innerHTML=`<table class="tbl"><tr><th>Кличка</th><th>Вид</th><th>Стадия</th><th>Ур.</th><th>🍖</th><th>😊</th><th>⚡</th><th>❤️</th><th>Владелец</th><th>Статус</th><th>Обновлён</th></tr>`+
      d.items.map(p=>`<tr><td>${escapeHtml(p.name)}</td><td>${p.species}</td><td>${p.stage}</td><td>${p.level}</td>
        <td>${p.hunger}</td><td>${p.happiness}</td><td>${p.energy}</td><td>${p.health}</td>
        <td>${escapeHtml(p.owner)} <small>(${p.userId})</small></td>
        <td>${p.archived?'<span class="bad">в архиве</span>':(p.sleeping?'<span class="warn">спит</span>':'<span class="ok">бодрствует</span>')}</td>
        <td>${p.lastUpdate}</td></tr>`).join("")+"</table>";
    pager($("#petsPager"),d.total,petsOff,o=>{petsOff=o;loadPets();});
  }catch(e){toast("Питомцы: "+e.message,"err");}
}
async function loadAch(){
  try{
    const d=await api("/api/achievements?with_holders="+($("#achHolders").checked?"true":"false"));
    $("#achTable").innerHTML=`<table class="tbl"><tr><th></th><th>Название</th><th>Категория</th><th>Редкость</th><th>Условие</th><th>Награда</th>${$("#achHolders").checked?"<th>Владельцев</th>":""}<th>Скрыто</th></tr>`+
      d.items.map(a=>`<tr><td style="font-size:20px">${a.icon}</td><td>${escapeHtml(a.title)}<br><small>${escapeHtml(a.description||"")}</small></td>
        <td>${a.category}</td><td>${a.rarity}</td><td>${a.conditionType} ≥ ${a.conditionValue}</td>
        <td>${a.rewardXp?a.rewardXp+" XP ":""}${a.rewardCoins?"· 🪙"+a.rewardCoins:""}</td>
        ${$("#achHolders").checked?`<td>${a.holders}</td>`:""}<td>${a.hidden?"🙈":""}</td></tr>`).join("")+"</table>";
  }catch(e){toast("Достижения: "+e.message,"err");}
}
let merchCache=null;
async function loadMerch(){
  try{
    const d=await api("/api/merch");merchCache=d.categories;
    const el=$("#merchTree");
    if(!d.categories.length){el.innerHTML='<div class="empty">каталог пуст — добавьте категорию</div>';return;}
    el.innerHTML=d.categories.map(c=>`<div class="catbox"><div class="cathead">${c.icon} <b>${escapeHtml(c.title)}</b> <small>code: ${escapeHtml(c.code)}</small>
      <span class="spacer"></span><button class="mini" data-act="del-cat" data-code="${escapeHtml(c.code)}">🗑 удалить категорию</button></div>`+
      (c.products.length?c.products.map(p=>`<div class="prodbox"><div class="prodhead">👕 <b>${escapeHtml(p.name)}</b> <small>${escapeHtml((p.description||"").slice(0,100))}</small>
        <button class="mini" data-act="add-var" data-pid="${p.id}" data-pname="${escapeHtml(p.name)}">＋ вариант</button>
        <button class="mini" data-act="del-prod" data-id="${p.id}">🗑 товар</button></div>`+
        (p.variants.length?`<table class="tbl sub-tbl"><tr><th>Размер</th><th>Цвет</th><th>Цена ₽</th><th>Остаток</th><th>Продано</th><th>Бронь</th><th></th></tr>`+
          p.variants.map(v=>`<tr><td>${escapeHtml(v.size||"—")}</td><td>${escapeHtml(v.color||"—")}</td>
          <td><input type="number" class="cellnum" id="pr_${v.id}" value="${v.priceRub}"></td>
          <td><input type="number" class="cellnum" id="st_${v.id}" value="${v.stock}"></td>
          <td>${v.soldCount}</td><td>${v.reservedBy?v.reservedBy:"—"}</td>
          <td class="rowbtns"><button class="mini" data-act="save-var" data-id="${v.id}">💾</button>
          <button class="mini" data-act="del-var" data-id="${v.id}">🗑</button></td></tr>`).join("")+"</table>":'<div class="empty small">нет вариантов (размеров/цветов)</div>')+
        `</div>`).join(""):'<div class="empty small">нет товаров в категории</div>')+`</div>`).join("");
  }catch(e){toast("Мерч: "+e.message,"err");}
}
function addVarModal(pid,pname){modal("Новый вариант для «"+pname+"»",`<div class="form"><label>Размер <input id="fv_size" placeholder="M / 56 см…"></label>
  <label>Цвет <input id="fv_color" placeholder="чёрный"></label><label>Цена, ₽ <input type="number" id="fv_price" value="0"></label>
  <label>Остаток <input type="number" id="fv_stock" value="1"></label>
  <button class="btn primary" id="btnSubmitVar" data-pid="${pid}">Добавить</button></div>`);}
function addCatModal(){modal("Новая категория мерча",`<div class="form"><label>Код (латиницей) <input id="fc_code" placeholder="clothes"></label>
  <label>Название <input id="fc_title" placeholder="Одежда"></label><label>Иконка <input id="fc_icon" value="🧢"></label>
  <label>Позиция <input type="number" id="fc_pos" value="0"></label><button class="btn primary" id="btnSubmitCat">Создать</button></div>`);}
async function addProdModal(){
  if(!merchCache)await loadMerch();
  if(!(merchCache||[]).length){toast("Сначала создайте категорию","warn");return;}
  modal("Новый товар",`<div class="form"><label>Категория <select id="fp_cat">${merchCache.map(c=>`<option value="${c.id}">${c.icon} ${escapeHtml(c.title)}</option>`).join("")}</select></label>
    <label>Название <input id="fp_name"></label><label>Описание <textarea id="fp_desc" rows="2"></textarea></label>
    <label>URL фото <input id="fp_img" placeholder="https://… (необязательно)"></label>
    <button class="btn primary" id="btnSubmitProd">Создать</button></div>`);
}
async function reservationsModal(){try{const d=await api("/api/merch/reservations");
  modal("Активные брони",d.items.length?`<table class="tbl"><tr><th>Товар</th><th>Размер</th><th>Цвет</th><th>Кем забронировано</th><th>Когда</th></tr>`+
    d.items.map(r=>`<tr><td>${escapeHtml(r.productName)}</td><td>${escapeHtml(r.size||"—")}</td><td>${escapeHtml(r.color||"—")}</td><td>${escapeHtml(String(r.reservedByTitle||r.reservedBy||""))}</td><td>${r.reservedAt||""}</td></tr>`).join("")+"</table>":'<div class="empty">броней нет</div>');
}catch(e){toast(e.message,"err");}}
async function loadEvents(){
  try{
    const d=await api("/api/events");window.__events=d.items;
    $("#eventsCount").textContent="всего: "+d.items.length;
    if(!d.items.length){$("#eventsTable").innerHTML='<div class="empty">мероприятий нет — создайте первое</div>';return;}
    $("#eventsTable").innerHTML=`<table class="tbl"><tr><th></th><th>Название</th><th>Дата</th><th>Время</th><th>Место</th><th>Сбор</th><th>Идут</th><th>Ссылка</th><th></th></tr>`+
      d.items.map(e=>`<tr><td style="font-size:20px">${e.icon}</td><td>${escapeHtml(e.title)}${e.description?`<br><small>${escapeHtml(e.description.slice(0,120))}</small>`:""}</td>
        <td>${e.date||"—"}</td><td>${e.time||"—"}</td><td>${escapeHtml(e.place||"—")}</td><td>${escapeHtml(e.meet||"—")}</td>
        <td>${e.goingCount}</td><td>${e.url?`<a href="${escapeHtml(e.url)}" target="_blank" rel="noopener">открыть</a>`:"—"}</td>
        <td class="rowbtns"><button class="mini" data-act="edit-event" data-id="${e.id}">✏️</button>
        <button class="mini" data-act="del-event" data-id="${e.id}">🗑</button></td></tr>`).join("")+"</table>";
  }catch(e){toast("Мероприятия: "+e.message,"err");}
}
function eventForm(ev){ev=ev||{};return `<div class="form grid2">
  <label>Название* <input id="fe_title" value="${escapeHtml(ev.title||"")}"></label>
  <label>Иконка <input id="fe_icon" value="${escapeHtml(ev.icon||"🎪")}"></label>
  <label>Дата (ГГГГ-ММ-ДД) <input id="fe_date" value="${escapeHtml(ev.date||"")}" placeholder="2026-10-05"></label>
  <label>Время <input id="fe_time" value="${escapeHtml(ev.time||"")}" placeholder="18:00"></label>
  <label>Место <input id="fe_place" value="${escapeHtml(ev.place||"")}"></label>
  <label>Точка сбора <input id="fe_meet" value="${escapeHtml(ev.meet||"")}"></label>
  <label>Ссылка <input id="fe_url" value="${escapeHtml(ev.url||"")}" placeholder="https://…"></label>
  <label>Фото URL <input id="fe_img" value="${escapeHtml(ev.imageUrl||"")}"></label>
  <label class="full">Описание <textarea id="fe_desc" rows="3">${escapeHtml(ev.description||"")}</textarea></label>
  </div>`;}
function editEventModal(id){const ev=(window.__events||[]).find(x=>x.id===id)||{};
  modal("Изменить мероприятие #"+id,eventForm(ev)+`<button class="btn primary" id="btnSubmitEvent" data-id="${id}">💾 Сохранить</button>`);}
function addEventModal(){modal("Новое мероприятие",eventForm()+`<button class="btn primary" id="btnSubmitEvent" data-id="">Создать</button>`);}
async function loadSubs(){
  try{
    const d=await api("/api/subscribers?limit=200");
    $("#subsTotal").textContent="всего: "+d.total;
    $("#subsTable").innerHTML=d.items.length?`<table class="tbl"><tr><th>ID</th><th>Имя</th><th>Чаты</th><th>Контакт с ботом</th><th>Первая встреча</th><th>Последняя активность</th></tr>`+
      d.items.map(r=>`<tr><td>${r.userId}</td><td>${escapeHtml(r.firstName||"")} ${r.username?"@"+escapeHtml(r.username):""}</td>
      <td>${(r.chats||[]).join(", ")||"—"}</td><td>${r.everContacted?"✔":"—"}</td><td>${r.firstSeen||""}</td><td>${r.lastSeen||""}</td></tr>`).join("")+"</table>"
      :'<div class="empty">нет данных</div>';
  }catch(e){toast("Подписчики: "+e.message,"err");}
}
async function loadNotifs(){
  try{
    const d=await api("/api/notifications");
    $("#notifsTable").innerHTML=d.items.length?`<table class="tbl"><tr><th>#</th><th>Кому</th><th>Тип</th><th>Текст</th><th>Когда</th><th>Отправлено</th></tr>`+
      d.items.map(n=>`<tr><td>${n.id}</td><td>${n.userId}</td><td>${escapeHtml(n.kind)}</td><td>${escapeHtml(n.text)}</td><td>${n.sendAt}</td><td>${n.sent?'<span class="ok">да</span>':'<span class="warn">в очереди</span>'}</td></tr>`).join("")+"</table>"
      :'<div class="empty">очередь пуста</div>';
  }catch(e){toast("Очередь: "+e.message,"err");}
}
function modal(title,html){
  closeModal();
  const bg=document.createElement("div");bg.id="modalBg";bg.className="modal-bg";
  bg.innerHTML=`<div class="modal"><div class="modal-head"><b>${title}</b><button class="mini" id="modalX">✕</button></div><div class="modal-body">${html}</div></div>`;
  document.body.appendChild(bg);
  bg.addEventListener("click",e=>{if(e.target===bg)closeModal();});
  $("#modalX").onclick=closeModal;
}
function closeModal(){const m=$("#modalBg");if(m)m.remove();}
window.addEventListener("keydown",e=>{if(e.key==="Escape")closeModal();});
document.body.addEventListener("click",async e=>{
  const b=e.target.closest("[data-act]");if(!b)return;
  const act=b.dataset.act,id=b.dataset.id;
  try{
    if(act==="show-user")await showUser(id);
    else if(act==="toggle-ban"){await api(`/api/users/${id}/ban`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({banned:b.dataset.val==="true"})});toast(b.dataset.val==="true"?"Пользователь забанен":"Бан снят");loadUsers();}
    else if(act==="del-cat"){if(confirm("Удалить категорию со всеми товарами?")){await api("/api/merch/categories/"+encodeURIComponent(b.dataset.code),{method:"DELETE"});toast("Категория удалена");loadMerch();}}
    else if(act==="del-prod"){if(confirm("Удалить товар со всеми вариантами?")){await api("/api/merch/products/"+id,{method:"DELETE"});toast("Товар удалён");loadMerch();}}
    else if(act==="del-var"){if(confirm("Удалить вариант?")){await api("/api/merch/variants/"+id,{method:"DELETE"});toast("Вариант удалён");loadMerch();}}
    else if(act==="save-var"){await api("/api/merch/variants/"+id,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({price_rub:+$("#pr_"+id).value,stock:+$("#st_"+id).value})});toast("Сохранено ✔");}
    else if(act==="add-var")addVarModal(id,b.dataset.pname);
    else if(act==="edit-event")editEventModal(+id);
    else if(act==="del-event"){if(confirm("Удалить мероприятие?")){await api("/api/events/"+id,{method:"DELETE"});toast("Удалено");loadEvents();}}
  }catch(err){toast(err.message,"err");}
});
document.body.addEventListener("click",async e=>{
  const b=e.target.closest("#btnSubmitVar,#btnSubmitCat,#btnSubmitProd,#btnSubmitEvent,#btnSaveUser");if(!b)return;
  try{
    if(b.id==="btnSubmitVar"){await api("/api/merch/variants",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({product_id:+b.dataset.pid,size:$("#fv_size").value.trim(),color:$("#fv_color").value.trim(),price_rub:+$("#fv_price").value,stock:+$("#fv_stock").value})});
      closeModal();toast("Вариант добавлен");loadMerch();}
    else if(b.id==="btnSubmitCat"){await api("/api/merch/categories",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({code:$("#fc_code").value.trim(),title:$("#fc_title").value.trim(),icon:$("#fc_icon").value.trim()||"🧢",position:+$("#fc_pos").value})});
      closeModal();toast("Категория создана");loadMerch();}
    else if(b.id==="btnSubmitProd"){await api("/api/merch/products",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({category_id:+$("#fp_cat").value,name:$("#fp_name").value.trim(),description:$("#fp_desc").value.trim(),image_url:$("#fp_img").value.trim()||null})});
      closeModal();toast("Товар создан");loadMerch();}
    else if(b.id==="btnSubmitEvent"){
      const body={title:$("#fe_title").value.trim(),date:$("#fe_date").value.trim(),time:$("#fe_time").value.trim(),
        place:$("#fe_place").value.trim(),meet:$("#fe_meet").value.trim(),description:$("#fe_desc").value.trim(),
        url:$("#fe_url").value.trim()||null,image_url:$("#fe_img").value.trim()||null,icon:$("#fe_icon").value.trim()||"🎪"};
      if(b.dataset.id)await api("/api/events/"+b.dataset.id,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      else await api("/api/events",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      closeModal();toast(b.dataset.id?"Сохранено ✔":"Создано ✔");loadEvents();}
    else if(b.id==="btnSaveUser"){await api(`/api/users/${b.dataset.id}/edit`,{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({xp:+$("#edXp").value,coins:+$("#edCoins").value,level:+$("#edLevel").value})});
      toast("Сохранено ✔");closeModal();loadUsers();}
  }catch(err){toast(err.message,"err");}
});
const LOADERS={dash:loadDbStats,users:loadUsers,pets:loadPets,ach:loadAch,merch:loadMerch,events:loadEvents,subs:loadSubs,notifs:loadNotifs};
document.querySelectorAll(".nav-item").forEach(n=>n.addEventListener("click",()=>{
  const v=n.dataset.view;if(LOADERS[v]&&!state.loadedViews[v]){state.loadedViews[v]=true;LOADERS[v]();}
}));
$("#usersRefresh").onclick=loadUsers;$("#petsRefresh").onclick=loadPets;$("#achRefresh").onclick=loadAch;
$("#mcRefresh").onclick=loadMerch;$("#evRefresh").onclick=loadEvents;$("#subsRefresh").onclick=loadSubs;$("#notifsRefresh").onclick=loadNotifs;
$("#mcAddCat").onclick=addCatModal;$("#mcAddProd").onclick=addProdModal;$("#mcResv").onclick=reservationsModal;$("#evAdd").onclick=addEventModal;
$("#userSearch").addEventListener("keydown",e=>{if(e.key==="Enter"){usersOff=0;loadUsers();}});
$("#petSearch").addEventListener("keydown",e=>{if(e.key==="Enter"){petsOff=0;loadPets();}});
$("#userSort").onchange=loadUsers;$("#userBanned").onchange=loadUsers;$("#achHolders").onchange=loadAch;
loadDbStats();
