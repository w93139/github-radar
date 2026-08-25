const state = { report: null, view: "new", loading: false, requestToken: 0 };
const $ = (selector) => document.querySelector(selector);

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
}

function number(value) { return new Intl.NumberFormat("zh-CN").format(value || 0); }
function list() { return state.view === "new" ? state.report?.new_projects || [] : state.report?.fast_growth || []; }

function setLoading(active) {
  state.loading = active;
  const initialLoad = active && !state.report;
  $("#loading").classList.toggle("hidden", !initialLoad);
  $("#projectList").classList.toggle("hidden", initialLoad);
  $("#refreshButton").disabled = active;
  $("#refreshButton").innerHTML = active ? "扫描中…" : "<span>↻</span> 刷新榜单";
}

function setSearchStatus(message = "") {
  const status = $("#searchStatus");
  status.querySelector("p").textContent = message;
  status.classList.toggle("hidden", !message);
}

function setServiceState(mode = "online") {
  const indicator = $("#serviceState");
  indicator.className = `service-state ${mode}`;
  indicator.querySelector("span").textContent = mode === "degraded" ? "缓存模式" : mode === "recovering" ? "正在恢复" : "本地服务";
}

const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
async function fetchWithRecovery(url) {
  let lastError;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      return await fetch(url, {cache: "no-store"});
    } catch (error) {
      lastError = error;
      setServiceState("recovering");
      if (attempt < 2) await wait(600 * (attempt + 1));
    }
  }
  throw lastError || new Error("本地服务暂时不可用");
}

function params(preferCache = false, force = false, enrich = false, local = false) {
  const query = new URLSearchParams({
    language: $("#language").value.trim() || "all",
    topic: $("#topic").value.trim(), limit: "10",
    new_window_days: "7", growth_hours: "24"
  });
  if (preferCache) query.set("prefer_cache", "1");
  if (force) query.set("force", "1");
  if (enrich) query.set("enrich", "1");
  if (local) query.set("local", "1");
  return query;
}

async function loadReport({preferCache = false, quiet = false, force = false, enrich = false, local = false} = {}) {
  const requestToken = ++state.requestToken;
  state.loading = true;
  if (!quiet) setLoading(true);
  setSearchStatus(enrich ? "榜单已显示，正在补充 README 简介…" : local ? "正在读取本地索引…" : "正在并行搜索 GitHub 数据源…");
  let refreshAfter = false;
  let enrichAfter = false;
  try {
    const response = await fetchWithRecovery(`/api/report?${params(preferCache, force, enrich, local)}`);
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || "加载榜单失败");
    if (requestToken !== state.requestToken) return;
    state.report = data;
    setServiceState(data._ui?.degraded ? "degraded" : "online");
    render();
    if (preferCache && data._ui?.cached) {
      $("#updatedAt").classList.add("cached-badge");
      refreshAfter = Number(data._ui.cache_age_seconds || 0) > 900;
    }
    enrichAfter = !enrich && !refreshAfter && Boolean(data._ui?.needs_enrichment);
    if (data._ui?.local_preview) refreshAfter = true;
  } catch (error) {
    if (requestToken !== state.requestToken) return;
    setServiceState("recovering");
    showNotice(error.message, true);
    if (!state.report) $("#projectList").innerHTML = `<li class="empty error">${escapeHtml(error.message)}</li>`;
  } finally {
    if (requestToken !== state.requestToken) return;
    state.loading = false;
    if (!quiet) setLoading(false);
    if (refreshAfter) {
      setSearchStatus("缓存已显示，正在更新最新榜单…");
      setTimeout(() => loadReport({quiet: true, force: true}), 0);
    } else if (enrichAfter) {
      setTimeout(() => loadReport({quiet: true, enrich: true}), 0);
    } else {
      setSearchStatus("");
    }
  }
}

function showNotice(message, error = false) {
  const notice = $("#notice");
  notice.textContent = message;
  notice.classList.toggle("hidden", !message);
  notice.classList.toggle("error", error);
}

function render() {
  const report = state.report;
  if (!report) return;
  const projects = list();
  const newProjects = report.new_projects || [];
  const growth = report.fast_growth || [];
  const topDelta = Math.max(0, ...growth.map(item => Number(item.star_delta || 0)));
  $("#summary").innerHTML = `
    <article class="metric"><small>新发现项目</small><strong>${number(newProjects.length)}</strong><em>近 ${report.scope?.new_window_days || 7} 天</em></article>
    <article class="metric"><small>增长候选</small><strong>${number(growth.length)}</strong><em>本次有效排名</em></article>
    <article class="metric"><small>最高实测增量</small><strong>+${number(topDelta)}</strong><em>${report.scope?.growth_hours || 24} 小时窗口</em></article>`;
  $("#updatedAt").textContent = `${report._ui?.cached ? "缓存 · " : ""}${formatTime(report.generated_at)}`;
  $("#updatedAt").classList.toggle("cached-badge", Boolean(report._ui?.cached));
  $("#listTitle").textContent = state.view === "new" ? `近 ${report.scope?.new_window_days || 7} 日新项目` : `${report.scope?.growth_hours || 24} 小时增长`;
  $("#scopeLabel").textContent = [report.scope?.language !== "all" ? report.scope?.language : "全部语言", report.scope?.topic].filter(Boolean).join(" · ");
  $("#projectList").innerHTML = projects.length ? projects.map(projectCard).join("") : '<li class="empty">当前筛选条件下没有可用项目</li>';
  $("#highlights").innerHTML = (report.highlights || []).slice(0,3).map(highlightCard).join("") || '<p>完成扫描后显示重点项目。</p>';
  $("#sources").textContent = (report.data_sources || []).map(source => source.name || "数据源").join(" · ");
  const warnings = report.warnings || [];
  showNotice(warnings.length ? warnings.join("；") : "");
}

function projectCard(item) {
  const intro = item.introduction || item.description || "暂无简介";
  const growth = state.view === "growth";
  const rate = Number(item.growth_rate || 0);
  const secondary = growth
    ? `${String(item.growth_source || "").startsWith("snapshot_") ? "本地实测" : "趋势口径"}${rate ? ` · ${rate.toFixed(1)}%` : ""}`
    : `${Number(item.stars_per_day || 0).toFixed(1)} Star / 日`;
  return `<li class="project">
    <span class="rank">${number(item.rank)}</span>
    <div class="project-main">
      <a class="project-name" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.full_name)}</a>
      <p class="description">${escapeHtml(intro)}</p>
      <div class="tags">${[item.language, item.license, ...(item.topics || []).slice(0,2)].filter(Boolean).map(tag => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</div>
    </div>
    <div class="stats"><strong>★ ${number(item.stars)}</strong><span>${growth ? `+${number(item.star_delta)}` : item.first_seen_today ? "NEW" : "已追踪"}</span><small>${escapeHtml(secondary)}</small></div>
  </li>`;
}

function highlightCard(item) {
  if (typeof item === "string") return `<div class="highlight"><p>${escapeHtml(item)}</p></div>`;
  const name = item.full_name || item.name || "重点项目";
  const text = item.introduction || item.description || item.summary || item.reason || "值得继续关注";
  return `<div class="highlight"><a href="${escapeHtml(item.url || "#")}" target="_blank" rel="noopener noreferrer">${escapeHtml(name)}</a><p>${escapeHtml(text)}</p></div>`;
}

function formatTime(value) {
  if (!value) return "刚刚更新";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("zh-CN", {month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit"}).format(date);
}

document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => {
  state.view = tab.dataset.view;
  document.querySelectorAll(".tab").forEach(item => { item.classList.toggle("active", item === tab); item.setAttribute("aria-selected", item === tab ? "true" : "false"); });
  if (state.report) render();
}));
$("#filters").addEventListener("submit", event => {
  event.preventDefault();
  state.report = null;
  $("#summary").innerHTML = "";
  $("#projectList").innerHTML = "";
  loadReport({local: true});
});
$("#refreshButton").addEventListener("click", () => loadReport({force: true}));
loadReport({preferCache: true});
