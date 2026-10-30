const CHANNEL_LABEL = { lexical: "词法 BM25", vector: "向量语义" };
const CHANNEL_CLASS = { lexical: "lex", vector: "vec" };

const tabs = [...document.querySelectorAll(".tab")];
const hitsEl = document.getElementById("demoHits");
const channelsEl = document.getElementById("demoChannels");
const memoryEl = document.getElementById("demoMemory");

let demo = null;

function escapeHtml(value) {
  return String(value).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function renderCase(item) {
  channelsEl.textContent = item.channels_used.map((name) => CHANNEL_LABEL[name] || name).join(" + ");
  memoryEl.innerHTML = item.memory_reused
    ? '<span class="badge mem">已复用导入记忆</span>'
    : '<span class="badge">未命中记忆</span>';

  hitsEl.innerHTML = item.results
    .map((hit) => {
      const badges = hit.channels
        .map((name) => `<span class="badge ${CHANNEL_CLASS[name] || ""}">${CHANNEL_LABEL[name] || name}</span>`)
        .join("");
      const memory = hit.memory_boost ? '<span class="badge mem">按阅读记忆加权</span>' : "";
      return `
        <li class="hit">
          <div class="hit-head">
            <span class="hit-title">《${escapeHtml(hit.title)}》</span>
            <span>第 ${hit.ordinal} 段 · 字符 ${hit.offset[0]}–${hit.offset[1]}</span>
            <span class="hit-score">相关度 ${Number(hit.score).toFixed(1)}</span>
            ${badges}${memory}
          </div>
          <p class="hit-text">${escapeHtml(hit.text)}</p>
        </li>`;
    })
    .join("");
}

function select(index) {
  tabs.forEach((tab, i) => {
    const active = i === index;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  renderCase(demo.cases[index]);
}

tabs.forEach((tab) => {
  tab.addEventListener("click", () => select(Number(tab.dataset.index)));
  tab.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    const next = (Number(tab.dataset.index) + (event.key === "ArrowDown" ? 1 : tabs.length - 1)) % tabs.length;
    tabs[next].focus();
    select(next);
  });
});

fetch("data/demo.json")
  .then((response) => {
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  })
  .then((payload) => {
    demo = payload;
    document.getElementById("factModel").textContent = `${payload.embedding.model} · ${payload.embedding.dimension} 维`;
    document.getElementById("factModes").textContent = payload.search_modes.join(" · ");
    document.getElementById("factScale").textContent = `${payload.documents} 篇文献 / ${payload.chunks} 个分块`;
    select(0);
  })
  .catch(() => {
    hitsEl.innerHTML = '<li class="hit"><p class="hit-text">演示数据加载失败，请直接运行本地工作台。</p></li>';
  });
