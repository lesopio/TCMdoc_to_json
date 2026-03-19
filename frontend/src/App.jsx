import { useEffect, useMemo, useState } from "react";

const API_BASE = "http://127.0.0.1:8000";

const initialForm = {
  input: "",
  envFile: ".env",
  onlyTerm: "",
  limit: "",
  concurrency: String(import.meta.env.VITE_DEFAULT_CONCURRENCY || "8"),
  skipLlm: false,
  resume: false,
  sleep: "0",
};

function StatCard({ label, value, hint }) {
  return (
    <div className="card stat-card">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value ?? "-"}</div>
      {hint ? <div className="stat-hint">{hint}</div> : null}
    </div>
  );
}

function ProgressBar({ value }) {
  const safe = Math.max(0, Math.min(100, value || 0));
  return (
    <div className="progress-shell">
      <div className="progress-fill" style={{ width: `${safe}%` }} />
    </div>
  );
}

function App() {
  const [form, setForm] = useState(initialForm);
  const [jobId, setJobId] = useState("");
  const [jobState, setJobState] = useState(null);
  const [catalogReport, setCatalogReport] = useState(null);
  const [logs, setLogs] = useState([]);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const progress = useMemo(() => {
    const snapshot = jobState?.snapshot || {};
    const entriesTotal = snapshot.entries_total || 0;
    const entriesProcessed = snapshot.entries_processed || 0;
    const sensesTotal = snapshot.senses_total || 0;
    const sensesProcessed = snapshot.senses_processed || 0;
    return {
      entriesPercent: entriesTotal ? (entriesProcessed / entriesTotal) * 100 : 0,
      sensesPercent: sensesTotal ? (sensesProcessed / sensesTotal) * 100 : 0,
      entriesTotal,
      entriesProcessed,
      sensesTotal,
      sensesProcessed,
    };
  }, [jobState]);

  useEffect(() => {
    if (!jobId) return undefined;
    const source = new EventSource(`${API_BASE}/api/jobs/${jobId}/events`);
    source.onmessage = (event) => {
      const data = JSON.parse(event.data);
      setLogs((prev) => [data, ...prev].slice(0, 20));
      setJobState((prev) => ({
        ...(prev || {}),
        id: jobId,
        state: data.state || prev?.state,
        snapshot: {
          ...(prev?.snapshot || {}),
          ...data,
        },
      }));
      if (data.catalog_report) {
        setCatalogReport(data.catalog_report);
      }
      if (data.type === "job_completed") {
        fetch(`${API_BASE}/api/jobs/${jobId}/catalog-report`)
          .then((res) => res.json())
          .then(setCatalogReport)
          .catch(() => {});
      }
    };
    source.onerror = () => {
      source.close();
    };
    return () => source.close();
  }, [jobId]);

  async function startJob(event) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    setLogs([]);
    setCatalogReport(null);
    try {
      const payload = {
        input: form.input || undefined,
        envFile: form.envFile || ".env",
        onlyTerm: form.onlyTerm || undefined,
        limit: form.limit ? Number(form.limit) : undefined,
        concurrency: form.concurrency ? Number(form.concurrency) : undefined,
        skipLlm: form.skipLlm,
        resume: form.resume,
        sleep: form.sleep ? Number(form.sleep) : 0,
      };
      const response = await fetch(`${API_BASE}/api/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || "创建任务失败");
      }
      setJobId(data.jobId);
      const stateResp = await fetch(`${API_BASE}/api/jobs/${data.jobId}`);
      const stateData = await stateResp.json();
      setJobState(stateData);
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  async function changeJobState(action) {
    if (!jobId) return;
    setError("");
    try {
      const response = await fetch(`${API_BASE}/api/jobs/${jobId}/${action}`, {
        method: "POST",
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || `${action} 失败`);
      }
      setJobState((prev) => ({
        ...(prev || {}),
        state: data.state,
        snapshot: {
          ...(prev?.snapshot || {}),
          state: data.state,
        },
      }));
    } catch (err) {
      setError(err.message);
    }
  }

  const snapshot = jobState?.snapshot || {};
  const metrics = snapshot.metrics_summary || jobState?.result?.metrics_summary || {};
  const trace = snapshot.last_llm_trace || null;
  const canDownload = Boolean(jobId) && jobState?.state === "completed";
  const canPause = Boolean(jobId) && jobState?.state === "running";
  const canResume = Boolean(jobId) && jobState?.state === "paused";

  return (
    <div className="app-shell">
      <header className="hero">
        <div>
          <h1>黄帝内经大词典解析面板</h1>
          <p>启动任务、看目录对账、实时观察 token 消耗和模型输入输出。</p>
        </div>
        <div className="hero-badge">{jobState?.state || "idle"}</div>
      </header>

      <section className="grid top-grid">
        <form className="card control-card" onSubmit={startJob}>
          <h2>任务控制</h2>
          <label>
            输入文件
            <input value={form.input} onChange={(e) => setForm((prev) => ({ ...prev, input: e.target.value }))} placeholder="留空则使用目录唯一 txt" />
          </label>
          <label>
            环境文件
            <input value={form.envFile} onChange={(e) => setForm((prev) => ({ ...prev, envFile: e.target.value }))} />
          </label>
          <label>
            指定词条
            <input value={form.onlyTerm} onChange={(e) => setForm((prev) => ({ ...prev, onlyTerm: e.target.value }))} placeholder="例如：天府" />
          </label>
          <div className="inline-fields">
            <label>
              限制条数
              <input value={form.limit} onChange={(e) => setForm((prev) => ({ ...prev, limit: e.target.value }))} />
            </label>
            <label>
              并发数量
              <input value={form.concurrency} onChange={(e) => setForm((prev) => ({ ...prev, concurrency: e.target.value }))} />
            </label>
            <label>
              请求间隔秒数
              <input value={form.sleep} onChange={(e) => setForm((prev) => ({ ...prev, sleep: e.target.value }))} />
            </label>
          </div>
          <div className="checkbox-row">
            <label><input type="checkbox" checked={form.skipLlm} onChange={(e) => setForm((prev) => ({ ...prev, skipLlm: e.target.checked }))} /> 跳过 LLM</label>
            <label><input type="checkbox" checked={form.resume} onChange={(e) => setForm((prev) => ({ ...prev, resume: e.target.checked }))} /> Resume</label>
          </div>
          <button type="submit" disabled={submitting}>{submitting ? "启动中..." : "开始解析"}</button>
          <div className="download-row">
            <button type="button" className="download-btn" disabled={!canPause} onClick={() => changeJobState("pause")}>
              暂停
            </button>
            <button type="button" className="download-btn" disabled={!canResume} onClick={() => changeJobState("resume")}>
              继续
            </button>
          </div>
          <div className="download-row">
            <a
              className={`download-btn ${canDownload ? "" : "disabled"}`}
              href={canDownload ? `${API_BASE}/api/jobs/${jobId}/download/final-json` : undefined}
              download={canDownload ? `${jobId}.final.json` : undefined}
            >
              下载总 JSON
            </a>
            <a
              className={`download-btn ${canDownload ? "" : "disabled"}`}
              href={canDownload ? `${API_BASE}/api/jobs/${jobId}/download/token-summary` : undefined}
              download={canDownload ? `${jobId}.token_summary.json` : undefined}
            >
              下载 Token 汇总
            </a>
            <a
              className={`download-btn ${canDownload ? "" : "disabled"}`}
              href={canDownload ? `${API_BASE}/api/jobs/${jobId}/download/catalog-report` : undefined}
              download={canDownload ? `${jobId}.catalog_report.json` : undefined}
            >
              下载目录对账
            </a>
            <a
              className={`download-btn ${canDownload ? "" : "disabled"}`}
              href={canDownload ? `${API_BASE}/api/jobs/${jobId}/download/entries-zip` : undefined}
              download={canDownload ? `${jobId}.entries.zip` : undefined}
            >
              下载词条 ZIP
            </a>
          </div>
          {jobId ? <div className="muted">当前任务: {jobId}</div> : null}
          {error ? <div className="error">{error}</div> : null}
        </form>

        <div className="card progress-card">
          <h2>总体进度</h2>
          <div className="progress-block">
            <div className="progress-head">
              <span>词条进度</span>
              <span>{progress.entriesProcessed} / {progress.entriesTotal}</span>
            </div>
            <ProgressBar value={progress.entriesPercent} />
          </div>
          <div className="progress-block">
            <div className="progress-head">
              <span>词性块进度</span>
              <span>{progress.sensesProcessed} / {progress.sensesTotal}</span>
            </div>
            <ProgressBar value={progress.sensesPercent} />
          </div>
          <div className="status-grid">
            <StatCard label="当前状态" value={jobState?.state || "-"} />
            <StatCard label="最近词条" value={snapshot.term || "-"} />
            <StatCard label="最近词性" value={snapshot.pos || "-"} />
            <StatCard label="LLM 启用" value={snapshot.llm_enabled === false ? "否" : "是"} />
          </div>
        </div>
      </section>

      <section className="grid metric-grid">
        <StatCard label="预估输入 Token" value={metrics.estimated_prompt_tokens} />
        <StatCard label="实际输入 Token" value={metrics.actual_prompt_tokens} />
        <StatCard label="实际输出 Token" value={metrics.actual_completion_tokens} />
        <StatCard label="总 Token" value={metrics.actual_total_tokens} />
        <StatCard label="Token 速度" value={metrics.total_tokens_per_sec} hint="tokens/sec" />
        <StatCard label="请求耗时" value={metrics.request_duration_ms} hint="ms" />
      </section>

      <section className="grid detail-grid">
        <div className="card detail-card">
          <h2>模型输入输出</h2>
          <div className="detail-meta">
            <div>模型: {trace?.model || "-"}</div>
            <div>词条: {trace?.term || "-"}</div>
            <div>词性: {trace?.pos || "-"}</div>
            <div>原文长度: {trace?.raw_text_length || "-"}</div>
          </div>
          <div className="two-col">
            <div>
              <h3>输入预览</h3>
              <pre>{trace?.request_preview || "暂无"}</pre>
            </div>
            <div>
              <h3>输出预览</h3>
              <pre>{trace?.response_preview || trace?.error || "暂无"}</pre>
            </div>
          </div>
        </div>

        <div className="card detail-card">
          <h2>词目录对账</h2>
          <div className="status-grid">
            <StatCard label="目录词数" value={catalogReport?.catalog_terms_total} />
            <StatCard label="正文词数" value={catalogReport?.body_terms_total} />
            <StatCard label="匹配词数" value={catalogReport?.matched_terms} />
            <StatCard label="匹配率" value={catalogReport?.match_rate} />
          </div>
          <div className="two-col">
            <div>
              <h3>目录独有</h3>
              <pre>{catalogReport?.catalog_only_terms?.join("\n") || "暂无"}</pre>
            </div>
            <div>
              <h3>正文独有</h3>
              <pre>{catalogReport?.body_only_terms?.join("\n") || "暂无"}</pre>
            </div>
          </div>
        </div>
      </section>

      <section className="card log-card">
        <h2>实时日志</h2>
        <div className="log-list">
          {logs.length === 0 ? <div className="muted">暂无日志</div> : null}
          {logs.map((log, index) => (
            <div className="log-item" key={`${log.timestamp}-${index}`}>
              <div className="log-type">{log.type || "event"}</div>
              <pre>{JSON.stringify(log, null, 2)}</pre>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

export default App;
