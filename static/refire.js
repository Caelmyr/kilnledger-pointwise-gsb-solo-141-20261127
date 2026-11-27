// 复烧选方 + 称料核对 workbench. Talks to the pure /api/refire endpoints and
// renders into the wide #output panel so the weighing tables have room.

const $ = selector => document.querySelector(selector);

function h(tag, options = {}, children = []) {
  const el = document.createElement(tag);
  const kids = Array.isArray(children) ? children : (children == null ? [] : [children]);
  for (const [key, value] of Object.entries(options)) {
    if (value === undefined || value === null) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key === "html") el.innerHTML = value;
    else if (key === "on") Object.entries(value).forEach(([type, fn]) => el.addEventListener(type, fn));
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key in el) el[key] = value;
    else el.setAttribute(key, value);
  }
  el.append(...kids);
  return el;
}

function num(value, digits = 2) {
  const n = typeof value === "number" ? value : parseFloat(value);
  return Number.isFinite(n) ? String(Math.round(n * 10 ** digits) / 10 ** digits) : "—";
}

function clone(value) { return JSON.parse(JSON.stringify(value)); }

function blankDraft() {
  return {
    adjustment_ranges: { L: [-2, 2], a: [-1.5, 1.5] },
    precision: "0.1",
    cup_grams: "20",
    targets: [],
    candidates: [],
    stock: {},
  };
}

export async function openRefireWorkbench(ctx) {
  const output = $("#output");
  output.hidden = false;
  output.replaceChildren();
  const root = h("div", { class: "refire-workbench", "aria-label": "复烧选方与称料核对工作台" });
  output.append(root);

  const initial = blankDraft();
  if (ctx.defaults) {
    if (ctx.defaults.precision) initial.precision = String(ctx.defaults.precision);
    if (ctx.defaults.cup_grams) initial.cup_grams = String(ctx.defaults.cup_grams);
  }
  const ui = {
    ctx,
    draft: initial,
    result: null,
    selectedId: null,
    weigh: null,
    actuals: {},
    loadingStored: false,
  };

  function notice(message, error = false) {
    if (ctx.notice) ctx.notice(message, error);
  }
  async function api(path, options) { return ctx.api(path, options); }

  const helpers = { notice, api, render: () => render() };

  helpers.solve = async function (btn) {
    if (btn) btn.disabled = true;
    try {
      const payload = buildPayload(ui.draft);
      const j = await api("/api/refire/plan", { method: "POST", data: payload });
      ui.result = j;
      ui.weigh = null;
      ui.actuals = {};
      ui.selectedId = j.status === "ready" ? j.plan.plan_id : null;
      render();
      $("#rf-solve")?.scrollIntoView({ block: "center", behavior: "smooth" });
    } catch (err) {
      console.error("rf.solve failed", err);
      notice(err.message, true);
    } finally {
      if (btn) btn.disabled = false;
    }
  };

  helpers.selectPlan = function (id) {
    ui.selectedId = id;
    ui.weigh = null;
    render();
    document.querySelector(`[data-plan-id="${id}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };

  helpers.adopt = function (id) {
    helpers.selectPlan(id);
    setTimeout(() => $(".rf-weigh")?.scrollIntoView({ block: "start", behavior: "smooth" }), 60);
  };

  function bagFor(planId) {
    if (!ui.actuals[planId]) ui.actuals[planId] = {};
    return ui.actuals[planId];
  }

  function currentWeighed(plan) {
    const bag = bagFor(plan.plan_id);
    const weighed = [];
    for (const cup of plan.cups) {
      const values = Object.fromEntries(Object.entries(bag[cup.cup_id] || {})
        .filter(([, v]) => String(v).trim() !== ""));
      if (Object.keys(values).length) weighed.push({ cup_id: cup.cup_id, weights: values });
    }
    return weighed;
  }

  helpers.recheck = async function (btn) {
    const plan = ui.result?.plans.find(p => p.plan_id === ui.selectedId);
    if (!plan) { notice("请先采用一组方案", true); return; }
    if (btn) btn.disabled = true;
    try {
      const weighed = currentWeighed(plan);
      const j = await api("/api/refire/weigh", {
        method: "POST",
        data: {
          precision: ui.draft.precision, cup_grams: ui.draft.cup_grams,
          stock: ui.draft.stock, plan, weighed,
        },
      });
      ui.weigh = j;
      render();
      $(".rf-weigh-result")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    } catch (err) {
      console.error("rf.recheck failed", err);
      notice(err.message, true);
    } finally {
      if (btn) btn.disabled = false;
    }
  };

  helpers.archive = async function (btn) {
    const plan = ui.result?.plans.find(p => p.plan_id === ui.selectedId);
    if (!plan || !ctx.photo) { notice("请先在当前照片上求出并采用一组方案", true); return; }
    if (btn) btn.disabled = true;
    try {
      const weighed = currentWeighed(plan);
      const payload = buildPayload(ui.draft);
      payload.plan_id = plan.plan_id;
      payload.weigh = {
        precision: ui.draft.precision, cup_grams: ui.draft.cup_grams,
        stock: ui.draft.stock, weighed,
      };
      await api(`/api/photos/${ctx.photo.id}/analysis/recipe/save`, {
        method: "POST",
        data: { title: `复烧选方 ${plan.plan_id} · 整图调整与称料核对`, settings: { refire: payload } },
      });
      notice("方案与称料核对已归档到返烧配方阵列");
      if (ctx.onSaved) ctx.onSaved();
    } catch (err) {
      console.error("rf.archive failed", err);
      notice(err.message, true);
    } finally {
      if (btn) btn.disabled = false;
    }
  };

  helpers.fillDemoActuals = function () {
    const demo = ui.draft.demo_weighed;
    const plan = ui.result?.plans.find(p => p.plan_id === ui.selectedId);
    if (!demo) { notice("请先点“填入示范复烧情景”", true); return; }
    if (!plan) return;
    const bag = bagFor(plan.plan_id);
    for (const entry of demo) {
      if (!bag[entry.cup_id]) bag[entry.cup_id] = {};
      Object.assign(bag[entry.cup_id], entry.weights);
    }
    ui.weigh = null;
    render();
  };

  helpers.fillAdoptedActuals = function () {
    const plan = ui.result?.plans.find(p => p.plan_id === ui.selectedId);
    if (!plan) return;
    const cup = plan.cups[0];
    const bag = bagFor(plan.plan_id);
    bag[cup.cup_id] = Object.fromEntries(cup.weights.map(w => [w.name, w.grams]));
    ui.weigh = null;
    render();
  };

  helpers.addRegionTarget = async function (btn) {
    const region = ctx.getRegion ? ctx.getRegion() : null;
    const photo = ctx.photo;
    if (!region || !photo) { notice("先在目标照上拖一个框", true); return; }
    if (btn) btn.disabled = true;
    try {
      const reading = await api(`/api/photos/${photo.id}/analysis/color`, {
        method: "POST", data: { region, calibrated: false, bins: 8 },
      });
      const lab = reading.analysis.result.lab;
      ui.draft.targets.push({
        name: `框选颜色${ui.draft.targets.length + 1}`, role: "spot", lab, limit: 4, region,
      });
      render();
      notice("已读取框选 Lab，补上色差限额与“大片底色/窄色带”定位");
    } catch (err) {
      notice(err.message, true);
    } finally {
      if (btn) btn.disabled = false;
    }
  };

  helpers.loadStored = async function (btn) {
    if (!ctx.firingId) { notice("还没有当前窑次", true); return; }
    if (btn) btn.disabled = true;
    try {
      const data = await api(`/api/firings/${ctx.firingId}/refire/options`);
      const known = new Set(ui.draft.candidates.map(c => c.id || c.address));
      let added = 0;
      for (const c of data.candidates) {
        if (known.has(c.id) || known.has(c.address)) continue;
        ui.draft.candidates.push(c);
        known.add(c.id);
        added++;
      }
      render();
      notice(added ? `已载入 ${added} 个旧实烧格` : "归档阵列里没有新的带 Lab 与克数的格");
    } catch (err) {
      notice(err.message, true);
    } finally {
      if (btn) btn.disabled = false;
    }
  };

  helpers.runDemo = async function (btn) {
    if (btn) btn.disabled = true;
    try {
      const data = await api("/api/refire/demo");
      ui.draft = normalizeDraft(data.scenario);
      await helpers.solve(null);
      helpers.fillDemoActuals();
      await helpers.recheck(null);
      notice("已填入示范：A1/B3 最准但超存量，可切换底色最佳与色带最佳方案；第一杯按实数占用库存后第二杯待核");
    } catch (err) {
      notice(err.message, true);
    } finally {
      if (btn) btn.disabled = false;
    }
  };

  function render() { renderAll(root, ui, helpers); }
  ui.rerender = render;
  ui.helpers = helpers;

  root.append(h("header", { class: "panel-heading" }, [
    h("h2", { text: "返烧阵列 · 复烧选方与称料核对" }),
    h("button", {
      type: "button", class: "quiet", text: "收起",
      on: { click: () => { output.hidden = true; output.replaceChildren(); } },
    }),
  ]));
  const body = h("div", { class: "refire-body" });
  root.append(body);
  ui.body = body;
  render();
  output.scrollIntoView({ block: "start", behavior: "smooth" });
  return ui;
}

function renderAll(root, ui, helpers) {
  const body = ui.body;
  body.replaceChildren(intro(ui, helpers), editor(ui, helpers), actions(ui, helpers));
  if (ui.result) body.append(planSection(ui, helpers));
  if (ui.result?.status === "ready" && ui.selectedId) body.append(weighSection(ui, helpers));
}

function buildPayload(draft) {
  return {
    adjustment_ranges: draft.adjustment_ranges,
    precision: draft.precision,
    cup_grams: draft.cup_grams,
    targets: draft.targets.map(t => ({ ...t, limit: Number(t.limit) })),
    candidates: draft.candidates,
    stock: draft.stock,
  };
}

function intro(ui, helpers) {
  return h("section", { class: "rf-intro" }, [
    h("p", { class: "help", text:
      "① 在目标照上框出要保的颜色并取色；② 加入旧实烧格、给两项整图调整范围和原料存量；"
      + "③ 求出同一份调整下全部过限额的配方方案，可在保大片底色与保窄色带的方案间切换并看清代价；"
      + "④ 采用后录入已称杯的实数，重新核对剩余杯。已称数量按实数占库存，采用配方不会被改写。" }),
    h("div", { class: "button-row" }, [
      h("button", {
        type: "button", class: "secondary", id: "rf-demo",
        text: "填入示范复烧情景并直接出方案",
        on: { click: e => helpers.runDemo(e.currentTarget) },
      }),
      h("button", {
        type: "button", class: "quiet",
        text: "从已归档返烧阵列载入旧格",
        on: { click: e => helpers.loadStored(e.currentTarget) },
      }),
    ]),
  ]);
}

// ---------------------------------------------------------------------------
// Scenario editor
// ---------------------------------------------------------------------------

function scalarField(label, value, oninput, { step = "any", width = "74px" } = {}) {
  const input = h("input", {
    type: "number", value: value ?? "", step, "aria-label": label,
    style: `width:${width}`,
    on: { change: e => oninput(e.target.value) },
  });
  return h("label", { class: "rf-mini-field" }, [h("span", { text: label }), input]);
}

function editor(ui, helpers) {
  const d = ui.draft;
  const section = h("section", { class: "rf-editor panel-inner", "aria-label": "复烧条件编辑" });

  const ranges = h("div", { class: "rf-ranges" }, [
    h("strong", { text: "整图调整范围（Lab）" }),
    rangePair("明度 L 最低", d.adjustment_ranges.L[0], v => d.adjustment_ranges.L[0] = parseFloat(v)),
    rangePair("明度 L 最高", d.adjustment_ranges.L[1], v => d.adjustment_ranges.L[1] = parseFloat(v)),
    rangePair("红绿 a 最低", d.adjustment_ranges.a[0], v => d.adjustment_ranges.a[0] = parseFloat(v)),
    rangePair("红绿 a 最高", d.adjustment_ranges.a[1], v => d.adjustment_ranges.a[1] = parseFloat(v)),
    scalarField("秤刻度 g", d.precision, v => d.precision = v, { step: "0.01" }),
    scalarField("每杯目标克数 g", d.cup_grams, v => d.cup_grams = v, { step: "0.1", width: "86px" }),
  ]);
  section.append(ranges);

  // ---- targets ----
  const targetHead = h("div", { class: "rf-section-head" }, [
    h("h4", { text: "要保的几处颜色（目标照框选）" }),
    h("button", {
      type: "button", class: "secondary small-button", id: "rf-add-region",
      text: "＋ 用当前框选加入一处颜色",
      on: { click: e => helpers.addRegionTarget(e.currentTarget) },
    }),
    h("button", {
      type: "button", class: "quiet small-button",
      text: "＋ 手动加一处",
      on: { click: () => { d.targets.push(blankTarget()); ui.rerender(); } },
    }),
  ]);
  section.append(targetHead, targetTable(ui));

  // ---- candidates ----
  const candHead = h("div", { class: "rf-section-head" }, [
    h("h4", { text: "可拿来对照的旧实烧格（含每杯用料）" }),
    h("button", {
      type: "button", class: "quiet small-button",
      text: "＋ 手动加旧格",
      on: { click: () => { d.candidates.push(blankCandidate()); ui.rerender(); } },
    }),
  ]);
  section.append(candHead, candidateTable(ui));

  // ---- stock ----
  section.append(h("h4", { text: "原料存量上限（各杯合计不得超过）" }), stockEditor(ui));

  // ---- JSON ----
  const jsonArea = h("textarea", {
    rows: 9, class: "rf-json", "aria-label": "复烧情景 JSON",
    placeholder: "可直接粘贴/编辑完整情景 JSON",
  });
  jsonArea.value = JSON.stringify(d, null, 2);
  section.append(h("details", { class: "rf-json-details" }, [
    h("summary", { text: "情景 JSON（高级粘贴 / 读取）" }),
    h("div", { class: "button-row" }, [
      h("button", {
        type: "button", class: "secondary small-button", text: "应用 JSON 到条件",
        on: { click: e => {
          try {
            ui.draft = normalizeDraft(JSON.parse(jsonArea.value));
            ui.result = null; ui.weigh = null; ui.rerender();
            helpers.notice("JSON 已应用");
          } catch (err) { helpers.notice("JSON 无法解析：" + err.message, true); }
        } },
      }),
      h("button", {
        type: "button", class: "quiet small-button", text: "把当前条件写入 JSON",
        on: { click: () => { jsonArea.value = JSON.stringify(ui.draft, null, 2); } },
      }),
    ]),
    jsonArea,
  ]));
  ui.jsonArea = jsonArea;
  return section;
}

function rangePair(label, value, oninput) {
  return h("label", { class: "rf-mini-field" }, [
    h("span", { text: label }),
    h("input", {
      type: "number", value: value ?? "", step: "0.1", "aria-label": label, style: "width:78px",
      on: { change: e => { oninput(e.target.value); } },
    }),
  ]);
}

function blankTarget() {
  return { name: `颜色${Math.random().toString(36).slice(2, 6)}`, role: "spot", lab: [60, 0, 0], limit: 4 };
}
function blankCandidate() {
  return { id: `cell-${Date.now().toString(36)}`, address: "C9", lab: [60, 0, 0], materials: [] };
}

function labInputs(lab, onchange, ariaPrefix) {
  const wrap = h("span", { class: "rf-lab" });
  ["L", "a", "b"].forEach((axis, i) => {
    wrap.append(h("input", {
      type: "number", step: "0.01", value: num(lab[i], 2), "aria-label": `${ariaPrefix} ${axis}`,
      style: "width:64px",
      on: { change: e => { lab[i] = parseFloat(e.target.value) || 0; onchange(); } },
    }));
  });
  return wrap;
}

function targetTable(ui) {
  const d = ui.draft;
  if (!d.targets.length) return h("p", { class: "record-empty", text: "还没有要保的颜色。在照片上拖框后点“用当前框选加入一处颜色”。" });
  const table = h("table", { class: "rf-table targets", "aria-label": "要保颜色列表" });
  table.append(h("thead", {}, h("tr", {}, [
    h("th", { text: "名称" }), h("th", { text: "定位" }), h("th", { text: "实测 Lab" }),
    h("th", { text: "色差限额" }), h("th", { text: "" }),
  ])));
  const tbody = h("tbody");
  d.targets.forEach((t, ix) => {
    const roleSelect = h("select", { "aria-label": `${t.name} 颜色定位`, on: { change: e => t.role = e.target.value } }, [
      h("option", { value: "base", text: "大片底色" }),
      h("option", { value: "band", text: "窄色带" }),
      h("option", { value: "spot", text: "普通颜色" }),
    ]);
    roleSelect.value = t.role || "spot";
    const nameInput = h("input", {
      type: "text", value: t.name, "aria-label": "颜色名称",
      on: { change: e => t.name = e.target.value },
    });
    const limitInput = h("input", {
      type: "number", step: "0.1", min: "0.1", value: t.limit, "aria-label": `${t.name} 色差限额`,
      style: "width:64px", on: { change: e => t.limit = parseFloat(e.target.value) },
    });
    tbody.append(h("tr", {}, [
      h("td", {}, [nameInput]),
      h("td", { class: "rf-role-cell" }, [roleSelect]),
      h("td", {}, [labInputs(t.lab, () => {}, t.name)]),
      h("td", {}, [limitInput]),
      h("td", {}, [h("button", {
        type: "button", class: "quiet small-button", text: "删除",
        on: { click: () => { d.targets.splice(ix, 1); ui.rerender(); } },
      })]),
    ]));
  });
  table.append(tbody);
  return h("div", { class: "rf-table-scroll" }, [table]);
}

function parseMaterialsText(text) {
  const out = [];
  for (const part of String(text).split(/[;；\n]+/)) {
    if (!part.trim()) continue;
    const bits = part.includes(":") ? part.split(":") : part.split(/[=：]/);
    if (bits.length < 2) continue;
    const name = bits[0].trim();
    const grams = bits[1].trim();
    if (name && grams) out.push({ name, grams });
  }
  return out;
}
function materialsText(mats) {
  return (mats || []).map(m => `${m.name}:${m.grams}`).join("；");
}

function candidateTable(ui) {
  const d = ui.draft;
  if (!d.candidates.length) return h("p", { class: "record-empty", text: "还没有旧实烧格。可从归档阵列载入，或手动添加并填写 Lab 与每杯克数。" });
  const table = h("table", { class: "rf-table candidates", "aria-label": "旧实烧格列表" });
  table.append(h("thead", {}, h("tr", {}, [
    h("th", { text: "格号" }), h("th", { text: "实物 Lab" }),
    h("th", { text: "每杯用料（原料:克数；…）" }), h("th", { text: "" }),
  ])));
  const tbody = h("tbody");
  d.candidates.forEach((c, ix) => {
    const addr = h("input", {
      type: "text", value: c.address, "aria-label": "旧格编号", style: "width:64px",
      on: { change: e => { c.address = e.target.value; c.label = e.target.value; } },
    });
    const mats = h("input", {
      type: "text", value: materialsText(c.materials), "aria-label": `${c.address} 用料克数`,
      placeholder: "长石:5.6；氧化铁:0.9",
      on: { change: e => c.materials = parseMaterialsText(e.target.value) },
    });
    tbody.append(h("tr", {}, [
      h("td", {}, [addr]),
      h("td", {}, [labInputs(c.lab, () => {}, c.address)]),
      h("td", { class: "rf-mats-cell" }, [mats]),
      h("td", {}, [h("button", {
        type: "button", class: "quiet small-button", text: "删除",
        on: { click: () => { d.candidates.splice(ix, 1); ui.rerender(); } },
      })]),
    ]));
  });
  table.append(tbody);
  return h("div", { class: "rf-table-scroll" }, [table]);
}

function stockEditor(ui) {
  const stock = ui.draft.stock;
  const wrap = h("div", { class: "rf-stock" });
  const renderRows = () => {
    wrap.replaceChildren();
    const table = h("table", { class: "rf-table stock", "aria-label": "原料存量上限" });
    const tbody = h("tbody");
    Object.entries(stock).forEach(([name, grams]) => {
      let currentName = name;
      const nameInput = h("input", {
        type: "text", value: name, "aria-label": "原料名",
        on: { change: e => {
          delete stock[currentName];
          currentName = e.target.value.trim() || currentName;
          stock[currentName] = grams;
        } },
      });
      const gramInput = h("input", {
        type: "number", step: "0.01", min: "0", value: grams, "aria-label": `${name} 存量克数`,
        style: "width:90px", on: { change: e => stock[currentName] = e.target.value },
      });
      tbody.append(h("tr", {}, [
        h("td", {}, [nameInput]), h("td", {}, [gramInput]),
        h("td", {}, [h("button", {
          type: "button", class: "quiet small-button", text: "删除",
          on: { click: () => { delete stock[currentName]; renderRows(); } },
        })]),
      ]));
    });
    table.append(tbody);
    wrap.append(table, h("button", {
      type: "button", class: "secondary small-button", text: "＋ 加一种原料上限",
      on: { click: () => {
        let base = "新原料", n = 1;
        while (base in stock) base = `新原料${n++}`;
        stock[base] = "100";
        renderRows();
      } },
    }));
  };
  renderRows();
  return wrap;
}

function normalizeDraft(raw) {
  const d = blankDraft();
  if (raw.adjustment_ranges) {
    d.adjustment_ranges = {
      L: raw.adjustment_ranges.L || raw.adjustment_ranges.l || [-2, 2],
      a: raw.adjustment_ranges.a || raw.adjustment_ranges.A || [-1.5, 1.5],
    };
  } else if (raw.lightness_range || raw.red_green_range) {
    d.adjustment_ranges = { L: raw.lightness_range || [-2, 2], a: raw.red_green_range || [-1.5, 1.5] };
  }
  d.precision = raw.precision ?? "0.1";
  d.cup_grams = raw.cup_grams ?? raw.grams ?? "20";
  d.targets = raw.targets || [];
  d.candidates = raw.candidates || raw.cells || [];
  d.stock = raw.stock || {};
  d.demo_weighed = raw.demo_weighed;
  return d;
}

function actions(ui, helpers) {
  return h("div", { class: "button-row rf-actions" }, [
    h("button", {
      type: "button", class: "primary", id: "rf-solve",
      text: "给出可以采用的配方方案",
      on: { click: e => helpers.solve(e.currentTarget) },
    }),
  ]);
}

// ---------------------------------------------------------------------------
// Plans
// ---------------------------------------------------------------------------

function planSection(ui, helpers) {
  const r = ui.result;
  const sec = h("section", { class: "rf-plans", "aria-label": "可行配方方案" });
  if (r.status !== "ready") {
    sec.append(h("div", { class: "rf-banner bad", role: "alert" }, [
      h("strong", { text: "没有合格方案" }),
      h("span", { text: r.message || "同一份整图调整下无法让所有颜色过限额。" }),
    ]));
    return sec;
  }
  sec.append(h("div", { class: "rf-banner good" }, [
    h("strong", { text: `已有 ${r.counts.qualified} 组合格方案` }),
    h("span", { text: `检查了 ${r.counts.assignments_checked} 种选格组合；另有 ${r.counts.stock_blocked} 组颜色可行但超存量。` }),
  ]));

  const switchRow = h("div", { class: "rf-role-switch", "aria-label": "按保色重点选方案" });
  const roleDefs = [
    ["base", "看保大片底色最准的选法"],
    ["band", "看保住窄色带的选法"],
    ["balanced", "看最均衡（最大色差最小）的推荐"],
  ];
  for (const [role, label] of roleDefs) {
    const id = r.role_choices?.[role];
    if (!id) continue;
    switchRow.append(h("button", {
      type: "button", class: "secondary", "data-plan": id,
      text: `${label}（${id}）`,
      on: { click: () => helpers.selectPlan(id, { weigh: false }) },
    }));
  }
  sec.append(switchRow);

  const cards = h("div", { class: "rf-plan-cards" });
  for (const p of r.plans) cards.append(planCard(ui, helpers, p));
  sec.append(cards);

  if (r.stock_blocked?.length) {
    sec.append(h("details", { class: "rf-blocked" }, [
      h("summary", { text: `颜色更准但超存量的方案（${r.stock_blocked.length}，不可采用）` }),
      ...r.stock_blocked.map(p => h("div", { class: "rf-blocked-row", text:
        `${p.plan_id}：${p.signature}；超量原料 `
        + p.materials.filter(m => !m.within_stock).map(m => `${m.name} 需 ${m.grams} / 上限 ${m.stock}`).join("，") })),
    ]));
  }
  return sec;
}

function planCard(ui, helpers, p) {
  const selected = ui.selectedId === p.plan_id;
  const r = ui.result;
  const isRole = role => r.role_choices?.[role] === p.plan_id;
  const tags = [];
  if (isRole("balanced")) tags.push(["recommended", "推荐采用"]);
  if (isRole("base")) tags.push(["base", "保大片底色最佳"]);
  if (isRole("band")) tags.push(["band", "保住窄色带最佳"]);

  const card = h("article", { class: "rf-plan-card" + (selected ? " selected" : ""), "data-plan-id": p.plan_id });
  card.append(h("header", {}, [
    h("h4", { text: `方案 ${p.plan_id}` }),
    h("span", { class: "rf-tags" }, tags.map(([cls, text]) => h("em", { class: `rf-tag ${cls}`, text }))),
  ]));

  card.append(h("p", { class: "rf-adjust" }, [
    h("strong", { text: "使用的整图调整：" }),
    h("span", {
      text: `ΔL ${signed(p.adjustment.L)}，Δa（红绿）${signed(p.adjustment.a)}，b 项不调；`
        + `最大 ΔE ${num(p.max_delta_e, 3)}，合计 ${num(p.sum_delta_e, 3)}`,
    }),
  ]));

  const table = h("table", { class: "rf-table deltas" });
  table.append(h("thead", {}, h("tr", {}, [
    h("th", { text: "要保颜色" }), h("th", { text: "采用旧格" }),
    h("th", { text: "该着色差 ΔE76" }), h("th", { text: "限额" }), h("th", { text: "结论" }),
  ])));
  const tb = h("tbody");
  for (const c of p.cells) {
    tb.append(h("tr", { class: c.passes ? "pass" : "fail" }, [
      h("td", { text: `${c.target}（${roleLabel(c.role)}）` }),
      h("td", { text: c.address }),
      h("td", { text: num(c.delta_e, 3) }),
      h("td", { text: num(c.limit, 2) }),
      h("td", { text: c.passes ? "过限额 ✓" : "超限额 ✗" }),
    ]));
  }
  table.append(tb);
  card.append(h("div", { class: "rf-table-scroll" }, [table]));

  const cost = p.cost || {};
  const costBits = [];
  if (cost.base_extra_delta_e != null) costBits.push(`相对底色最佳：底色多 ${num(cost.base_extra_delta_e, 3)}`);
  if (cost.band_extra_delta_e != null) costBits.push(`相对色带最佳：色带多 ${num(cost.band_extra_delta_e, 3)}`);
  if (costBits.length) card.append(h("p", { class: "rf-cost", text: "选择的代价 → " + costBits.join("；") }));

  const matLine = p.materials
    .map(m => `${m.name} ${m.grams}g${m.stock != null ? ` / 存量 ${m.stock}g` : ""}`)
    .join("，");
  card.append(h("p", { class: "rf-materials-line", text: "各杯合计：" + matLine }));

  const buttons = h("div", { class: "button-row" });
  if (selected) {
    buttons.append(h("span", { class: "rf-adopted-badge", text: "✓ 已采用这组配方" }));
  } else {
    buttons.append(h("button", {
      type: "button", class: "primary small-button",
      text: `采用 ${p.plan_id} 并称料`,
      on: { click: () => helpers.adopt(p.plan_id) },
    }));
    buttons.append(h("button", {
      type: "button", class: "secondary small-button rf-switch",
      text: `换到另一组合格方案（${p.cells.map(c => c.address).join("+")}）`,
      on: { click: () => helpers.selectPlan(p.plan_id, { weigh: false }) },
    }));
  }
  card.append(buttons);
  return card;
}

function roleLabel(role) {
  return { base: "大片底色", band: "窄色带", spot: "普通颜色" }[role] || role;
}
function signed(v) { const n = parseFloat(v); return (n >= 0 ? "+" : "") + num(n, 3); }

// ---------------------------------------------------------------------------
// Weighing
// ---------------------------------------------------------------------------

function weighSection(ui, helpers) {
  const plan = ui.result.plans.find(p => p.plan_id === ui.selectedId) || ui.result.plan;
  const sec = h("section", { class: "rf-weigh", "aria-label": "称量表重新核对" });
  sec.append(h("h3", { text: `称量表 · 按采用方案 ${plan.plan_id} 核对` }));
  sec.append(h("p", { class: "help", text:
    "在秤刻度、每杯目标克数、每料可偏半格与原料上限下核对。先称好的杯把实数填入并保留；"
    + "系统按实数扣库存，只重新核对剩下的杯，不会更改已称数量或偷换配方。" }));

  const bag = ui.actuals[plan.plan_id] || (ui.actuals[plan.plan_id] = {});
  for (const cup of plan.cups) sec.append(cupWeighTable(ui, cup, bag));

  sec.append(h("div", { class: "button-row" }, [
    h("button", {
      type: "button", class: "primary", id: "rf-recheck",
      text: "重新核对剩下的杯",
      on: { click: e => helpers.recheck(e.currentTarget) },
    }),
    h("button", {
      type: "button", class: "secondary small-button",
      text: "把第一杯按示范实数填入",
      on: { click: () => helpers.fillDemoActuals() },
    }),
    h("button", {
      type: "button", class: "quiet small-button",
      text: "第一杯改按采用刻度填入",
      on: { click: () => helpers.fillAdoptedActuals() },
    }),
    h("button", {
      type: "button", class: "secondary",
      text: "归档此方案与称料核对",
      on: { click: e => helpers.archive(e.currentTarget) },
    }),
  ]));

  if (ui.weigh) sec.append(weighResult(ui.weigh));
  return sec;
}

function cupWeighTable(ui, cup, bag) {
  if (!bag[cup.cup_id]) {
    bag[cup.cup_id] = Object.fromEntries(cup.weights.map(w => [w.name, ""]));
  }
  const actuals = bag[cup.cup_id];
  const wrap = h("div", { class: "rf-cup" });
  wrap.append(h("h4", { text: `杯：${cup.label}${cup.address ? `（旧格 ${cup.address}）` : ""}` }));
  const table = h("table", { class: "rf-table weigh" });
  table.append(h("thead", {}, h("tr", {}, [
    h("th", { text: "原料" }), h("th", { text: "目标 g" }), h("th", { text: "半格容差" }),
    h("th", { text: "采用刻度 g" }), h("th", { text: "实称已入杯 g" }),
  ])));
  const tb = h("tbody");
  for (const w of cup.weights) {
    if (!(w.name in actuals)) actuals[w.name] = "";
    const input = h("input", {
      type: "number", step: ui.draft.precision || "0.1", min: "0",
      value: actuals[w.name], "aria-label": `${cup.label} ${w.name} 实称克数`,
      placeholder: "未称", style: "width:96px",
      on: { input: e => { actuals[w.name] = e.target.value; } },
    });
    tb.append(h("tr", {}, [
      h("td", { text: w.name }),
      h("td", { text: num(w.target_g ?? w.grams, 3) }),
      h("td", { text: `±${num(parseFloat(ui.draft.precision || "0.1") / 2, 3)}` }),
      h("td", { text: w.grams }),
      h("td", {}, [input]),
    ]));
  }
  table.append(tb);
  wrap.append(h("div", { class: "rf-table-scroll" }, [table]));
  return wrap;
}

function weighResult(w) {
  const wrap = h("div", { class: "rf-weigh-result" });
  const bad = w.status === "pending";
  wrap.append(h("div", { class: "rf-banner " + (bad ? "bad" : "good"), role: "status" }, [
    h("strong", { text: `核对状态：${w.status_label}` }),
    h("span", {
      text: bad
        ? "余料或刻度无法凑齐，先停在“待核”，未改动采用配方。"
        : w.status === "complete" ? "所有杯均已称完。" : "剩下的杯可在半格容差与存量内称取。",
    }),
  ]));
  if (w.issues?.length) {
    wrap.append(h("ul", { class: "rf-issues" }, w.issues.map(i => h("li", { text: i.message }))));
  }
  if (w.stock?.length) {
    const t = h("table", { class: "rf-table stock-check" });
    t.append(h("thead", {}, h("tr", {}, [
      h("th", { text: "原料" }), h("th", { text: "上限 g" }),
      h("th", { text: "已称实数占用 g" }), h("th", { text: "称后余料 g" }),
      h("th", { text: "剩余杯需 g" }), h("th", { text: "核后余量 g" }), h("th", { text: "结论" }),
    ])));
    const tb = h("tbody");
    for (const s of w.stock) {
      tb.append(h("tr", { class: s.sufficient ? "pass" : "fail" }, [
        h("td", { text: s.name }), h("td", { text: s.limit_g }),
        h("td", { text: s.weighed_actual_g }),
        h("td", { text: s.available_after_weighed_g }),
        h("td", { text: s.remaining_cups_g + (s.remaining_is_minimum ? "（至少）" : "") }),
        h("td", { text: s.remaining_after_g }),
        h("td", { text: s.sufficient ? "够" : "不足 · 待核" }),
      ]));
    }
    t.append(tb);
    wrap.append(h("div", { class: "rf-table-scroll" }, [t]));
  }
  for (const cup of w.cups || []) {
    if (cup.state === "weighed") {
      wrap.append(h("p", { class: "rf-weighed-note",
        text: `已称杯「${cup.label}」按实数保留：`
          + Object.entries(cup.actual).map(([n, v]) => `${n} ${v}g`).join("，")
          + (cup.issues?.length ? "（" + cup.issues.map(i => i.message).join("；") + "）" : "") }));
    } else if (cup.state === "ready") {
      wrap.append(h("p", { class: "rf-ready-note",
        text: `剩余杯「${cup.label}」可称：`
          + cup.rows.map(r => `${r.name} ${r.planned_grams}g`
            + (r.adopted_kept === false ? "（半格内取另一刻度）" : "（沿用采用刻度）")).join("，") }));
    } else if (cup.state === "pending") {
      wrap.append(h("p", { class: "rf-pending-note",
        text: `剩余杯「${cup.label}」待核：` + (cup.issues || []).map(i => i.message).join("；") }));
    }
  }
  return wrap;
}
