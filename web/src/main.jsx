import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const api = async (path, options) => {
  const res = await fetch(path, options);
  if (!res.ok) {
    let text = await res.text();
    try {
      text = JSON.parse(text).error || text;
    } catch {}
    throw new Error(text || `HTTP ${res.status}`);
  }
  return res.json();
};

const pages = {
  dashboard: ["대시보드", "수집 상태와 다음 작업을 한눈에 봅니다."],
  rooms: ["대화방", "로컬 DB 스냅샷에서 방을 고르고 요약 작업만 설정합니다."],
  styles: ["요약 스타일", "작업에 사용할 기본 요약 형식을 설정합니다."],
  run: ["실행", "수동 실행 명령과 최신화 상태를 확인합니다."],
  automation: ["자동화", "저장된 요약 작업 목록을 확인하고 삭제합니다."],
  settings: ["설정", "출력 경로와 로컬 동작 기준을 확인합니다."]
};

const styleCards = [
  ["structured", "구조화 요약", "핵심 이슈, 결정사항, 할 일, 주의할 점을 나눠 정리합니다."],
  ["daily_brief", "데일리 브리프", "하루 단위 흐름을 짧은 보고서처럼 정리합니다."],
  ["action_items", "할 일 중심", "담당자, 액션, 마감 힌트를 우선해서 정리합니다."]
];

const defaultStyle = {
  style: "structured",
  outputFormat: "markdown",
  language: "ko",
  prompt:
    "대화 내용을 핵심 이슈, 결정사항, 할 일, 주의할 점으로 나눠 간결하게 요약해줘. 불확실한 내용은 추측하지 말고 원문 기준으로만 정리해줘."
};

function useTheme() {
  const [theme, setTheme] = useState(localStorage.getItem("kwin-theme") || "auto");
  useEffect(() => {
    localStorage.setItem("kwin-theme", theme);
    document.documentElement.dataset.theme = theme;
  }, [theme]);
  return [theme, setTheme];
}

function App() {
  const [page, setPage] = useState("rooms");
  const [theme, setTheme] = useTheme();
  const [rooms, setRooms] = useState([]);
  const [dbMeta, setDbMeta] = useState({});
  const [query, setQuery] = useState("");
  const [selectedRoom, setSelectedRoom] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [jobsFile, setJobsFile] = useState("");
  const [summaryStyle, setSummaryStyle] = useState(defaultStyle);
  const [styleFile, setStyleFile] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [busyKind, setBusyKind] = useState("");

  const pageTitle = pages[page][0];
  const pageSubtitle = pages[page][1];

  const loadRooms = async (q = query) => {
    const data = await api(`/api/rooms?limit=300&q=${encodeURIComponent(q)}`);
    setRooms(data.rooms || []);
    setDbMeta(data);
    return data;
  };

  const loadJobs = async () => {
    const data = await api("/api/jobs");
    setJobs(data.jobs || []);
    setJobsFile(data.jobsFile || "");
    return data;
  };

  const loadStyle = async () => {
    const data = await api("/api/style");
    setSummaryStyle({ ...defaultStyle, ...(data.style || {}) });
    setStyleFile(data.styleFile || "");
    return data;
  };

  useEffect(() => {
    loadRooms().catch((e) => setStatus(e.message));
    loadStyle().catch(() => {});
    loadJobs().catch(() => {});
  }, []);

  useEffect(() => {
    if (page === "automation") loadJobs().catch((e) => setStatus(e.message));
    if (page === "styles") loadStyle().catch((e) => setStatus(e.message));
  }, [page]);

  useEffect(() => {
    const t = setTimeout(() => {
      if (page === "rooms") loadRooms(query).catch((e) => setStatus(e.message));
    }, 180);
    return () => clearTimeout(t);
  }, [query]);

  const sync = async () => {
    setBusy(true);
    setBusyKind("sync");
    setStatus("대화 최신화 중입니다. KakaoTalk에서 열린 방 기준으로 v2sync를 실행합니다...");
    try {
      const data = await api("/api/sync", { method: "POST" });
      setRooms(data.rooms || []);
      setDbMeta(data);
      setStatus(`대화 최신화 완료 · ${data.dbUpdatedKst || data.result?.ranAtKst || ""}`);
    } catch (e) {
      setStatus(e.message);
    } finally {
      setBusy(false);
      setBusyKind("");
    }
  };

  const recoverSync = async () => {
    setBusy(true);
    setBusyKind("recover-sync");
    setStatus("DB 키 찾기부터 시작합니다. KakaoTalk에서 필요한 대화방을 몇 개 열어둔 상태가 가장 좋습니다...");
    try {
      const data = await api("/api/recover-sync", { method: "POST" });
      setRooms(data.rooms || []);
      setDbMeta(data);
      setStatus(`키 찾기 + 대화 최신화 완료 · ${data.dbUpdatedKst || data.result?.ranAtKst || ""}`);
    } catch (e) {
      setStatus(e.message);
    } finally {
      setBusy(false);
      setBusyKind("");
    }
  };

  const saveJob = async (form) => {
    if (!selectedRoom) return;
    const data = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chatId: selectedRoom.chatId,
        roomTitle: selectedRoom.title,
        ...form
      })
    });
    setModalOpen(false);
    setStatus(`작업 저장 완료 · ${data.jobsFile}`);
    await loadJobs();
  };

  const deleteJob = async (job) => {
    if (!window.confirm("이 자동화 작업을 삭제할까요?")) return;
    await api(`/api/jobs?id=${encodeURIComponent(job.id)}`, { method: "DELETE" });
    await loadJobs();
  };

  const saveStyle = async (nextStyle) => {
    const data = await api("/api/style", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(nextStyle)
    });
    setSummaryStyle(data.style || nextStyle);
    setStyleFile(data.styleFile || "");
    setStatus(`요약 스타일 저장 완료 · ${data.styleFile}`);
  };

  return (
    <div className="app">
      <Sidebar page={page} setPage={setPage} />
      <main className="content">
        <header className="pageHead">
          <div>
            <h1>{pageTitle}</h1>
            <div className="subtitle">{pageSubtitle}</div>
          </div>
          <div className="actions">
            <ThemeSwitch theme={theme} setTheme={setTheme} />
            <button className="ghost" onClick={recoverSync} disabled={busy}>
              {busy && busyKind === "recover-sync" ? "키 찾는 중..." : "키 찾기 + 최신화"}
            </button>
            <button className="primary" onClick={sync} disabled={busy}>
              {busy && busyKind === "sync" ? "최신화 중..." : "대화 최신화"}
            </button>
          </div>
        </header>
        <section className="workspace">
          {page === "dashboard" && <Dashboard dbMeta={dbMeta} jobs={jobs} status={status} />}
          {page === "rooms" && (
            <Rooms
              rooms={rooms}
              query={query}
              setQuery={setQuery}
              dbMeta={dbMeta}
              selectedRoom={selectedRoom}
              setSelectedRoom={setSelectedRoom}
              openJob={() => setModalOpen(true)}
              status={status}
            />
          )}
          {page === "styles" && <Styles value={summaryStyle} styleFile={styleFile} onSave={saveStyle} />}
          {page === "run" && <Run status={status} sync={sync} recoverSync={recoverSync} busy={busy} busyKind={busyKind} />}
          {page === "automation" && <Automation jobs={jobs} jobsFile={jobsFile} onDelete={deleteJob} />}
          {page === "settings" && <Settings dbMeta={dbMeta} jobsFile={jobsFile} styleFile={styleFile} />}
        </section>
      </main>
      {modalOpen && (
        <JobModal
          room={selectedRoom}
          defaultStyle={summaryStyle.style}
          onClose={() => setModalOpen(false)}
          onSave={saveJob}
        />
      )}
    </div>
  );
}

function Sidebar({ page, setPage }) {
  const items = [
    ["dashboard", "▦", "대시보드"],
    ["rooms", "▱", "대화방"],
    ["styles", "▤", "요약 스타일"],
    ["run", "▷", "실행"],
    ["automation", "◴", "자동화"],
    ["settings", "⚙", "설정"]
  ];
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="logo">K</div>
        <div>Kakao Daily</div>
      </div>
      <nav className="nav">
        {items.map(([key, icon, label]) => (
          <button key={key} className={`navBtn ${page === key ? "active" : ""}`} onClick={() => setPage(key)}>
            <span className="navIcon">{icon}</span>
            <span className="navLabel">{label}</span>
          </button>
        ))}
      </nav>
      <div className="sideBottom">
        로컬 전용
        <br />
        <span>Windows v0.2.0</span>
      </div>
    </aside>
  );
}

function ThemeSwitch({ theme, setTheme }) {
  return (
    <div className="segmented">
      {["auto", "light", "dark"].map((x) => (
        <button key={x} className={theme === x ? "active" : ""} onClick={() => setTheme(x)}>
          {x === "auto" ? "자동" : x === "light" ? "라이트" : "다크"}
        </button>
      ))}
    </div>
  );
}

function Dashboard({ dbMeta, jobs, status }) {
  return (
    <div className="grid3">
      <InfoCard title="로컬 전용" body="127.0.0.1에서만 동작하며 데이터를 외부로 업로드하지 않습니다." />
      <InfoCard title="현재 DB" body={dbMeta.db || "아직 DB를 찾지 못했습니다."} mono />
      <InfoCard title="자동화 작업" body={`${jobs.length}개 저장됨`} />
      {status && <section className="card cardPad wide"><div className="hint">{status}</div></section>}
    </div>
  );
}

function Rooms({ rooms, query, setQuery, dbMeta, selectedRoom, setSelectedRoom, openJob, status }) {
  return (
    <div className="grid2">
      <section className="card">
        <div className="cardHead">
          <div className="cardTitle">대화방 목록</div>
          <div className="hint">{rooms.length}개 표시 · DB 갱신 {dbMeta.dbUpdatedKst || "-"}</div>
          <input className="searchBox" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="방 이름 검색" />
        </div>
        <div className="scroll">
          {rooms.length ? rooms.map((room, index) => (
            <button
              key={room.chatId}
              className={`room ${selectedRoom?.chatId === room.chatId ? "active" : ""}`}
              onClick={() => setSelectedRoom(room)}
            >
              <div className="roomName">{index + 1}. {room.title}</div>
              <div className="meta">
                {room.roomType && <span className="pill">{room.roomType}</span>}
                {room.activeMembersCount && <span className="pill">{room.activeMembersCount}명</span>}
                <span className="pill">{room.messageCount}개</span>
                <span>{room.lastSentKst}</span>
              </div>
              <div className="preview">{room.lastMessage}</div>
            </button>
          )) : <div className="empty">대화방이 없습니다. 대화 최신화를 먼저 실행하세요.</div>}
        </div>
      </section>
      <section className="card selected">
        <div>
          <div className="cardTitle">선택된 대화방</div>
          <div className="hint">웹에는 원문 대화와 멤버 목록을 노출하지 않습니다.</div>
        </div>
        {selectedRoom ? (
          <>
            <div>
              <div className="selectedTitle">{selectedRoom.title}</div>
              <div className="selectedSub">이 방을 기준으로 요약 작업을 만들 수 있습니다.</div>
            </div>
            <div className="infoGrid">
              <Metric label="마지막 수집" value={selectedRoom.lastSentKst || "-"} />
              <Metric label="메시지 수" value={selectedRoom.messageCount ?? "-"} />
              <Metric label="방 유형" value={selectedRoom.roomType || "-"} />
              <Metric label="멤버 수" value={selectedRoom.activeMembersCount || "-"} />
            </div>
            <button className="primary" onClick={openJob}>+ 작업 추가</button>
          </>
        ) : (
          <div className="empty">왼쪽 목록에서 대화방을 선택하세요.</div>
        )}
        {status && <div className="hint">{status}</div>}
      </section>
    </div>
  );
}

function Styles({ value, styleFile, onSave }) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const update = (patch) => setDraft((prev) => ({ ...prev, ...patch }));
  return (
    <div className="grid2">
      <section className="card cardPad">
        <div className="cardTitle">기본 요약 스타일</div>
        <div className="hint">작업추가 팝업에서 기본으로 선택될 스타일입니다.</div>
        <div className="styleGrid">
          {styleCards.map(([key, title, desc]) => (
            <button key={key} className={`stylePick ${draft.style === key ? "active" : ""}`} onClick={() => update({ style: key })}>
              <div className="cardTitle">{title}</div>
              <div className="hint">{desc}</div>
            </button>
          ))}
        </div>
        <div className="fieldGrid">
          <label className="field">출력 형식
            <select value={draft.outputFormat} onChange={(e) => update({ outputFormat: e.target.value })}>
              <option value="markdown">Markdown</option>
              <option value="json">JSON</option>
              <option value="txt">TXT</option>
            </select>
          </label>
          <label className="field">언어
            <select value={draft.language} onChange={(e) => update({ language: e.target.value })}>
              <option value="ko">한국어</option>
              <option value="en">English</option>
            </select>
          </label>
        </div>
        <label className="field">요약 프롬프트
          <textarea value={draft.prompt} onChange={(e) => update({ prompt: e.target.value })} />
        </label>
        <button className="primary" onClick={() => onSave(draft)}>저장</button>
      </section>
      <InfoCard title="저장 파일" body={styleFile || "저장 전"} mono />
    </div>
  );
}

function Automation({ jobs, jobsFile, onDelete }) {
  return (
    <section className="card cardPad">
      <div className="cardTitle">자동화 작업</div>
      <div className="hint mono">저장 파일: {jobsFile || "-"}</div>
      <div className="stack">
        {jobs.length ? jobs.map((job, i) => (
          <div className="infoBox jobTop" key={job.id}>
            <div>
              <div className="infoLabel">#{i + 1} · {job.scheduleTime} · {job.period}</div>
              <div className="infoValue">{job.roomTitle || "대화방"}</div>
              <div className="hint">{job.style} · {job.destination} · {job.createdAtKst}</div>
            </div>
            <button className="danger" onClick={() => onDelete(job)}>삭제</button>
          </div>
        )) : <div className="empty">저장된 작업이 없습니다.</div>}
      </div>
    </section>
  );
}

function Run({ status, sync, recoverSync, busy, busyKind }) {
  return (
    <section className="card cardPad">
      <div className="cardTitle">수동 실행</div>
      <div className="hint">처음 실행하거나 키가 없을 때는 키 찾기부터, 이미 키가 있으면 대화 최신화만 실행하면 됩니다.</div>
      <div className="runActions">
        <button className="ghost" onClick={recoverSync} disabled={busy}>
          {busy && busyKind === "recover-sync" ? "키 찾는 중..." : "키 찾기 + 최신화"}
        </button>
        <button className="primary" onClick={sync} disabled={busy}>
          {busy && busyKind === "sync" ? "최신화 중..." : "대화 최신화"}
        </button>
      </div>
      <div className="hint">키 찾기 + 최신화가 서버에서 실행하는 명령입니다.</div>
      <p className="mono">python -m kwin v2recover</p>
      <p className="mono">python -m kwin v2sync</p>
      <div className="hint">대화 최신화 버튼은 아래 명령만 실행합니다.</div>
      <p className="mono">python -m kwin v2sync</p>
      <div className="hint">KakaoTalk에서 필요한 방을 열어둔 뒤 실행하면 더 많은 최신 DB 키가 잡힐 수 있습니다.</div>
      {status && <div className="infoBox">{status}</div>}
    </section>
  );
}

function Settings({ dbMeta, jobsFile, styleFile }) {
  return (
    <section className="card cardPad">
      <div className="cardTitle">설정</div>
      <div className="stack">
        <Metric label="현재 DB" value={dbMeta.db || "-"} mono />
        <Metric label="자동화 작업 파일" value={jobsFile || "-"} mono />
        <Metric label="요약 스타일 파일" value={styleFile || "-"} mono />
      </div>
    </section>
  );
}

function JobModal({ room, defaultStyle, onClose, onSave }) {
  const [form, setForm] = useState({
    scheduleTime: "오전 09:00",
    repeatMode: "daily",
    weekdays: [],
    period: "since_last_run",
    style: defaultStyle || "structured",
    destination: "local_file"
  });
  const update = (patch) => setForm((prev) => ({ ...prev, ...patch }));
  const toggleDay = (day) => {
    update({ weekdays: form.weekdays.includes(day) ? form.weekdays.filter((x) => x !== day) : [...form.weekdays, day] });
  };
  return (
    <div className="modalLayer">
      <div className="modal">
        <div className="modalMain">
          <div className="modalTop">
            <div>
              <div className="modalTitle">요약 작업</div>
              <div className="hint">{room?.title}</div>
            </div>
            <button className="closeBtn" onClick={onClose}>×</button>
          </div>
          <label className="field">실행 시각
            <input value={form.scheduleTime} onChange={(e) => update({ scheduleTime: e.target.value })} />
          </label>
          <div className="field">
            <div className="label">반복 요일</div>
            <div className="chipRow">
              {["daily", "weekday", "weekend"].map((x) => <button key={x} className={`chip ${form.repeatMode === x ? "active" : ""}`} onClick={() => update({ repeatMode: x })}>{x === "daily" ? "매일" : x === "weekday" ? "평일" : "주말"}</button>)}
            </div>
            <div className="chipRow">
              {["일", "월", "화", "수", "목", "금", "토"].map((d) => <button key={d} className={`chip ${form.weekdays.includes(d) ? "active" : ""}`} onClick={() => toggleDay(d)}>{d}</button>)}
            </div>
          </div>
          <div className="field">
            <div className="label">요약 기간</div>
            <div className="chipRow">
              {[["since_last_run", "직전 실행 이후"], ["yesterday", "어제"], ["last_7_days", "최근 7일"]].map(([v, t]) => <button key={v} className={`chip ${form.period === v ? "active" : ""}`} onClick={() => update({ period: v })}>{t}</button>)}
            </div>
          </div>
          <div className="fieldGrid">
            <label className="field">요약 스타일
              <select value={form.style} onChange={(e) => update({ style: e.target.value })}>
                <option value="structured">구조화 요약</option>
                <option value="daily_brief">데일리 브리프</option>
                <option value="action_items">할 일 중심</option>
              </select>
            </label>
            <label className="field">저장 위치
              <select value={form.destination} onChange={(e) => update({ destination: e.target.value })}>
                <option value="local_file">로컬 파일</option>
                <option value="markdown">마크다운 파일</option>
                <option value="json">JSON 파일</option>
              </select>
            </label>
          </div>
        </div>
        <div className="modalFoot">
          <button className="ghost" onClick={onClose}>취소</button>
          <button className="primary" onClick={() => onSave(form)}>▣ 저장</button>
        </div>
      </div>
    </div>
  );
}

function InfoCard({ title, body, mono }) {
  return <section className="card cardPad"><div className="cardTitle">{title}</div><div className={mono ? "hint mono" : "hint"}>{body}</div></section>;
}

function Metric({ label, value, mono }) {
  return <div className="infoBox"><div className="infoLabel">{label}</div><div className={mono ? "mono" : "infoValue"}>{value}</div></div>;
}

createRoot(document.getElementById("root")).render(<App />);
