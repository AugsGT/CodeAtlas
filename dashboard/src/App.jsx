import { useEffect, useState } from "react";

const API_BASE = "http://localhost:8000";
const MAX_HISTORY_TURNS = 5;
const SEVERITIES = ["critical", "high", "medium", "low", "info"];

function StatsBar({ stats }) {
  if (!stats) return null;
  return (
    <div className="stats-bar">
      {Object.entries(stats).map(([label, count]) => (
        <div className="stat" key={label}>
          <span className="stat-count">{count}</span>
          <span className="stat-label">{label}</span>
        </div>
      ))}
    </div>
  );
}

function EvidenceList({ evidence }) {
  if (!evidence) return null;
  return (
    <div className="evidence">
      <div className="evidence-column">
        <h3>Nodes ({evidence.nodes.length})</h3>
        <ul>
          {evidence.nodes.map((node) => {
            const id = node.id ?? node.path;
            return (
              <li key={id}>
                <span className="badge">{node.label}</span> <code>{id}</code>
                {node.name && node.name !== id ? ` — ${node.name}` : ""}
              </li>
            );
          })}
        </ul>
      </div>
      <div className="evidence-column">
        <h3>Edges ({evidence.edges.length})</h3>
        <ul>
          {evidence.edges.map((edge, i) => (
            <li key={i}>
              <code>{edge.from}</code> <span className="badge badge-edge">{edge.type}</span>{" "}
              <code>{edge.to}</code>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function AlertsPanel({ alerts }) {
  if (!alerts) return null;
  return (
    <section className="panel">
      <h2>Alerts ({alerts.length})</h2>
      {alerts.length === 0 ? (
        <p className="meta">No problems detected in the current evidence.</p>
      ) : (
        <ul className="alerts-list">
          {alerts.map((a, i) => (
            <li key={i} className={`alert alert-${a.severity}`}>
              <span className="badge">{a.severity}</span>{" "}
              {a.type && <span className="badge badge-edge">{a.type}</span>} {a.message}
              {a.file && (
                <>
                  {" "}
                  — <code>{a.file}{a.line ? `:${a.line}` : ""}</code>
                </>
              )}
              {a.entity_id && <> — <code>{a.entity_id}</code></>}
              {a.detection_method && <span className="meta"> ({a.detection_method})</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ValidationBanner({ validation }) {
  if (!validation) return null;
  return (
    <div className={validation.is_grounded ? "validation ok" : "validation bad"}>
      <strong>{validation.is_grounded ? "Grounded" : "Ungrounded — issues found"}</strong>
      {validation.warnings.length > 0 && (
        <ul>
          {validation.warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function AnalysisTrace({ turn }) {
  const steps = [
    `Query received: "${turn.question}"`,
    `Intent classified: ${turn.classified_intent}`,
    turn.target && `Target extracted: ${turn.target}`,
    turn.resolved_target.id
      ? `Resolved to ${turn.resolved_target.kind}: ${turn.resolved_target.id}`
      : "No target resolved",
    turn.intent !== turn.classified_intent && `Fell back to '${turn.intent}' evidence retrieval`,
    `Retrieved ${turn.evidence.nodes.length} node(s), ${turn.evidence.edges.length} edge(s)`,
    "Answer generated",
    turn.validation.is_grounded ? "Validation: grounded" : "Validation: ungrounded — see warnings below",
  ].filter(Boolean);

  return (
    <details className="analysis-trace">
      <summary>Analysis Trace</summary>
      <ol>
        {steps.map((step, i) => (
          <li key={i}>✓ {step}</li>
        ))}
      </ol>
    </details>
  );
}

function ProposedFix({ detail, entityId, repoRoot }) {
  const [validating, setValidating] = useState(false);
  const [fixCheck, setFixCheck] = useState(null);
  const [fixCheckError, setFixCheckError] = useState(null);

  const [applying, setApplying] = useState(false);
  const [applyResult, setApplyResult] = useState(null);
  const [applyError, setApplyError] = useState(null);

  if (!detail || (!detail.proposed_fix && !detail.improved_code)) return null;

  const canValidate = Boolean(detail.improved_code && entityId && repoRoot);
  const canApply = canValidate;

  const handleValidate = async () => {
    setValidating(true);
    setFixCheckError(null);
    setFixCheck(null);
    try {
      const res = await fetch(`${API_BASE}/api/validate-fix`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_root: repoRoot, entity_id: entityId, improved_code: detail.improved_code }),
      });
      if (!res.ok) {
        const body = await res.json();
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      setFixCheck(await res.json());
    } catch (err) {
      setFixCheckError(err.message);
    } finally {
      setValidating(false);
    }
  };

  const handleApply = async () => {
    const warning = fixCheck
      ? fixCheck.resolved === false
        ? "Validation showed this fix does NOT resolve the issue. "
        : fixCheck.resolved === true
        ? ""
        : "This fix hasn't been confirmed to resolve the issue (inconclusive). "
      : "This fix hasn't been validated yet. ";
    const confirmed = window.confirm(
      `${warning}Apply this fix to ${entityId} in your real repository now? ` +
        "A timestamped backup of the original file will be created first."
    );
    if (!confirmed) return;

    setApplying(true);
    setApplyError(null);
    setApplyResult(null);
    try {
      const res = await fetch(`${API_BASE}/api/apply-fix`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_root: repoRoot, entity_id: entityId, improved_code: detail.improved_code }),
      });
      if (!res.ok) {
        const body = await res.json();
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      setApplyResult(await res.json());
    } catch (err) {
      setApplyError(err.message);
    } finally {
      setApplying(false);
    }
  };

  return (
    <div className="proposed-fix">
      <h4>Proposed Fix</h4>
      {detail.proposed_fix && <p>{detail.proposed_fix}</p>}
      {detail.improved_code && (
        <pre>
          <code>{detail.improved_code}</code>
        </pre>
      )}
      {detail.confidence_basis && (
        <p className="meta">
          <strong>Confidence basis:</strong> {detail.confidence_basis}
        </p>
      )}
      {detail.limitations && (
        <p className="meta">
          <strong>Limitations:</strong> {detail.limitations}
        </p>
      )}
      {canValidate && (
        <button type="button" className="secondary" onClick={handleValidate} disabled={validating}>
          {validating ? "Validating…" : "Validate Fix"}
        </button>
      )}
      {canApply && (
        <button type="button" className="apply-fix" onClick={handleApply} disabled={applying || applyResult?.applied}>
          {applying ? "Applying…" : applyResult?.applied ? "Applied" : "Apply Fix"}
        </button>
      )}
      {fixCheckError && <p className="error">{fixCheckError}</p>}
      {fixCheck && (
        <div
          className={
            "validation " +
            (fixCheck.resolved === true ? "ok" : fixCheck.resolved === false ? "bad" : "")
          }
        >
          <strong>
            {!fixCheck.attempted
              ? `Could not validate: ${fixCheck.reason}`
              : fixCheck.resolved === true
              ? "Fix confirmed — issue resolved"
              : fixCheck.resolved === false
              ? "Fix did not resolve the issue"
              : "Inconclusive"}
          </strong>
          {fixCheck.attempted && (
            <p className="meta">
              {fixCheck.explanation}
              {fixCheck.isolation_warning && <> {fixCheck.isolation_warning}</>}
            </p>
          )}
        </div>
      )}
      {applyError && <p className="error">{applyError}</p>}
      {applyResult && (
        <div className={"validation " + (applyResult.applied ? "ok" : "bad")}>
          <strong>
            {applyResult.applied
              ? `Applied to ${applyResult.file}`
              : `Could not apply: ${applyResult.reason}`}
          </strong>
          {applyResult.applied && (
            <p className="meta">
              Original backed up to <code>{applyResult.backup_path}</code>. Re-ingest the
              repository to refresh evidence against the updated source.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function ConversationTurn({ turn, repoRoot }) {
  const entityId = turn.resolved_target.kind === "entity" ? turn.resolved_target.id : null;
  return (
    <div className="turn">
      <p className="turn-question">{turn.question}</p>
      <p className="meta">
        Intent: <span className="badge">{turn.intent}</span>
        {turn.resolved_target.id && (
          <>
            {" "}
            · Target: <code>{turn.resolved_target.id}</code>
          </>
        )}
      </p>
      <p className="answer">{turn.answer}</p>
      <ProposedFix detail={turn.diagnosis_detail} entityId={entityId} repoRoot={repoRoot} />
      <ValidationBanner validation={turn.validation} />
      <AnalysisTrace turn={turn} />
      <details>
        <summary>Evidence ({turn.evidence.nodes.length} nodes, {turn.evidence.edges.length} edges)</summary>
        <EvidenceList evidence={turn.evidence} />
      </details>
    </div>
  );
}

function buildFileTree(modules) {
  const root = {};
  for (const m of modules) {
    const parts = m.path.split("/");
    let node = root;
    parts.forEach((part, i) => {
      if (i === parts.length - 1) {
        node.__files = node.__files || [];
        node.__files.push({ name: part, module: m });
      } else {
        node[part] = node[part] || {};
        node = node[part];
      }
    });
  }
  return root;
}

function FileTreeNode({ node, selected, onSelect }) {
  const dirs = Object.keys(node).filter((k) => k !== "__files").sort();
  const files = (node.__files || []).slice().sort((a, b) => a.name.localeCompare(b.name));
  return (
    <ul className="file-tree-list">
      {dirs.map((dir) => (
        <li key={dir}>
          <details open>
            <summary>{dir}/</summary>
            <FileTreeNode node={node[dir]} selected={selected} onSelect={onSelect} />
          </details>
        </li>
      ))}
      {files.map(({ name, module }) => (
        <li key={module.path}>
          <button
            type="button"
            className={
              "file-entry" +
              (selected === module.path ? " active" : "") +
              (module.parse_error ? " has-error" : "")
            }
            onClick={() => onSelect(module.path)}
            title={module.parse_error || module.path}
          >
            {module.parse_error && <span className="dot dot-error" />}
            {name}
          </button>
        </li>
      ))}
    </ul>
  );
}

function FileTree({ modules, selected, onSelect }) {
  if (!modules || modules.length === 0) {
    return <p className="meta">No files ingested yet.</p>;
  }
  return <FileTreeNode node={buildFileTree(modules)} selected={selected} onSelect={onSelect} />;
}

function CodeViewer({ path, content, loading, error, issues, onAskAbout }) {
  if (!path) {
    return <p className="meta">Select a file on the left to view its source.</p>;
  }
  if (loading) {
    return <p className="meta">Loading {path}…</p>;
  }
  if (error) {
    return <p className="error">{error}</p>;
  }

  const issuesByLine = {};
  for (const issue of issues || []) {
    if (issue.line) {
      (issuesByLine[issue.line] = issuesByLine[issue.line] || []).push(issue);
    }
  }
  const lines = (content || "").split("\n");

  return (
    <div className="code-viewer">
      <div className="code-viewer-header">
        <code>{path}</code>
        <button type="button" className="secondary small" onClick={() => onAskAbout(path)}>
          Ask about this file
        </button>
      </div>
      <pre className="code-block">
        {lines.map((line, i) => {
          const lineNo = i + 1;
          const lineIssues = issuesByLine[lineNo];
          return (
            <div key={lineNo} className={"code-line" + (lineIssues ? " issue-line" : "")}>
              <span className="line-no">{lineNo}</span>
              <span className="line-text">{line.length ? line : " "}</span>
              {lineIssues && (
                <span className="line-issue-badges">
                  {lineIssues.map((iss, idx) => (
                    <span key={idx} className={`badge badge-${iss.severity}`} title={iss.message}>
                      {iss.type}
                    </span>
                  ))}
                </span>
              )}
            </div>
          );
        })}
      </pre>
    </div>
  );
}

function IssuesBrowser({ issues, loading, filters, onFilterChange, onAskAbout }) {
  return (
    <div className="issues-browser">
      <div className="issues-filters">
        <select
          value={filters.severity}
          onChange={(e) => onFilterChange({ ...filters, severity: e.target.value })}
        >
          <option value="">All severities</option>
          {SEVERITIES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select
          value={filters.detection_method}
          onChange={(e) => onFilterChange({ ...filters, detection_method: e.target.value })}
        >
          <option value="">All sources</option>
          <option value="static">static</option>
          <option value="runtime">runtime</option>
        </select>
      </div>
      {loading && <p className="meta">Loading issues…</p>}
      {!loading && issues.length === 0 && <p className="meta">No issues match this filter.</p>}
      <ul className="issues-list">
        {issues.map((issue) => (
          <li key={issue.id} className="issue-row">
            <span className={`badge badge-${issue.severity}`}>{issue.severity}</span>
            <span className="badge badge-edge">{issue.type}</span>
            <span className="issue-message">{issue.message}</span>
            <span className="meta">
              {(issue.file || issue.module_path) && (
                <>
                  {" "}
                  — <code>
                    {issue.file || issue.module_path}
                    {issue.line ? `:${issue.line}` : ""}
                  </code>
                </>
              )}{" "}
              ({issue.detection_method})
            </span>
            <button type="button" className="secondary small" onClick={() => onAskAbout(issue)}>
              Ask about this
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function App() {
  const [repoRoot, setRepoRoot] = useState("");
  const [stats, setStats] = useState(null);
  const [ingesting, setIngesting] = useState(false);
  const [ingestError, setIngestError] = useState(null);
  const [parseErrors, setParseErrors] = useState([]);

  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [askError, setAskError] = useState(null);
  const [conversation, setConversation] = useState([]);

  const [executing, setExecuting] = useState(false);
  const [executeResult, setExecuteResult] = useState(null);
  const [executeError, setExecuteError] = useState(null);

  const [alerts, setAlerts] = useState(null);

  const [modules, setModules] = useState([]);
  const [selectedFile, setSelectedFile] = useState(null);
  const [fileContent, setFileContent] = useState(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState(null);
  const [fileIssues, setFileIssues] = useState([]);

  const [centerView, setCenterView] = useState("code");
  const [allIssues, setAllIssues] = useState([]);
  const [issuesLoading, setIssuesLoading] = useState(false);
  const [issueFilters, setIssueFilters] = useState({ severity: "", detection_method: "" });

  const refreshStats = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/stats`);
      setStats(await res.json());
    } catch {
      // health/stats failure is surfaced by the ask/ingest flows instead
    }
  };

  const refreshAlerts = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/alerts`);
      const body = await res.json();
      setAlerts(body.alerts);
    } catch {
      // alerts are best-effort; leave the previous list showing
    }
  };

  const refreshModules = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/modules`);
      const body = await res.json();
      setModules(body.modules || []);
    } catch {
      // best-effort
    }
  };

  const refreshIssues = async (filters = issueFilters) => {
    setIssuesLoading(true);
    try {
      const params = new URLSearchParams();
      if (filters.severity) params.set("severity", filters.severity);
      if (filters.detection_method) params.set("detection_method", filters.detection_method);
      const res = await fetch(`${API_BASE}/api/issues?${params.toString()}`);
      const body = await res.json();
      setAllIssues(body.issues || []);
    } catch {
      // best-effort
    } finally {
      setIssuesLoading(false);
    }
  };

  useEffect(() => {
    // refreshAlerts (like refreshIssues) triggers a server-side
    // sync_runtime_issues() as a side effect of /api/alerts -
    // /api/stats' Issue count is a plain read with no sync of its own,
    // so it must wait for that to finish first or it can read a
    // transiently incomplete count (observed live: 4 instead of 5,
    // reading mid-resync). refreshModules has no such dependency.
    (async () => {
      await refreshAlerts();
      await Promise.all([refreshStats(), refreshModules(), refreshIssues()]);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleIngest = async (e) => {
    e.preventDefault();
    setIngesting(true);
    setIngestError(null);
    setParseErrors([]);
    setSelectedFile(null);
    setFileContent(null);
    try {
      const res = await fetch(`${API_BASE}/api/ingest`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_root: repoRoot }),
      });
      if (!res.ok) {
        const body = await res.json();
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const body = await res.json();
      setParseErrors(body.parse_errors || []);
      await Promise.all([refreshStats(), refreshAlerts(), refreshModules(), refreshIssues()]);
    } catch (err) {
      setIngestError(err.message);
    } finally {
      setIngesting(false);
    }
  };

  const handleExecute = async () => {
    setExecuting(true);
    setExecuteError(null);
    setExecuteResult(null);
    try {
      const res = await fetch(`${API_BASE}/api/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_root: repoRoot }),
      });
      if (!res.ok) {
        const body = await res.json();
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const body = await res.json();
      setExecuteResult(body);
      // /api/execute doesn't sync runtime Issue nodes itself (that's lazy,
      // deferred to the next /api/alerts or /api/issues read - see
      // graph/issues.py::sync_runtime_issues) - refreshAlerts must resolve
      // FIRST so /api/stats' Issue count isn't read mid-sync and stale.
      await refreshAlerts();
      await Promise.all([refreshStats(), refreshIssues()]);
      if (selectedFile) {
        await loadFile(selectedFile);
      }
    } catch (err) {
      setExecuteError(err.message);
    } finally {
      setExecuting(false);
    }
  };

  const handleAsk = async (e) => {
    e.preventDefault();
    setAsking(true);
    setAskError(null);
    const askedQuestion = question;
    try {
      const history = conversation.slice(-MAX_HISTORY_TURNS).map((turn) => ({
        question: turn.question,
        answer: turn.answer,
        resolved_target: turn.resolved_target,
      }));
      const res = await fetch(`${API_BASE}/api/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: askedQuestion, history }),
      });
      if (!res.ok) {
        const body = await res.json();
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const turn = await res.json();
      setConversation((prev) => [...prev, turn]);
      setQuestion("");
    } catch (err) {
      setAskError(err.message);
    } finally {
      setAsking(false);
    }
  };

  const loadFile = async (path) => {
    setSelectedFile(path);
    setCenterView("code");
    setFileLoading(true);
    setFileError(null);
    setFileContent(null);
    setFileIssues([]);
    try {
      const [fileRes, issuesRes] = await Promise.all([
        fetch(`${API_BASE}/api/file?path=${encodeURIComponent(path)}`),
        fetch(`${API_BASE}/api/issues?file=${encodeURIComponent(path)}`),
      ]);
      if (!fileRes.ok) {
        const body = await fileRes.json();
        throw new Error(body.detail || `HTTP ${fileRes.status}`);
      }
      const fileBody = await fileRes.json();
      setFileContent(fileBody.content);
      if (issuesRes.ok) {
        const issuesBody = await issuesRes.json();
        setFileIssues(issuesBody.issues || []);
      }
    } catch (err) {
      setFileError(err.message);
    } finally {
      setFileLoading(false);
    }
  };

  const handleIssueFilterChange = (newFilters) => {
    setIssueFilters(newFilters);
    refreshIssues(newFilters);
  };

  const askAbout = (targetText) => {
    setQuestion(`What is wrong in ${targetText}?`);
  };

  const issueCount = stats ? stats.Issue ?? 0 : 0;

  return (
    <div className="app-shell">
      <header className="top-bar">
        <div className="top-bar-title">
          <h1>CodeAtlas</h1>
          <p className="subtitle">Graph-grounded code and runtime intelligence</p>
        </div>

        <form onSubmit={handleIngest} className="row ingest-row">
          <input
            type="text"
            placeholder="Absolute path to a Python repo, e.g. C:\Storage\CodeAtlas\sample_repo"
            value={repoRoot}
            onChange={(e) => setRepoRoot(e.target.value)}
          />
          <button type="submit" disabled={ingesting || !repoRoot}>
            {ingesting ? "Ingesting…" : "Ingest"}
          </button>
          <button type="button" onClick={handleExecute} disabled={executing || !repoRoot}>
            {executing ? "Executing…" : "Execute"}
          </button>
        </form>

        <StatsBar stats={stats} />
      </header>

      <div className="status-row">
        {ingestError && <p className="error">{ingestError}</p>}
        {parseErrors.length > 0 && (
          <p className="meta">
            {parseErrors.length} file(s) could not be parsed:{" "}
            {parseErrors.map((e) => e.path).join(", ")}
          </p>
        )}
        {executeError && <p className="error">{executeError}</p>}
        {executeResult && (
          <p className={executeResult.success ? "meta" : "error"}>
            {executeResult.success
              ? `Ran via ${executeResult.entrypoint_source} (${executeResult.isolation}) — exit code ${executeResult.exit_code}` +
                (executeResult.crash ? ` — crashed: ${executeResult.crash.type}: ${executeResult.crash.message}` : "")
              : `Could not execute: ${executeResult.reason}`}
          </p>
        )}
      </div>

      <div className="main-grid">
        <aside className="left-pane panel">
          <h2>Files</h2>
          <FileTree modules={modules} selected={selectedFile} onSelect={loadFile} />
        </aside>

        <main className="center-pane panel">
          <div className="tabs">
            <button
              type="button"
              className={"tab" + (centerView === "code" ? " active" : "")}
              onClick={() => setCenterView("code")}
            >
              Code
            </button>
            <button
              type="button"
              className={"tab" + (centerView === "issues" ? " active" : "")}
              onClick={() => setCenterView("issues")}
            >
              Issues ({issueCount})
            </button>
          </div>
          {centerView === "code" ? (
            <CodeViewer
              path={selectedFile}
              content={fileContent}
              loading={fileLoading}
              error={fileError}
              issues={fileIssues}
              onAskAbout={askAbout}
            />
          ) : (
            <IssuesBrowser
              issues={allIssues}
              loading={issuesLoading}
              filters={issueFilters}
              onFilterChange={handleIssueFilterChange}
              onAskAbout={(issue) => askAbout(issue.file || issue.module_path || issue.message)}
            />
          )}
        </main>

        <aside className="right-pane">
          <AlertsPanel alerts={alerts} />

          <section className="panel ask-panel">
            <h2>Ask CodeAtlas</h2>
            <form onSubmit={handleAsk} className="row">
              <input
                type="text"
                placeholder={
                  conversation.length > 0
                    ? "Ask a follow-up, e.g. What does it call?"
                    : "e.g. Who calls the add function?"
                }
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
              />
              <button type="submit" disabled={asking || !question}>
                {asking ? "Thinking…" : "Ask"}
              </button>
              {conversation.length > 0 && (
                <button type="button" className="secondary" onClick={() => setConversation([])}>
                  Clear
                </button>
              )}
            </form>
            {askError && <p className="error">{askError}</p>}

            {conversation.length > 0 && (
              <div className="conversation">
                {conversation.map((turn, i) => (
                  <ConversationTurn key={i} turn={turn} repoRoot={repoRoot} />
                ))}
              </div>
            )}
          </section>
        </aside>
      </div>
    </div>
  );
}
