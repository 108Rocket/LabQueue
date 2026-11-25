const taskPage = byId("tasks-page");
const detailId = taskPage.dataset.taskId;
const taskPath = detailId ? `/api/tasks/${encodeURIComponent(detailId)}` : "/api/tasks";
const versionKey = `labqueue.task-version.${taskPage.dataset.snapshotScope}`;
const taskFeedback = byId("tasks-feedback");
const taskResults = byId("task-results");
const waitingReasons = {
  gpu_busy: "两张模拟 GPU 均被占用。",
  gpu_visibility_unknown: "模拟 GPU 占用状态待确认。",
};
const decimalVersion = /^(?:0|[1-9][0-9]*)$/;
const taskUuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const utcTime = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/;
let versionFloor = "0";
let displayedVersion = null;
let lastRequest = 0;
let reportedRequest = 0;
let sessionValid = true;

class VersionCoordinationError extends Error {}

function isDecimal(value) {
  return typeof value === "string" && value === value.trim() && decimalVersion.test(value);
}

function earlierVersion(version, other) {
  return version.length === other.length ? version < other : version.length < other.length;
}

function isTaskView(task) {
  if (!task || typeof task !== "object" || Array.isArray(task)
      || typeof task.task_id !== "string" || !taskUuid.test(task.task_id)
      || typeof task.name !== "string" || task.name.length === 0
      || !isDecimal(task.enqueue_seq) || task.enqueue_seq === "0"
      || typeof task.queued_at !== "string" || !utcTime.test(task.queued_at)
      || !Number.isFinite(Date.parse(task.queued_at))
      || typeof task.status !== "string" || !task.status.trim()
      || task.can_cancel !== false) {
    return false;
  }
  if (task.status === "queued") {
    return Number.isSafeInteger(task.queue_position) && task.queue_position > 0
      && typeof task.waiting_reason === "string" && Object.hasOwn(waitingReasons, task.waiting_reason);
  }
  return task.queue_position === null && task.waiting_reason === null;
}

function readTaskRows(snapshot) {
  if (!isDecimal(snapshot?.snapshot_version)) throw new Error("Invalid snapshot version");
  if (detailId) {
    if (isTaskView(snapshot.task) && snapshot.task.task_id === detailId) return [snapshot.task];
  } else if (Array.isArray(snapshot.tasks) && snapshot.tasks.every(isTaskView)) {
    return snapshot.tasks;
  }
  throw new Error("Incomplete task snapshot");
}

function markLastConfirmed(message) {
  taskFeedback.textContent = displayedVersion === null ? message : `上次确认。${message}`;
}

function accessVersionStorage(operation) {
  try {
    return operation();
  } catch {
    throw new VersionCoordinationError("请允许访问浏览器本地存储，以核对任务版本。");
  }
}

function withVersionFloor(useFloor) {
  if (!navigator.locks?.request) {
    throw new VersionCoordinationError("当前浏览器需要支持 Web Locks 才能核对任务版本。");
  }
  // Web Locks 将不同页面的读取、比较和持久写入串成同一临界区。
  return navigator.locks.request(versionKey, () => {
    if (!sessionValid) return;
    const stored = accessVersionStorage(() => localStorage.getItem(versionKey));
    if (stored !== null && !isDecimal(stored)) {
      throw new VersionCoordinationError("无法核对本地任务版本，请重新登录。");
    }
    if (stored !== null && earlierVersion(versionFloor, stored)) versionFloor = stored;
    if (displayedVersion !== null && earlierVersion(displayedVersion, versionFloor)) {
      markLastConfirmed("已有更新，请刷新此页。");
    }
    useFloor?.(stored);
  });
}

function taskCard(task) {
  const card = document.createElement("article");
  const heading = document.createElement("h3");
  const name = /[^\p{White_Space}\p{Default_Ignorable_Code_Point}\p{Cc}]/u.test(task.name)
    ? task.name : `任务 ${task.task_id}`;
  if (detailId) {
    heading.textContent = name;
  } else {
    const link = document.createElement("a");
    link.href = `/tasks/${encodeURIComponent(task.task_id)}`;
    link.textContent = name;
    heading.append(link);
  }
  const queued = task.status === "queued";
  const status = document.createElement("p");
  status.textContent = queued ? `排队中 · 全局队位 ${task.queue_position}` : "已离开队列";
  card.append(heading, status);
  if (queued) {
    const reason = document.createElement("p");
    reason.textContent = waitingReasons[task.waiting_reason];
    card.append(reason);
  }
  const registration = document.createElement("p");
  const confirmedAt = task.queued_at.replace("T", " ").replace(/(?:\.\d+)?Z$/, " UTC");
  const confirmation = `确认时间 ${confirmedAt}`;
  registration.textContent = detailId ? `入队序号 ${task.enqueue_seq} · ${confirmation}` : confirmation;
  card.append(registration);
  return card;
}

async function readTasks() {
  try {
    const snapshot = await request(taskPath, { headers: sessionHeaders });
    return { version: snapshot.snapshot_version, tasks: readTaskRows(snapshot) };
  } catch (error) {
    if (detailId && error.status === 404 && isDecimal(error.snapshotVersion)) {
      return { version: error.snapshotVersion, tasks: null };
    }
    throw error;
  }
}

function showReadFailure(error) {
  markLastConfirmed(error instanceof VersionCoordinationError
    ? error.message : "暂时无法读取任务，请刷新此页。");
}

async function refreshTasks() {
  if (!sessionValid) return;
  const requestNumber = ++lastRequest;
  if (displayedVersion === null) taskFeedback.textContent = "正在读取任务。";
  try {
    const { version, tasks } = await readTasks();
    await withVersionFloor((stored) => {
      if (earlierVersion(version, versionFloor)) {
        if (displayedVersion === null && requestNumber === lastRequest) {
          markLastConfirmed("尚未取得当前任务，请刷新此页。");
        }
        return;
      }
      if (version === displayedVersion && requestNumber < reportedRequest) return;
      if (stored !== version) accessVersionStorage(() => localStorage.setItem(versionKey, version));
      // 同版本只更新核对反馈，保留任务链接和键盘焦点。
      if (version !== displayedVersion) {
        const cards = document.createDocumentFragment();
        for (const task of tasks ?? []) cards.append(taskCard(task));
        taskResults.replaceChildren(cards);
      }
      versionFloor = version;
      displayedVersion = version;
      reportedRequest = Math.max(requestNumber, reportedRequest);
      if (tasks === null) taskFeedback.textContent = "未找到该任务。";
      else taskFeedback.textContent = tasks.length ? "已更新任务。" : "暂无本人任务。";
    });
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      sessionValid = false;
      displayedVersion = null;
      taskResults.replaceChildren();
      taskFeedback.textContent = "会话已失效，请重新登录。";
    } else if (requestNumber === lastRequest && sessionValid) {
      reportedRequest = requestNumber;
      showReadFailure(error);
    }
  }
}

async function recheckVersionFloor() {
  try {
    await withVersionFloor();
  } catch (error) {
    if (sessionValid) showReadFailure(error);
  }
}

byId("refresh-tasks").addEventListener("click", refreshTasks);
window.addEventListener("storage", (event) => {
  if (event.key === versionKey || event.key === null) return recheckVersionFloor();
});
window.addEventListener("pagehide", (event) => {
  if (event.persisted && displayedVersion !== null) markLastConfirmed("返回时重新核对任务。");
});
window.addEventListener("pageshow", async (event) => {
  if (!event.persisted) return;
  if (displayedVersion !== null) markLastConfirmed("正在重新核对任务。");
  await recheckVersionFloor();
  await refreshTasks();
});
refreshTasks();
