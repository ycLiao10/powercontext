import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  BACKEND,
  backendState,
  currentPref,
  embedText,
  embedTexts,
  loadModel,
  modelInfo,
  setBackendPref,
} from './lib/browser-embedder.js';
import { BACKEND_PREF } from './lib/backend-pref.js';
import * as api from './lib/api.js';
import { parseDocuments, readFile } from './lib/parse.js';

const BATCH_SIZE = 8;

/** 后端偏好选项：自动 / WebGPU / WASM。 */
const PREF_OPTIONS = [
  {
    value: BACKEND_PREF.AUTO,
    label: '自动',
    hint: '探测 WebGPU，可用就用 WebGPU，否则退回 CPU 推理',
  },
  {
    value: BACKEND_PREF.WEBGPU,
    label: 'WebGPU',
    hint: '强制 WebGPU；不可用时回落到自动',
  },
  {
    value: BACKEND_PREF.WASM,
    label: 'WASM',
    hint: '强制 WASM；核显上 q8 量化模型通常更快',
  },
];

/**
 * 后端徽标：右上角常驻显示当前推理后端。
 *
 * 可点击展开下拉，手动切换「自动 / WebGPU / WASM」。
 * 选择写入 localStorage，刷新后保持。
 */
function BackendBadge({ state }) {
  const [open, setOpen] = useState(false);
  const [pref, setPref] = useState(currentPref());
  const wrapRef = useRef(null);

  const isWebGPU = state.device === BACKEND.WEBGPU;
  const loading = state.device === null;
  const label = loading ? '加载中' : isWebGPU ? 'WebGPU' : 'WASM';
  const cls = loading ? 'badge badge-loading' : isWebGPU ? 'badge badge-webgpu' : 'badge badge-wasm';
  const prefLabel = PREF_OPTIONS.find((o) => o.value === pref)?.label ?? '自动';

  // 点击外部关闭下拉
  useEffect(() => {
    if (!open) return undefined;
    const onDocClick = (e) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const pick = (value) => {
    setOpen(false);
    if (value === pref) return;
    setPref(value);
    setBackendPref(value);
  };

  return (
    <div className="badge-wrap" ref={wrapRef}>
      <button
        type="button"
        className={`${cls} badge-button`}
        aria-haspopup="listbox"
        aria-expanded={open}
        title={
          loading
            ? '正在加载模型并探测推理后端'
            : `推理后端：${label}（模型 ${state.model}，dtype ${state.dtype}，维度 ${state.dimension}）` +
              `\n当前选择：${prefLabel}（点击可切换）` +
              (state.webgpuError ? `\nWebGPU 降级原因：${state.webgpuError}` : '')
        }
        onClick={() => setOpen((v) => !v)}
      >
        <span className="badge-dot" />
        <span className="badge-text">推理后端</span>
        <span className="badge-value">{label}</span>
        <span className="badge-caret">▾</span>
      </button>

      {open ? (
        <div className="badge-menu" role="listbox" aria-label="选择推理后端">
          {PREF_OPTIONS.map((o) => (
            <button
              key={o.value}
              type="button"
              role="option"
              aria-selected={o.value === pref}
              data-value={o.value}
              className={`badge-option ${o.value === pref ? 'badge-option-active' : ''}`}
              onClick={() => pick(o.value)}
              title={o.hint}
            >
              <span className="badge-option-label">
                {o.value === pref ? '● ' : '○ '}
                {o.label}
              </span>
              <span className="badge-option-hint">{o.hint}</span>
            </button>
          ))}
          <div className="badge-menu-foot">
            选择会保存在本机 localStorage，刷新后保持。
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** 进度条：向量化 / 写入阶段共用。 */
function Progress({ stage, done, total, note }) {
  if (!stage) return null;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  return (
    <div className="progress">
      <div className="progress-head">
        <span className="progress-stage">{stage}</span>
        <span className="progress-count">
          {total > 0 ? `${done} / ${total}` : ''}
        </span>
      </div>
      <div className="progress-track">
        <div className="progress-fill" style={{ width: `${pct}%` }} />
      </div>
      {note ? <div className="progress-note">{note}</div> : null}
    </div>
  );
}

export default function App() {
  const [backend, setBackend] = useState({ ...backendState });
  const [modelReady, setModelReady] = useState(false);
  const [modelError, setModelError] = useState(null);

  const [rawText, setRawText] = useState('');
  const [fileName, setFileName] = useState('');
  const [docs, setDocs] = useState([]);

  const [importStage, setImportStage] = useState(null);
  const [importDone, setImportDone] = useState(0);
  const [importTotal, setImportTotal] = useState(0);
  const [importNote, setImportNote] = useState('');
  const [importMsg, setImportMsg] = useState(null);

  const [query, setQuery] = useState('');
  const [mode, setMode] = useState('hybrid');
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState(null);
  const [searchMeta, setSearchMeta] = useState(null);
  const [error, setError] = useState(null);

  const [kbInfo, setKbInfo] = useState(null);
  const fileInputRef = useRef(null);

  const refreshBackend = useCallback(() => setBackend({ ...backendState }), []);

  // 探活
  useEffect(() => {
    api.health().then(setKbInfo).catch((e) => setError(`代理连接失败：${e.message}`));
  }, []);

  // 预热模型（不等加载完成，界面可先交互）
  useEffect(() => {
    let cancelled = false;
    loadModel()
      .then(() => {
        if (!cancelled) {
          setModelReady(true);
          refreshBackend();
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setModelError(e.message);
          refreshBackend();
        }
      });
    return () => {
      cancelled = true;
    };
  }, [refreshBackend]);

  // 如果上一轮是「WebGPU 建会话失败 → 自动切 WASM 重新加载」，把原因显示出来
  useEffect(() => {
    try {
      const reason = sessionStorage.getItem('stepfun:webgpu-error');
      if (reason) {
        sessionStorage.removeItem('stepfun:webgpu-error');
        backendState.webgpuError = reason;
        setBackend({ ...backendState });
      }
    } catch {
      /* 忽略 */
    }
  }, []);

  const webgpuHint = useMemo(() => {
    if (backend.device === BACKEND.WEBGPU) return null;
    // 一次性提示优先（如「手动选的 WebGPU 不可用，已回落到自动」）
    if (backend.notice) return backend.notice;
    const reason = backend.webgpuError || backend.webgpuProbe?.reason;
    if (reason) return `WebGPU 不可用，已降级 WASM：${reason}`;
    return null;
  }, [backend]);

  // ---------- 导入 ----------
  const onFileChange = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setFileName(file.name);
    const text = await readFile(file);
    setRawText(text);
    setDocs(parseDocuments(text, file.name.replace(/\.[^.]+$/, '')));
  };

  const onTextChange = (e) => {
    setRawText(e.target.value);
    setFileName('粘贴的文本');
    setDocs(parseDocuments(e.target.value, 'pasted'));
  };

  const runImport = async () => {
    if (!docs.length) return;
    setError(null);
    setImportMsg(null);
    try {
      // 1) 浏览器内向量化（分批，带进度）
      setImportStage('浏览器内向量化');
      setImportTotal(docs.length);
      setImportDone(0);
      setImportNote('向量在浏览器内计算，未上传任何文本');

      const vectors = [];
      for (let i = 0; i < docs.length; i += BATCH_SIZE) {
        const batch = docs.slice(i, i + BATCH_SIZE);
        const batchVectors = await embedTexts(
          batch.map((d) => `${d.title}。${d.content}`),
          (done, total) => {
            setImportDone(i + done);
            setImportTotal(docs.length);
          },
        );
        vectors.push(...batchVectors);
      }
      setImportDone(docs.length);

      // 2) 连同向量一起提交给代理写入 seekdb
      setImportStage('写入 seekdb');
      setImportNote('向量 + 原文 + 元数据一起落库，索引同步模式为 IMMEDIATE');
      const payload = docs.map((d, i) => ({
        id: d.id,
        title: d.title,
        content: d.content,
        embedding: vectors[i],
        metadata: d.metadata || {},
      }));
      const res = await api.writeDocuments(payload);

      setImportStage(null);
      setImportMsg(`✓ 已导入 ${res.inserted} 条文档，知识库现有 ${res.documents} 条`);
      const info = await api.health();
      setKbInfo(info);
    } catch (e) {
      setImportStage(null);
      setError(`导入失败：${e.message}`);
    }
  };

  // ---------- 检索 ----------
  const runSearch = async (e) => {
    e?.preventDefault();
    if (!query.trim()) return;
    setError(null);
    setSearching(true);
    setSearchMeta(null);
    const t0 = performance.now();
    try {
      let embedding;
      if (mode !== 'fulltext') {
        embedding = await embedText(query);
      }
      const tEmbed = performance.now();
      const res = await api.search({ query, embedding, mode, topK: 5 });
      const tSearch = performance.now();
      setResults(res.results);
      setSearchMeta({ tookMs: res.tookMs, mode: res.mode, count: res.count });
      // 分阶段耗时：给基准脚本（scripts/bench-browser.mjs）交叉验证真实界面用
      window.__stepfunLastQuery = {
        mode,
        embedMs: Number((tEmbed - t0).toFixed(2)),
        searchMs: Number((tSearch - tEmbed).toFixed(2)),
        serverSearchMs: res.tookMs,
        totalMs: Number((performance.now() - t0).toFixed(2)),
        count: res.count,
      };
    } catch (err) {
      setError(`检索失败：${err.message}`);
      setResults(null);
    } finally {
      setSearching(false);
    }
  };

  const clearAll = async () => {
    setError(null);
    try {
      await api.clearDocuments();
      setResults(null);
      setSearchMeta(null);
      const info = await api.health();
      setKbInfo(info);
      setImportMsg(null);
    } catch (e) {
      setError(`清空失败：${e.message}`);
    }
  };

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <h1>seekdb 混合检索</h1>
          <span className="subtitle">浏览器内本地向量化 · 向量+全文在 seekdb 内一条 SQL 融合</span>
        </div>
        <div className="topbar-right">
          {kbInfo ? (
            <div className="kb-chip" title={`${kbInfo.seekdb}/${kbInfo.database}.${kbInfo.table}`}>
              <span className="kb-label">知识库</span>
              <span className="kb-value">{kbInfo.documents} 条</span>
            </div>
          ) : null}
          <BackendBadge state={backend} />
        </div>
      </header>

      {webgpuHint ? <div className="hint">{webgpuHint}</div> : null}
      {modelError ? <div className="hint hint-error">模型加载失败：{modelError}</div> : null}
      {error ? <div className="hint hint-error">{error}</div> : null}

      <main className="columns">
        {/* ---------------- 左栏：导入 ---------------- */}
        <section className="panel panel-left">
          <h2 className="panel-title">1 · 导入文档</h2>

          <div className="field">
            <label htmlFor="file">选择文件（.txt / .jsonl）</label>
            <input
              id="file"
              ref={fileInputRef}
              type="file"
              accept=".txt,.jsonl,.md,text/plain,application/json"
              onChange={onFileChange}
            />
          </div>

          <div className="field">
            <label htmlFor="paste">或直接粘贴文本</label>
            <textarea
              id="paste"
              value={rawText}
              onChange={onTextChange}
              placeholder={'# 标题\n正文内容，空行分段。\n\n# 另一篇\n第二篇正文。'}
              rows={10}
            />
          </div>

          <div className="parse-info">
            {fileName ? <span className="tag">{fileName}</span> : null}
            <span className="tag tag-count">已解析 {docs.length} 篇</span>
          </div>

          <Progress
            stage={importStage}
            done={importDone}
            total={importTotal}
            note={importNote}
          />

          <div className="actions">
            <button
              className="btn btn-primary"
              onClick={runImport}
              disabled={!docs.length || !!importStage || !modelReady}
            >
              {importStage ? '处理中…' : '向量化并导入'}
            </button>
            <button className="btn" onClick={clearAll} disabled={!kbInfo?.documents}>
              清空知识库
            </button>
          </div>

          {importMsg ? <div className="ok-msg">{importMsg}</div> : null}

          <div className="meta-box">
            <div className="meta-row">
              <span>模型</span>
              <code>{backend.model}</code>
            </div>
            <div className="meta-row">
              <span>量化</span>
              <code>{backend.dtype}</code>
            </div>
            <div className="meta-row">
              <span>维度</span>
              <code>{backend.dimension}</code>
            </div>
            <div className="meta-row">
              <span>权重源</span>
              <code>{backend.remoteHost}</code>
            </div>
            <div className="meta-row">
              <span>权重来源</span>
              <code>{backend.weightSource}</code>
            </div>
          </div>
        </section>

        {/* ---------------- 右栏：检索 ---------------- */}
        <section className="panel panel-right">
          <h2 className="panel-title">2 · 混合检索</h2>

          <form onSubmit={runSearch} className="search-form">
            <input
              className="search-input"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="输入查询，例如：缓存雪崩和穿透怎么处理"
            />
            <button className="btn btn-primary" type="submit" disabled={searching || !query.trim()}>
              {searching ? '检索中…' : '检索'}
            </button>
          </form>

          <div className="mode-switch" role="group" aria-label="检索模式">
            {[
              ['hybrid', '混合（向量+全文）'],
              ['vector', '纯向量'],
              ['fulltext', '纯全文'],
            ].map(([value, label]) => (
              <button
                key={value}
                type="button"
                className={`mode-btn ${mode === value ? 'mode-btn-active' : ''}`}
                onClick={() => setMode(value)}
              >
                {label}
              </button>
            ))}
          </div>

          {searchMeta ? (
            <div className="search-meta">
              命中 <strong>{searchMeta.count}</strong> 条 · 检索耗时{' '}
              <strong>{searchMeta.tookMs}</strong> ms · 模式 <code>{searchMeta.mode}</code>
            </div>
          ) : null}

          <div className="results">
            {results && results.length === 0 ? (
              <div className="empty">没有匹配的结果。</div>
            ) : null}
            {results
              ? results.map((r, i) => (
                  <article className="result-card" key={r.id}>
                    <div className="result-head">
                      <span className="result-rank">#{i + 1}</span>
                      <h3 className="result-title">{r.title || r.id}</h3>
                      <span className="result-score" title="RRF 融合分数">
                        {Number(r.hybrid_score ?? r.distance ?? r.relevance).toFixed(6)}
                      </span>
                    </div>
                    <div className="result-tags">
                      {r.fulltext_rank !== undefined ? (
                        <span className="rt rt-ft">
                          全文命中 rank={r.fulltext_rank}
                        </span>
                      ) : null}
                      {r.vector_rank !== undefined ? (
                        <span className="rt rt-vec">向量命中 rank={r.vector_rank}</span>
                      ) : null}
                      {r.metadata?.domain ? (
                        <span className="rt rt-domain">{r.metadata.domain}</span>
                      ) : null}
                    </div>
                    <p className="result-content">{String(r.content || '').slice(0, 220)}</p>
                  </article>
                ))
              : null}
            {!results && !searching ? (
              <div className="empty">
                先在左侧导入文档，再输入查询。向量在浏览器内计算后提交给本地代理，
                混合检索由 seekdb 内的一条 SQL 完成。
              </div>
            ) : null}
          </div>
        </section>
      </main>

      <footer className="footer">
        文档向量与查询向量均在浏览器内由 {backend.model}（{backend.dtype}）计算，
        不调用任何云端推理 API；向量与原文存于 seekdb，混合检索在数据库内一条 SQL 完成。
      </footer>
    </div>
  );
}
