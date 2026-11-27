import {ROLE, STATE, PURPOSE, SHEETS, SPECS, LABELS, CSV_KINDS, measure, menu, string, toggle,
        centered, anchorPoints, extendSettings} from "./tools.js";

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const ns = "http://www.w3.org/2000/svg";
const app = {
  view:"studio", pane:"analyze", tool:"color", firings:[], firing:null, photos:[], entries:[],
  photo:null, region:null, points:[], draw:"box", dragging:null, zoom:1,
  tray:null, inspected:null, picks:[], kept:new Set(), output:null, lookup:null, busy:0, load:0,
};

function node(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(options)) {
    if (value === undefined || value === null) continue;
    if (key === "class") element.className = value;
    else if (key === "text") element.textContent = value;
    else if (key === "on") Object.entries(value).forEach(([type, fn])=>element.addEventListener(type, fn));
    else if (key === "dataset") Object.assign(element.dataset, value);
    else if (key in element) element[key] = value;
    else element.setAttribute(key, value);
  }
  element.append(...children);
  return element;
}

function button(label, handler, kind = "secondary", id) {
  return node("button", {text:label,type:"button",class:kind,id,on:{click:handler}});
}

function link(label, href, kind = "secondary small-button", other = {}) {
  return node("a", {text:label,href,class:kind,...other});
}

function notice(message, error = false) {
  const target = $("#notice");
  target.textContent = message;
  target.dataset.error = error;
  target.hidden = false;
  clearTimeout(notice.timer);
  notice.timer = setTimeout(()=>target.hidden=true, error ? 6600 : 4800);
}

async function api(path, {method="GET", data, raw=false}={}) {
  const options = {method};
  if (data instanceof FormData) options.body = data;
  else if (data !== undefined) {
    options.headers = {"Content-Type":"application/json"};
    options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  if (!response.ok) {
    let description = "请求失败，请重试";
    if ((response.headers.get("content-type") || "").includes("json")) description = (await response.json()).error || description;
    throw new Error(description);
  }
  if (raw) return response.blob();
  if (response.status === 204) return null;
  return response.json();
}

async function action(event, callback, success) {
  const pressed = event?.currentTarget;
  if (pressed) pressed.disabled = true;
  app.busy++;
  document.body.setAttribute("aria-busy","true");
  try {
    const outcome = await callback();
    if (success) notice(typeof success === "function" ? success(outcome) : success);
    return outcome;
  } catch (error) {
    notice(error.message, true);
    return null;
  } finally {
    app.busy--;
    document.body.setAttribute("aria-busy",String(Boolean(app.busy)));
    if (pressed) pressed.disabled = false;
  }
}

function optionList(el, options, value) {
  el.replaceChildren(...Object.entries(options).map(([key,label])=>node("option",{value:key,text:label})));
  if (value !== undefined) el.value = value;
}

function makeField(spec) {
  let input;
  if (spec.type === "select" || spec.type === "photo") {
    input = node("select",{name:spec.key,id:spec.id,required:spec.required, "aria-label":spec.label});
    const choice = spec.type === "photo"
      ? Object.fromEntries(app.photos.filter(p=>p.id!==app.photo?.id).map(p=>[p.id,p.title])) : spec.options;
    optionList(input, choice, spec.value);
  } else if (spec.type === "textarea") {
    input = node("textarea",{name:spec.key,id:spec.id,value:spec.value||"",rows:spec.rows||3, "aria-label":spec.label});
  } else {
    input = node("input",{name:spec.key,id:spec.id,type:spec.type,value:spec.type==="checkbox"?"on":spec.value,
      checked:spec.type==="checkbox"&&spec.value,min:spec.min,max:spec.max,step:spec.step,
      required:spec.required,placeholder:spec.placeholder,accept:spec.accept, "aria-label":spec.label});
  }
  const label = node("label",{class:(spec.full?"full ":"")+(spec.type==="checkbox"?"check-field":"field")},
    [node("span",{text:spec.label}),input]);
  return {label,input};
}

function formFields(specs) {
  const fields = specs.map(makeField);
  return {
    element:node("div",{class:"form-grid"},fields.map(field=>field.label)),
    inputs:Object.fromEntries(fields.map((field,index)=>[specs[index].key,field.input])),
    read:()=>Object.fromEntries(fields.map(({input})=>[input.name,input.type==="checkbox"?input.checked:
      input.type==="number"?Number(input.value):input.value])),
  };
}

function details(label, child) {
  return node("details", {}, [node("summary",{text:label}),child]);
}
function paragraph(text, kind="help") {return node("p",{class:kind,text});}
function buttonRow(...buttons) {return node("div",{class:"button-row"},buttons);}
function apiFiring(suffix="") {return "/api/firings/"+app.firing.id+suffix;}
function apiPhoto(suffix="") {return "/api/photos/"+app.photo.id+suffix;}
function photoURL(photo, thumb=false) {return "/api/photos/"+photo.id+"/image"+(thumb?"?preview=1":"");}
function boundsFill() {return [0,0,app.photo.width,app.photo.height];}

function selectedTray() {return app.entries.find(entry=>entry.kind==="tray"&&entry.photo_id===app.photo?.id) || null;}

function drawFilm() {
  const query = $("#photo-search").value.trim().toLocaleLowerCase();
  const role = $("#photo-role").value;
  const filtered = app.photos.filter(photo=>(!role||role===photo.role) &&
    (!query||[photo.title,photo.note].some(s=>s.toLocaleLowerCase().includes(query))));
  $("#photo-count").textContent = filtered.length;
  $("#photo-list").replaceChildren(...filtered.map(photo=>{
    const thumb = node("img",{src:photoURL(photo,true),alt:"",loading:"lazy"});
    const info = node("div",{},[node("strong",{text:photo.title}),node("small",{text:ROLE[photo.role]||photo.role})]);
    return node("button",{type:"button",class:"photo-tile"+(photo.id===app.photo?.id?" active":""),
      "aria-label":"选择 "+photo.title, "aria-pressed":photo.id===app.photo?.id,
      on:{click:()=>selectPhoto(photo.id)}},[thumb,info]);
  }));
  if (!filtered.length) $("#photo-list").append(paragraph(app.photos.length?"没有匹配的照片":"当前窑次暂无照片","record-empty"));
}

function framePhoto() {
  const photo = app.photo;
  const img = $("#source-image");
  $("#empty-photo").hidden = Boolean(photo);
  $("#image-wrap").hidden = !photo;
  $("#edit-photo").disabled = !photo;
  $("#photo-title").textContent = photo?.title || "原照";
  $("#photo-meta").textContent = photo ? [photo.width+" × "+photo.height,ROLE[photo.role],photo.original_format].join("  ·  ") : "";
  const original = $("#original-link");
  original.hidden = !photo;
  if (!photo) {
    img.removeAttribute("src"); $("#drawing").replaceChildren(); return;
  }
  if (img.getAttribute("src")!==photoURL(photo)) img.src = photoURL(photo);
  img.alt = photo.title;
  original.href = photoURL(photo);
  drawMarkup();
}

function framing(firings) {
  const current = app.firing;
  optionList($("#firing"),Object.fromEntries(firings.map(entry=>[entry.id,entry.name])),current?.id);
  $("#firing-name").textContent = current?.name || "新建窑次档案";
  $("#firing-caption").textContent = current
    ? [current.kiln,current.fired_on,current.notes].filter(Boolean).join("  ·  ") : "为窑后照片和试片记录建立一个目录。";
  $("#edit-firing").disabled = !current;
  $("#firing").disabled = !firings.length;
  $("#upload-open").disabled = !current;
}

function redraw() {
  framing(app.firings); drawFilm(); framePhoto(); drawTools(); drawRecords(); drawLibrary();
}

async function reload(firingId = app.firing?.id, photoId = app.photo?.id, {full=true}={}) {
  const stamp = ++app.load;
  const list = full ? (await api("/api/firings")).firings : app.firings;
  const active = list.some(f=>f.id===firingId) ? firingId : list[0]?.id;
  let data = {firing:null,photos:[],entries:[]};
  if (active) data = await api("/api/firings/"+active);
  if (stamp!==app.load) return;
  const newFiring = app.firing?.id!==active;
  const oldPhoto = app.photo?.id;
  [app.firings,app.firing,app.photos,app.entries] = [list,data.firing,data.photos,data.entries];
  if (newFiring) {
    app.picks=[]; app.kept.clear(); app.lookup=null; app.inspected=null; hideOutput();
  }
  app.photo = app.photos.find(value=>value.id===photoId) || app.photos[0] || null;
  app.tray = selectedTray();
  if (oldPhoto!==app.photo?.id) {
    app.region=null; app.points=[]; app.inspected=null; app.zoom=1;
  }
  redraw();
}

function selectPhoto(id) {
  if (id===app.photo?.id) return;
  app.photo=app.photos.find(photo=>photo.id===id) || null;
  app.tray=selectedTray(); app.inspected=null; app.region=null; app.points=[]; app.zoom=1;
  drawFilm(); framePhoto(); drawTools(); hideOutput();
}

function setView(view) {
  app.view=view;
  for (const key of ["studio","records","library"]) $("#"+key).hidden=key!==view;
  $$(".top-tab").forEach(item=>item.classList.toggle("active",item.dataset.view===view));
  hideOutput();
  if (view==="records") drawRecords();
  if (view==="library") drawLibrary();
}

function drawMode(mode) {
  app.draw=mode;
  $$(".draw-mode").forEach(button=>button.classList.toggle("active",button.dataset.draw===mode));
  drawMarkup();
}

function svg(tag, values) {
  const rendered = document.createElementNS(ns,tag);
  for (const [name,value] of Object.entries(values)) rendered.setAttribute(name,value);
  return rendered;
}
function renderPoly(points) {return points.map(p=>p.join(",")).join(" ");}
function detailsForRegion(region) {
  if (!region) return "";
  const size = [region[2]-region[0],region[3]-region[1]].map(Math.round);
  return "样区 "+size.join(" × ")+" px";
}
function projectedCell(tray, cell) {
  const data=tray.payload;
  const [x,y]=data.origin;
  const [c,d]=data.last_column;
  const [a,b]=data.last_row;
  return [[0,0],[1,0],[1,1],[0,1]].map(([dx,dy])=>{
    const col=(cell.column+dx)/data.columns, row=(cell.row+dy)/data.rows;
    return [x+(c-x)*col+(a-x)*row,y+(d-y)*col+(b-y)*row];
  });
}
function drawMarkup() {
  const image=$("#drawing"); const frame=$("#image-wrap");
  frame.style.width=(app.zoom*100)+"%";
  frame.style.maxWidth=app.zoom>1?"none":"910px";
  $("#zoom-value").textContent=app.zoom===1?"适合窗口":Math.round(app.zoom*100)+"%";
  if (!app.photo) return;
  image.setAttribute("viewBox",`0 0 ${app.photo.width} ${app.photo.height}`);
  image.setAttribute("preserveAspectRatio","none");
  image.replaceChildren();
  if (app.pane==="tray" && app.tray) {
    for (const cell of app.tray.payload.cells) {
      const color={identified:"#80d9aa",unread:"#f5c169",empty:"#d1c6b6",covered:"#ef956b"}[cell.state];
      const marked=cell.address===app.inspected?.observation.address;
      const poly=projectedCell(app.tray,cell);
      image.append(svg("polygon",{points:renderPoly(poly),stroke:color,"stroke-width":marked?2.5:1.1,
        fill:color,"fill-opacity":marked ? .17 : .018}));
    }
  }
  if (app.region) {
    const [a,b,c,d]=app.region;
    image.append(svg("rect",{x:a,y:b,width:c-a,height:d-b,fill:"#ffd395","fill-opacity":.11,stroke:"#ffd38b",
      "stroke-width":2,"stroke-dasharray":"5 4"}));
  }
  if (app.points.length > 1) {
    image.append(svg("polyline",{points:renderPoly(app.points),fill:"none",stroke:"#ffe0a0","stroke-width":1.5}));
  }
  for (const [index,point] of app.points.entries()) {
    const [x,y]=point, radius=Math.max(3,app.photo.width/155);
    image.append(svg("circle",{cx:x,cy:y,r:radius,fill:"#f2b361",stroke:"#33281e","stroke-width":1}));
    const label = svg("text",{x:x+1.55*radius,y:y-.65*radius,fill:"#fff1cf","font-size":radius*3});
    label.textContent=String(index+1);
    image.append(label);
  }
  $("#selection-brief").textContent = [detailsForRegion(app.region),app.points.length?app.points.length+" 点":""].filter(Boolean).join("  ·  ")
    || (app.draw==="box"?"拖动框出样区":"按顺序点击照片标点");
}
function imagePoint(event) {
  const b=$("#drawing").getBoundingClientRect();
  return [Math.round(Math.max(0,Math.min(app.photo.width,(event.clientX-b.left)/b.width*app.photo.width))),
    Math.round(Math.max(0,Math.min(app.photo.height,(event.clientY-b.top)/b.height*app.photo.height)))];
}
$("#drawing").addEventListener("pointerdown", event=>{
  if (!app.photo || event.button!==0) return;
  app.dragging={start:imagePoint(event),id:event.pointerId};
  event.currentTarget.setPointerCapture(event.pointerId);
});
$("#drawing").addEventListener("pointermove", event=>{
  if (!app.photo) return;
  const p=imagePoint(event);
  $("#position").textContent=`X ${p[0]}  /  Y ${p[1]}`;
  if (app.dragging && app.draw==="box") {
    const [x,y]=app.dragging.start;
    app.region=[Math.min(x,p[0]),Math.min(y,p[1]),Math.max(x,p[0]),Math.max(y,p[1])];
    drawMarkup();
  }
});
$("#drawing").addEventListener("pointerup", event=>{
  if (!app.dragging) return;
  const point=imagePoint(event);
  const [x,y]=app.dragging.start;
  app.dragging=null;
  if (Math.hypot(point[0]-x,point[1]-y)<4 && app.pane==="tray" && app.tray && app.draw==="box") {
    action(null,()=>inspectTray({point}));
    return;
  }
  if (app.draw==="points") {
    if (app.points.length<40) app.points.push(point);
  } else if (app.region && (app.region[2]-app.region[0]<3 || app.region[3]-app.region[1]<3)) app.region=null;
  drawMarkup();
});
$("#drawing").addEventListener("pointercancel",()=>{app.dragging=null;});

function togglePane(name) {
  app.pane=name;
  $$(".inspector-tabs button").forEach(item=>item.classList.toggle("active",item.dataset.pane===name));
  drawTools(); drawMarkup(); hideOutput();
}
function drawTools() {
  $("#picks-count").textContent=app.picks.length?String(app.picks.length):"";
  if (!app.firing) {
    $("#tool-body").replaceChildren(paragraph("新建窑次后可使用检视工具。"));
    return;
  }
  if (app.pane==="cards") return drawCards();
  if (!app.photo) {$("#tool-body").replaceChildren(paragraph("选择照片后开始测量。"));return;}
  if (app.pane==="tray") return drawTray();
  drawAnalysis();
}

function parseExtra(input, built) {
  const value = input.value.trim();
  if (!value) return built;
  let details;
  try {details=JSON.parse(value);} catch {throw new Error("高级设置必须为有效 JSON");}
  if (!details || typeof details!=="object" || Array.isArray(details)) throw new Error("高级设置需要是一份参数对象");
  return {...built,...details};
}

function drawAnalysis() {
  const spec=SPECS[app.tool];
  const selection=node("select",{id:"analysis-kind","aria-label":"测量工具"});
  for (const [label, entries] of Object.entries(SHEETS)) {
    const group=node("optgroup",{label});
    optionList(group,Object.fromEntries(Object.entries(entries).map(([key,value])=>[key,value.label])));
    selection.append(group);
  }
  selection.value=app.tool;
  selection.addEventListener("change",()=>{
    app.tool=selection.value;
    if (["marks","spot","mask","line","points","rectify","layer"].includes(SPECS[app.tool].type)) drawMode("points");
    drawAnalysis();
  });
  const form=formFields([...spec.fields,toggle("calibrated","采用已归档校色",true)]);
  const prefs=node("textarea",{id:"analysis-json",rows:8,"aria-label":"分析高级设置",
    placeholder:"从页面输入生成参数，或在此粘贴 JSON 对象"});
  const build=()=>extendSettings(app.tool,form.read(),app.photo,app.photos,app.region,app.points);
  const settings=()=>{
    const current = parseExtra(prefs,build());
    if (form.inputs.calibrated.dataset.selection) current.selection=form.inputs.calibrated.dataset.selection;
    return current;
  };
  const controls=[
    node("label",{class:"tool-select"},[node("span",{text:"测量工具"}),selection]),paragraph(spec.help),form.element,
  ];
  if (spec.json) controls.push(details("高级设置",(node("div",{class:"tool-body"},[
    button("将当前选择写入参数",()=>{prefs.value=JSON.stringify(build(),null,2);},"secondary small-button"),
    prefs,
  ]))));
  const run=button("运行检视",event=>action(event,async()=>{
    const photoId=app.photo.id, mode=app.tool, input=settings();
    const result=await api(apiPhoto("/analysis/"+mode),{method:"POST",data:input});
    if (app.photo?.id!==photoId || app.tool!==mode) return;
    app.output={...result,heading:SPECS[mode].label,data:result.analysis.result};
    showOutput(app.output);
  }),"primary","run-analysis");
  const save=button("归档结果",event=>action(event,async()=>{
    const photoId=app.photo.id, mode=app.tool;
    const stored=await api(apiPhoto("/analysis/"+mode+"/save"),{method:"POST",data:{settings:settings()}});
    await reload(app.firing.id,photoId);
    const result=stored.entry.payload.result;
    showOutput({heading:stored.entry.title,data:result,entry:stored.entry,preview:stored.entry.artifact_sha?entryArtifact(stored.entry):null});
  },"检视结果已归档"),"secondary","save-analysis");
  const archiveSelection=button("保留选取位置",saveSelectionPopup,"quiet small-button");
  const locations=app.entries.filter(e=>e.kind==="selection"&&e.photo_id===app.photo.id);
  if (locations.length) {
    const saved = formFields([menu("selection","从保留位置取样",{"":"用当前选择",...Object.fromEntries(locations.map(e=>[e.id,e.title]))},"")]);
    saved.inputs.selection.addEventListener("change",()=>form.inputs.calibrated.dataset.selection=saved.inputs.selection.value);
    controls.push(saved.element);
  }
  controls.push(buttonRow(run,save,archiveSelection));
  $("#tool-body").replaceChildren(...controls);
}

function saveSelectionPopup() {
  if (!app.region && !app.points.length) {notice("先在照片上框选或标点",true);return;}
  const form=formFields([string("name","位置名称",""),menu("category","位置用途",
    {routine:"日常检视",control:"试片对照",attention:"待复查"},"routine"),
    menu("shape","位置形状",{region:"框选样区",points:"标记点列"},app.region?"region":"points"),
    {...string("note","位置备注",""),full:true}]);
  openDialog("保留影像位置",form.element,buttonRow(button("保存位置",event=>action(event,async()=>{
    const received=await api(apiPhoto("/selections"),{method:"POST",data:{...form.read(),region:app.region,points:app.points}});
    closeDialog();
    await reload(app.firing.id,app.photo.id);
    showEntry(received.entry);
  },"影像位置已归档"),"primary")));
}

async function inspectTray(selection) {
  const trayId=app.tray?.id;
  if (!trayId) return;
  const result=await api("/api/trays/"+trayId+"/inspect",{method:"POST",data:selection});
  if (app.tray?.id!==trayId) return;
  app.inspected=result;
  drawTray(); drawMarkup();
}

function traySettingsPopup() {
  const current=app.tray?.payload;
  const form=formFields([
    {...string("name","托盘名",current?.name || app.photo.title),full:true,required:true},
    measure("rows","行数",current?.rows||3,1,12,1),measure("columns","列数",current?.columns||4,1,12,1),
    {...string("note","抄录备注",current?.note||""),full:true},toggle("backlit","背光照片",current?.backlit||false),
    {...string("anchors","左上、右上、左下三点（JSON）",JSON.stringify(app.points.length>=3?app.points.slice(0,3):
      current?[current.origin,current.last_column,current.last_row]:anchorPoints(app.photo))),full:true}
  ]);
  const save=button("保存托盘",ev=>action(ev,async()=>{
    const photoId=app.photo.id;
    const values=form.read();
    let anchors;
    try {anchors=JSON.parse(values.anchors);} catch {throw new Error("参照点需要三个 [x,y] 数组");}
    if (!Array.isArray(anchors)||anchors.length!==3) throw new Error("参照点需要三个 [x,y] 数组");
    const existing=app.tray;
    const cells=existing?.payload.cells.filter(cell=>cell.row<values.rows && cell.column<values.columns) || [];
    if (existing && cells.length<existing.payload.cells.length &&
        !window.confirm("缩小网格会舍去边缘抄录，确认保存？")) return;
    const {name,rows,columns,note,backlit}=values;
    await api(apiPhoto("/tray"),{method:"PUT",data:{name,rows,columns,note,backlit,cells,
      origin:anchors[0],last_column:anchors[1],last_row:anchors[2],revision:existing?.revision}});
    closeDialog();
    app.inspected=null;
    await reload(app.firing.id,photoId);
  },"托盘已保存"),"primary");
  openDialog("托盘网格",form.element,paragraph("角点可以先在原照上按顺序标出，再打开此窗口。"),buttonRow(save));
}

function drawTray() {
  const tray=app.tray;
  const wrap=$("#tool-body");
  const def=button(tray?"编辑网格":"建立网格",traySettingsPopup,"secondary small-button","tray-grid");
  if (!tray) {wrap.replaceChildren(paragraph("用三个参照点建立单张照片的托盘网格，再逐格抄录。"),def);return;}
  const row=node("div",{class:"block-title"},[node("h3",{text:tray.title}),def]);
  const status=tray.payload.counts;
  const stats=paragraph(`已抄 ${status.identified} · 待抄 ${status.unread} · 空格 ${status.empty} · 遮挡 ${status.covered}`);
  const grid=node("div",{class:"sample-row","aria-label":"托盘格"});
  grid.style.setProperty("--tray-columns",tray.payload.columns);
  for (const cell of tray.payload.cells) {
    const a=node("button",{type:"button",class:"cell-button"+(cell.address===app.inspected?.observation.address?" selected":""),
      "aria-label":"检视 "+cell.address,dataset:{state:cell.state},on:{click:event=>action(event,()=>inspectTray(cell))}},
      [node("strong",{text:cell.address}),node("span",{text:cell.coupon||STATE[cell.state]})]);
    grid.append(a);
  }
  const appLinks=buttonRow(link("托盘接触片","/api/trays/"+tray.id+"/sheet","secondary small-button",{target:"_blank",rel:"noopener"}),
    link("下载网格","/api/trays/"+tray.id+"/outlines","secondary small-button",{download:"tray-outlines.svg"}));
  wrap.replaceChildren(row,stats,grid);
  if (app.inspected) wrap.append(cellEditor(tray));
  wrap.append(appLinks);
}

function cellEditor(tray) {
  const pick=app.inspected.observation;
  const form=formFields([menu("state","抄录状态",STATE,pick.state),string("coupon","实物编号",pick.coupon),
    {...string("note","格备注",pick.note),full:true}]);
  const section=node("fieldset",{class:"fieldset"},[node("legend",{text:`${pick.address} · 实底 ${Math.round(pick.quality.coverage*100)}%`}),
    node("img",{class:"mini-photo",src:app.inspected.preview,alt:pick.address+" 格照片"}),form.element]);
  section.append(buttonRow(
    button("保存抄录",event=>action(event,async()=>{
      const values=form.read();
      if (values.state!=="identified") values.coupon="";
      const photoId=app.photo.id;
      await api(`/api/trays/${tray.id}/cells/${pick.row}/${pick.column}`,{method:"PUT",data:{...values,revision:tray.revision}});
      await reload(app.firing.id,photoId);
      await inspectTray({row:pick.row,column:pick.column});
    },"格子抄录已保存"),"primary small-button"),
    button("加入留样卡",()=>addPick(tray,pick),"secondary small-button","pick-current"),
  ));
  return section;
}

function addPick(tray,pick) {
  if (app.picks.some(item=>item.tray===tray.id && item.row===pick.row && item.column===pick.column)) {
    notice("此格已加入留样卡",true);return;
  }
  if (app.picks.length>=26) {notice("一套留样卡最多26格",true);return;}
  app.picks.push({tray:tray.id,revision:tray.revision,row:pick.row,column:pick.column,
    label:pick.coupon||"待核",address:pick.address,purpose:"retention",name:tray.title});
  $("#picks-count").textContent=String(app.picks.length);
  notice(`已加入 ${tray.title} ${pick.address}`);
}
function paperFields() {
  return [menu("format","纸张",{a4:"A4",letter:"Letter",a5:"A5"},"a4"),
    menu("orientation","方向",{portrait:"纵向",landscape:"横向"},"portrait"),
    measure("columns","每行列数",2,1,4,1),measure("row_size","卡片高度 mm",55,37,108)];
}
function drawCards() {
  const tools=$("#tool-body");
  if (!app.picks.length) {
    tools.replaceChildren(paragraph("从托盘抄录中选一格并加入留样卡。可组合当前窑次多张照片的实物格。"),
      button("前往托盘抄录",()=>togglePane("tray"),"secondary"));
    return;
  }
  const types=formFields([menu("edition","制卡类型",{review:"待核样卡",issue:"正式签发"},"review"),
    string("title","制卡名目","釉样实物留样卡"),...paperFields()]);
  const picked=node("ul",{class:"pick-list"});
  for (const [index,item] of app.picks.entries()) {
    const choice=makeField(menu("purpose","保留目的",PURPOSE,item.purpose)).input;
    choice.addEventListener("change",()=>item.purpose=choice.value);
    const remove=button("移除",()=>{app.picks.splice(index,1);drawTools();},"quiet small-button");
    const changed=app.entries.find(e=>e.id===item.tray)?.revision!==item.revision;
    const label=node("span",{text:`${item.name} ${item.address} · ${item.label}`+(changed?" · 抄录已更新":"")});
    picked.append(node("li",{class:"list-item"},[label,remove]),node("li",{},[choice]));
  }
  const run=(event,save)=>action(event,async()=>{
    const {title,edition,...paper}=types.read();
    const picks=app.picks.map(({tray,revision,row,column,purpose})=>({tray,revision,row,column,purpose}));
    const data=await api(apiFiring("/cards"),{method:"POST",data:{title,edition,paper,picks,save}});
    if (save) await reload(app.firing.id,app.photo?.id);
    showOutput({heading:save?"留样卡已归档":"留样卡预览",data:data.result,html:data.html,entry:data.entry});
  },save?"留样卡已归档":null);
  tools.replaceChildren(picked,types.element,buttonRow(
    button("预览留样卡",event=>run(event,false),"primary","preview-cards"),
    button("归档留样卡",event=>run(event,true),"secondary","archive-cards")));
}

const METRICS = {
  swatch:"色样",calibrated:"已校色",coverage_px:"有效面积 px²",coverage_fraction:"实底比例",
  count:"计数",area_px:"选中面积 px²",fraction:"占比",valid_count:"有效样点",length_px:"长度 px",
  length_mm:"长度 mm",usable_px:"可计算面积 px²",center_delta_e:"中心色差",pages:"页数",entry_count:"封存记录",
  photo_count:"来源照片",file_count:"文件数",presentation_count:"展板项目",measured_cells:"已测格",
  usable_formulae:"可称量配方",visible_fraction_in_specimen:"实物底占比",accepted_stations:"有效剖面站点",
  thickness_mm:"厚度统计 mm",warning_count:"警报数",passed:"通过检查",valid:"封存完整",
  common_pixels_estimate:"共同实底 px²",above_threshold_fraction:"超阈值面积比例",
  marked_px:"标记面积",solid_count:"实底像素",line_pixels:"纹线像素",node_groups:"交会数",
  adjusted_px:"已调整像素",hold_count:"超出检查项",missing_count:"缺少证据",review_count:"需复核格数",
  edition:"卡片状态",output_size:"输出尺寸",pixels_retained:"保留像素",accepted_region_count:"着色片区",
  unique_transcribed:"实物编号数",total_views:"照片位置数",rev:"版本",
  default_family:"默认采用的试样带方案",requested_family_feasible:"指定方案可行",
};

function hideOutput() {$("#output").hidden=true;app.output=null;}
function entryArtifact(entry) {return "/api/entries/"+entry.id+"/artifact";}
function objectText(value) {
  if (value===null) return "—";
  if (typeof value==="boolean") return value?"是":"否";
  if (Array.isArray(value) && value.every(v=>["number","string"].includes(typeof v))) return value.join(" · ");
  if (typeof value==="string" || typeof value==="number") return String(value);
  return null;
}

function showOutput(content) {
  app.output=content;
  const wrap=$("#output"), output=content.data||{};
  const head=node("header",{class:"panel-heading"},[node("h2",{text:content.heading}),
    button("收起结果",hideOutput,"quiet small-button")]);
  const metrics=node("div",{class:"metrics"});
  for (const [name,label] of Object.entries(METRICS)) {
    if (!Object.hasOwn(output,name)) continue;
    const value=objectText(output[name]);
    if (value===null) continue;
    const item=node("div",{class:"metric"},[node("span",{text:label}),node("strong",{text:value})]);
    if (name==="swatch" && /^#[a-fA-F0-9]{6}$/.test(value)) item.lastElementChild.style.color=value;
    metrics.append(item);
  }
  metrics.append(details("结果明细",node("pre",{text:JSON.stringify(output,null,2)})));
  if (content.entry) {
    const entry=content.entry;
    const links=[link("记录清单","/api/entries/"+entry.id+"/manifest")];
    if (entry.artifact_sha) links.push(link(entry.payload.format==="zip"?"下载物证归档":"下载成果",entryArtifact(entry)+"?download=1"));
    if (CSV_KINDS.has(entry.kind)) links.push(link("下载 CSV","/api/entries/"+entry.id+"/csv"));
    if (entry.payload.format==="html") links.push(link("打开打印",entryArtifact(entry),"secondary small-button",{target:"_blank",rel:"noopener"}));
    metrics.append(buttonRow(...links));
  } else if (content.html) {
    metrics.append(button("打开打印预览",()=>{
      const url=URL.createObjectURL(new Blob([content.html],{type:"text/html;charset=utf-8"}));
      window.open(url,"_blank","noopener");
      setTimeout(()=>URL.revokeObjectURL(url),45000);
    },"secondary small-button"));
  }
  if (content.actions) metrics.append(buttonRow(...content.actions));
  const media=content.html || content.frame
    ? node("iframe",{class:"preview-frame",title:"打印版式预览",sandbox:"",srcdoc:content.html,src:content.frame})
    : (content.preview ? node("img",{class:"preview-image",src:content.preview,alt:content.heading+"图像"}) : null);
  const row=node("div",{class:"output-main"+(!media?" single":"")},media?[media,metrics]:[metrics]);
  wrap.replaceChildren(head,row);wrap.hidden=false;
  wrap.scrollIntoView({block:"nearest",behavior:"smooth"});
}

function openDialog(heading,...body) {
  $("#dialog-title").textContent=heading;
  $("#dialog-body").replaceChildren(...body);
  if (!$("#dialog").open) $("#dialog").showModal();
}
function closeDialog() {if ($("#dialog").open) $("#dialog").close();}

function editFiring(current) {
  const fields=formFields([
    {...string("name","窑次名称",current?.name||""),full:true,required:true},
    string("kiln","窑炉编号",current?.kiln||""),
    {key:"fired_on",type:"date",label:"出窑日期",value:current?.fired_on||""},
    {key:"notes",type:"textarea",label:"窑次说明",value:current?.notes||"",full:true},
  ]);
  const save=button("保存窑次",event=>action(event,async()=>{
    const data={...fields.read(),revision:current?.revision};
    const result=await api(current?"/api/firings/"+current.id:"/api/firings",{method:current?"PATCH":"POST",data});
    closeDialog();
    await reload(result.firing.id,current?app.photo?.id:null);
    if (!current) setView("studio");
  },current?"窑次已更新":"已建立新窑次"),"primary");
  const options=[save];
  if (!current) options.push(button("载入合成演示",event=>action(event,async()=>{
    const demo=await api("/api/firings/demo",{method:"POST"});
    closeDialog();await reload(demo.firing.id);setView("studio");
  },"已载入合成素材"),"secondary"));
  openDialog(current?"编辑窑次":"新窑次",fields.element,buttonRow(...options));
}

function openUpload() {
  if (!app.firing) {editFiring(null);return;}
  const fields=formFields([
    {key:"photo",label:"选择图片",type:"file",accept:".png,.jpg,.jpeg,.webp,.tif,.tiff",full:true,required:true},
    {...string("title","照片名称",""),placeholder:"留空时使用原文件名",full:true},
    menu("role","照片角色",ROLE,"overview"),
    {key:"note",label:"照片说明",type:"textarea",value:"",full:true},
  ]);
  const send=async event=>{event.preventDefault();await action(event,async()=>{
    const data=new FormData();
    const file=fields.inputs.photo.files[0];
    if (!file) throw new Error("请先选择一张照片");
    data.set("photo",file);
    for(const name of ["title","role","note"]) data.set(name,fields.inputs[name].value);
    const result=await api(apiFiring("/photos"),{method:"POST",data});
    closeDialog();await reload(app.firing.id,result.photo.id);setView("studio");
  },"照片已保存到窑次");};
  const form=node("form",{on:{submit:send}},[fields.element,paragraph("PNG、JPEG、WebP 或单页 TIFF。单张最多 14 MiB。"),
    node("button",{text:"保存照片",type:"submit",class:"primary"})]);
  openDialog("上传窑后照片",form);
}

function editPhoto() {
  const current=app.photo;
  if (!current) return;
  const info=formFields([
    {...string("title","照片名称",current.title),full:true},menu("role","照片角色",ROLE,current.role),
    {key:"note",label:"照片说明",type:"textarea",value:current.note,full:true},
  ]);
  const save=button("保存照片信息",ev=>action(ev,async()=>{
    await api(apiPhoto(),{method:"PATCH",data:{...info.read(),revision:current.revision}});
    closeDialog();await reload(app.firing.id,current.id);
  },"照片信息已更新"),"primary");
  const remove=button("移除未引用照片",ev=>action(ev,async()=>{
    if (!window.confirm("确定移除此张照片？")) return;
    await api(apiPhoto("?revision="+current.revision),{method:"DELETE"});
    closeDialog();await reload(app.firing.id);
  }),"quiet");
  openDialog("照片信息",paragraph(current.attachment_name+" · "+current.width+" × "+current.height),
    info.element,buttonRow(save,remove));
}

function lookupDefault() {
  return {query:"",state:"identified",match:"contains",page:1,limit:28};
}

function drawLibrary() {
  const result=$("#lookup-results"), controls=$("#lookup-tools");
  if(!app.firing) {result.replaceChildren(paragraph("新建窑次后可查询实物编号。"));controls.replaceChildren();return;}
  const prior=app.lookup?.settings||lookupDefault();
  const form=formFields([string("query","实物编号",prior.query),
    menu("state","格子状态",{identified:"已抄",...STATE,all:"全部"},prior.state),
    menu("match","匹配方式",{contains:"包含",exact:"完全相同"},prior.match),
    measure("limit","每页格数",prior.limit,5,70,1),
    toggle("skip_backlit","略过背光托盘",prior.skip_backlit||false)]);
  const state=()=>({...form.read(),page:1});
  const query=async number=>{
    const data={...form.read(),page:number};
    const response=await api(apiFiring("/catalog"),{method:"POST",data});
    app.lookup={settings:data,response};
    drawLibrary();
  };
  controls.replaceChildren(form.element,buttonRow(
    button("查询编号",ev=>action(ev,()=>query(1)),"primary"),
    button("并排检视",ev=>action(ev,async()=>{
      const answer=await api(apiFiring("/catalog/contact"),{method:"POST",data:state()});
      showOutput({heading:"实物照片位置",data:{count:answer.items.length,items:answer.items},preview:answer.preview});
    }),"secondary","catalog-contact"),
    button("制作编号图册",ev=>action(ev,async()=>{
      const answer=await api(apiFiring("/catalog/print"),{method:"POST",data:{...state(),save:true}});
      await reload(app.firing.id,app.photo?.id);
      showOutput({heading:"编号图册已归档",data:answer.result,entry:answer.entry,html:answer.html});
    },"已归档编号图册")),
  ));
  $("#lookup-count").textContent=app.lookup?.response.total_views??"—";
  if (!app.lookup) {result.replaceChildren(paragraph("输入编号或选格子状态，查找已抄录的原图位置。","record-empty"));return;}
  const data=app.lookup.response;
  const body=[];
  for (const group of data.groups) {
    const groupEl=node("fieldset",{class:"fieldset"},[node("legend",{text:(group.label||"待抄格")+" · "+group.count+" 处"})]);
    for (const view of group.views) {
      groupEl.append(node("div",{class:"list-item"},[
        node("span",{text:`${view.tray_title} · ${view.address} · ${STATE[view.state]}`}),
        button("在原照检视",()=>action(null,async()=>{
          setView("studio");selectPhoto(view.photo_id);togglePane("tray");
          await inspectTray({row:view.row,column:view.column});
        }),"quiet small-button"),
      ]));
    }
    body.push(groupEl);
  }
  if(!body.length) body.push(paragraph("当前条件没有匹配的格子。"));
  if(data.page_count>1) {
    const prev=button("上一页",ev=>action(ev,()=>query(data.page-1)),"secondary small-button");
    const next=button("下一页",ev=>action(ev,()=>query(data.page+1)),"secondary small-button");
    prev.disabled=data.page<=1;next.disabled=data.page>=data.page_count;
    body.push(buttonRow(prev,paragraph(`${data.page} / ${data.page_count}`),next));
  }
  result.replaceChildren(...body);
}

function selectedRecords() {return app.entries.filter(entry=>app.kept.has(entry.id));}
function visibleRecords() {
  const chosen=$("#entry-kind").value;
  return app.entries.filter(entry=>!chosen||chosen===entry.kind);
}
function showEntry(entry) {
  const format=entry.payload.format;
  showOutput({
    heading:entry.title,data:entry.payload.result||entry.payload,
    preview:format==="png"?entryArtifact(entry):null,
    frame:format==="html"?entryArtifact(entry):null,entry,
  });
}
function drawRecords() {
  const list=$("#record-list");
  const active=$("#entry-kind").value||"";
  optionList($("#entry-kind"),{"":"全部类型",...LABELS},active);
  const filtered=visibleRecords();
  $("#record-count").textContent=String(filtered.length);
  list.replaceChildren();
  for(const entry of filtered) {
    const checkbox=node("input",{type:"checkbox",checked:app.kept.has(entry.id),"aria-label":"选择 "+entry.title,
      on:{change:event=>{
        if(event.currentTarget.checked) app.kept.add(entry.id);
        else app.kept.delete(entry.id);
        drawRecordTools();
      }}});
    checkbox.disabled=entry.kind==="packet";
    const left=node("div",{},[node("strong",{text:entry.title}),
      node("div",{class:"meta",text:`${LABELS[entry.kind]||entry.kind} · ${entry.updated.slice(0,10)} · v${entry.revision}`})]);
    const items=[button("查看",()=>showEntry(entry),"quiet small-button")];
    if(entry.kind==="packet") items.push(link("下载 ZIP",entryArtifact(entry)+"?download=1"));
    const right=node("div",{class:"right"},items);
    list.append(node("div",{class:"record-row"},[checkbox,left,right]));
  }
  if(!filtered.length) list.append(paragraph("暂无匹配的记录","record-empty"));
  drawRecordTools();
}

function settingsDialog(title,description,defaultValue,submit) {
  const area=node("textarea",{rows:14,value:JSON.stringify(defaultValue,null,2),"aria-label":title+"参数"});
  const buttonSubmit=button("确认生成",event=>action(event,async()=>{
    const input=parseExtra(area,{});
    await submit(input);
    closeDialog();
  }),"primary");
  openDialog(title,paragraph(description),area,buttonRow(buttonSubmit));
}

function boardDialog() {
  const selected=app.photo ? [app.photo,...app.photos.filter(i=>i.id!==app.photo.id)] : app.photos;
  if(!selected.length) {notice("此窑次还没有照片",true);return;}
  const items=selected.slice(0,4).map((p,index)=>({photo:p.id,label:p.title,region:centered(p),caption:p.note,group:index<2&&selected.length>=2?"并列复查":""}));
  settingsDialog("对照展板","可调整照片标识、样区、标题、两两对照组和纸张。",{
    items,paper:{format:"a4",columns:2,title:"窑后呈色对照展板"},fit:"contain",calibrated:true
  },async data=>{
    const result=await api(apiFiring("/board"),{method:"POST",data:{...data,save:true}});
    await reload(app.firing.id,app.photo?.id);
    showOutput({heading:"已归档对照展板",html:result.html,data:result.result,entry:result.entry});
  });
}

function closeoutDialog() {
  const byType={color:"color_spread",pinholes:"pinhole_count",glare:"glare_usable",coverage:"sample_coverage",
    target:"patches_pass",crazing:"crazing_length",phases:"phase_confidence"};
  const checks=selectedRecords().filter(row=>byType[row.kind]).slice(0,14).map((row,index)=>({
    id:"check-"+(index+1),target:byType[row.kind],entry:row.id,required:true
  }));
  if (!checks.length) checks.push({id:"check-1",target:"color_spread",required:true});
  settingsDialog("留档检核单","填写人、检查记录和阈值可在下方修改。留空的必查项会列成待补。",{
    author:"",name:"窑次照片检核单",checks,
  },async data=>{
    const result=await api(apiFiring("/closeout"),{method:"POST",data:{...data,save:true}});
    await reload(app.firing.id,app.photo?.id);
    showOutput({heading:"已归档留档检核单",html:result.html,data:result.result,entry:result.entry});
  });
}

const TREND_FIELDS={color:"color_shift",glare:"glare_fraction",texture:"fine_texture",
  spot:"spot_contrast",pinholes:"pinhole_density",crazing:"crazing_density"};
async function trendsDialog() {
  const entries=selectedRecords().filter(entry=>TREND_FIELDS[entry.kind]);
  const kind=entries[0]?.kind||"color";
  const active=entries.filter(entry=>entry.kind===kind);
  const sources=app.firings.filter(f=>f.id!==app.firing.id).slice(0,10);
  let external=[];
  for(const item of sources) {
    const found=await api("/api/firings/"+item.id+"?kind="+kind);
    external.push(...found.entries.slice(0,2).map(row=>({entry:row.id,label:item.name+" · "+row.title,track:"共同观察"})));
  }
  const select=active.length ? active : app.entries.filter(entry=>entry.kind===kind).slice(0,3);
  const readings=[...select.map(row=>({entry:row.id,label:row.title,track:"共同观察"})),...external];
  const settings={feature:TREND_FIELDS[kind],policy:"strict",tolerance:5,readings};
  settingsDialog("跨窑次趋势","使用已归档的同类测量记录。可以填入其它窑次的记录编号，并指定比较组。",settings,async input=>{
    const outcome=await api("/api/trends",{method:"POST",data:input});
    const download=(label,format)=>button(label,ev=>action(ev,async()=>{
      const blob=await api("/api/trends?format="+format,{method:"POST",data:input,raw:true});
      const url=URL.createObjectURL(blob);
      const anchor=node("a",{href:url,download:"kiln-trends."+format});
      anchor.click();setTimeout(()=>URL.revokeObjectURL(url),15000);
    }),"secondary small-button");
    showOutput({heading:"跨窑次趋势",data:outcome.result,preview:outcome.preview,
      actions:[download("下载趋势 CSV","csv"),download("下载趋势图","png")]});
  });
}

function drawRecordTools() {
  const tools=$("#record-tools");
  if(!app.firing) {tools.replaceChildren(paragraph("打开窑次后可封存物证。"));return;}
  const selected=selectedRecords();
  const files=formFields([{type:"file",key:"packet",label:"选择本地封存 ZIP",accept:".zip",full:true}]);
  const config=formFields([{...string("title","封存包名称",""),full:true},toggle("include_assets","包含来源照片",true)]);
  const packaging=button("封存所选记录",event=>action(event,async()=>{
    const ids=selected.map(item=>item.id);
    const result=await api(apiFiring("/packet"),{method:"POST",data:{...config.read(),entries:ids}});
    app.kept.clear();
    await reload(app.firing.id,app.photo?.id);
    showEntry(result.entry);
  },"物证封存成功"),"primary","archive-packet");
  packaging.disabled=!selected.length;
  const verify=button("核验封存包",event=>action(event,async()=>{
    const chosen=files.inputs.packet.files[0];
    if(!chosen) throw new Error("先选择一份本地封存 ZIP");
    const input=new FormData();input.set("packet",chosen);
    const result=await api("/api/packet/verify",{method:"POST",data:input});
    showOutput({heading:result.valid?"封存核验通过":"封存核验发现问题",data:result});
  }),"secondary","verify-packet");
  tools.replaceChildren(paragraph("已选 "+selected.length+" 条记录"),config.element,buttonRow(packaging),
    node("div",{class:"notice-block"},[paragraph("选择本地 ZIP 可复查文件摘要、来源照片和记录内容。"),files.element,verify]),
    buttonRow(button("对照展板",boardDialog),button("留档检核单",closeoutDialog),
      button("跨窑次趋势",event=>action(event,trendsDialog))));
}

function showHelp() {
  openDialog("釉样台使用说明",
    paragraph("1　创建窑次，上传静态 PNG、JPEG、WebP 或单页 TIFF。演示照片和质地均为合成素材。"),
    paragraph("2　在图像工作台框选区域或顺序标点。测量输出可以单独归档，标尺与校色可供同照片后续检视使用。"),
    paragraph("3　托盘按左上、右上、左下建立。点击网格或实际照片位置后可查看、抄录并加入留样卡。"),
    paragraph("4　到窑次留档选择记录并生成物证包。保存好的 HTML 制卡文件在浏览器按 100% 比例打印。"),
    paragraph("照片读数取决于光照、标尺和记录设置。透明区域以及来源不明的盖片需要单独核对。"));
}

$$(".top-tab").forEach(el=>el.addEventListener("click",()=>setView(el.dataset.view)));
$$(".inspector-tabs button").forEach(el=>el.addEventListener("click",()=>togglePane(el.dataset.pane)));
$$(".draw-mode").forEach(el=>el.addEventListener("click",()=>drawMode(el.dataset.draw)));
for(const id of ["upload-open","upload-from-film","upload-from-empty"]) $("#"+id).addEventListener("click",openUpload);
$("#help-open").addEventListener("click",showHelp);
$("#edit-firing").addEventListener("click",()=>editFiring(app.firing));
$("#edit-photo").addEventListener("click",editPhoto);
$("#new-firing").addEventListener("click",()=>editFiring(null));
$("#dialog-close").addEventListener("click",closeDialog);
$("#firing").addEventListener("change",event=>action(event,()=>reload(event.currentTarget.value,null)));
$("#entry-kind").addEventListener("change",drawRecords);
$("#select-records").addEventListener("click",()=>{
  const eligible=visibleRecords().filter(entry=>entry.kind!=="packet");
  const add=!eligible.every(entry=>app.kept.has(entry.id));
  for(const entry of eligible) add?app.kept.add(entry.id):app.kept.delete(entry.id);
  drawRecords();
});
$("#photo-search").addEventListener("input",drawFilm);
$("#photo-role").addEventListener("change",drawFilm);
$("#clear-drawing").addEventListener("click",()=>{app.region=null;app.points=[];drawMarkup();});
$("#zoom-in").addEventListener("click",()=>{app.zoom=Math.min(4,app.zoom+.25);drawMarkup();});
$("#zoom-out").addEventListener("click",()=>{app.zoom=Math.max(1,app.zoom-.25);drawMarkup();});
$("#zoom-reset").addEventListener("click",()=>{app.zoom=1;drawMarkup();});
action(null,()=>reload());
