const byId = (id) => document.getElementById(id);
const sessionChangeKey = "labqueue.session-change";

function notifySessionChange() {
  try {
    // 存储事件只唤醒其他标签页核对 Cookie，不保存任何身份凭证。
    localStorage.setItem(sessionChangeKey, crypto.randomUUID());
  } catch {
    // 任务页也会在恢复可见或处理响应时复核会话。
  }
}

async function request(path, { method = "GET", body, headers = {} } = {}) {
  const response = await fetch(path, {
    method,
    signal: AbortSignal.timeout(12000),
    headers: body ? { ...headers, "Content-Type": "application/json" } : headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) {
    throw Object.assign(new Error(data?.detail), {
      status: response.status, fields: data?.fields, snapshotVersion: data?.snapshot_version,
    });
  }
  return data;
}

const loginForm = byId("login");
if (loginForm) {
  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = loginForm.querySelector("button");
    button.disabled = true;
    byId("login-feedback").textContent = "";
    try {
      await request("/api/session", {
        method: "POST",
        headers: { "X-LabQueue-Request": "1" },
        body: { username: byId("username").value, password: byId("password").value },
      });
      notifySessionChange();
      window.location.reload();
    } catch (error) {
      byId("login-feedback").textContent = error.status ? error.message : "暂时无法登录，请重试。";
      button.disabled = false;
    }
  });
}

const session = byId("session");
const sessionHeaders = session ? { "X-CSRF-Token": session.dataset.csrf } : {};
if (session && byId("submission")) {
  const form = byId("submission");
  const fields = byId("submission-fields");
  const feedback = byId("submission-feedback");
  const lookupFeedback = byId("lookup-feedback");
  const lookupKey = byId("lookup-key");
  const mode = byId("memory-mode");
  const retry = byId("retry");
  const newSubmission = byId("new-submission");
  const submitButton = byId("submit-button");
  const storageKey = `labqueue.pending-submission.${session.dataset.owner}`;
  const errorFields = ["name", "command", "memory_gib"];
  // 只保留未确认的键。原命令留在当前页面内存中，刷新后可只读核对。
  const unresolvedKeys = new Set();
  try {
    const keys = JSON.parse(sessionStorage.getItem(storageKey) || "[]");
    if (Array.isArray(keys)) keys.forEach((key) => unresolvedKeys.add(key));
  } catch {
    // 禁用浏览器存储时，本页仍可使用原键核对和重试。
  }
  let pending = null;
  let sending = false;
  let lookupVersion = 0;

  function updatePendingKeys() {
    const keys = [...unresolvedKeys];
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(keys));
    } catch {
      // 保留内存中的键和页面上的可复制值。
    }
    byId("pending-keys").replaceChildren(...keys.map((key) => {
      const option = document.createElement("option");
      option.value = key;
      return option;
    }));
  }

  function clearLookupFeedback() {
    lookupVersion += 1;
    lookupFeedback.textContent = "";
  }

  function confirmKey(key) {
    if (unresolvedKeys.delete(key)) updatePendingKeys();
  }

  function updateControls() {
    fields.disabled = sending || pending !== null;
    submitButton.hidden = pending !== null;
    submitButton.disabled = sending;
    retry.hidden = pending === null;
    retry.disabled = sending;
    newSubmission.hidden = pending === null;
    newSubmission.disabled = sending;
  }

  function updateMemoryField() {
    const estimating = mode.value === "estimate";
    byId("estimate").hidden = !estimating;
    byId("memory_gib").required = estimating;
    if (!estimating) {
      byId("memory_gib-error").textContent = "";
      byId("memory_gib").removeAttribute("aria-invalid");
    }
  }

  function showFieldErrors(errors = {}) {
    for (const name of errorFields) {
      byId(`${name}-error`).textContent = errors[name] || "";
      byId(name).setAttribute("aria-invalid", errors[name] ? "true" : "false");
    }
  }

  function showReceipt(receipt, target) {
    const memory = receipt.memory_gib === null ? "未知" : `${receipt.memory_gib} GiB`;
    target.textContent = `已确认入队。任务编号 ${receipt.task_id}，显存估计 ${memory}。`;
  }

  async function send(attempt) {
    if (sending) return;
    sending = true;
    pending = attempt;
    updateControls();
    feedback.textContent = "正在提交。";
    showFieldErrors();
    lookupKey.value = attempt.submission_key;
    clearLookupFeedback();
    const alreadyUnresolved = unresolvedKeys.has(attempt.submission_key);
    unresolvedKeys.add(attempt.submission_key);
    updatePendingKeys();
    try {
      showReceipt(await request("/api/submissions", {
        method: "POST", body: attempt, headers: sessionHeaders,
      }), feedback);
      if (lookupKey.value.trim().toLowerCase() === attempt.submission_key) {
        clearLookupFeedback();
      }
      confirmKey(attempt.submission_key);
      pending = null;
      form.reset();
      updateMemoryField();
    } catch (error) {
      // 本次提交的只读核对可能先于写请求的响应完成。
      if (pending !== attempt) return;
      if ([401, 403, 409, 422].includes(error.status)) {
        feedback.textContent = error.message;
        showFieldErrors(error.fields);
        pending = null;
        if (!alreadyUnresolved || error.status === 409) {
          confirmKey(attempt.submission_key);
        }
      } else {
        pending = attempt;
        feedback.textContent = "暂未确认提交结果。请先核对回执，也可以手动重试原提交。";
      }
    } finally {
      sending = false;
      updateControls();
    }
  }

  if (unresolvedKeys.size) {
    lookupKey.value = [...unresolvedKeys].at(-1);
    feedback.textContent = "有提交结果待核对，可在下方选择原提交键。";
    updatePendingKeys();
  }
  mode.addEventListener("change", updateMemoryField);
  updateMemoryField();

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (sending || pending) return;
    if (unresolvedKeys.size && !window.confirm("待核对的提交仍可能已入队。确定使用新提交键吗？")) return;
    return send({
      submission_key: crypto.randomUUID(),
      name: byId("name").value,
      command: byId("command").value,
      memory_gib: mode.value === "estimate" ? byId("memory_gib").value : null,
    });
  });
  retry.addEventListener("click", () => {
    if (pending) return send(pending);
  });
  newSubmission.addEventListener("click", () => {
    pending = null;
    feedback.textContent = "前一次提交仍可能已入队，原提交键可用于核对。";
    updateControls();
  });

  lookupKey.addEventListener("input", clearLookupFeedback);
  byId("lookup").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button");
    const key = lookupKey.value.trim().toLowerCase();
    const version = ++lookupVersion;
    button.disabled = true;
    lookupFeedback.textContent = "正在核对。";
    try {
      const receipt = await request(`/api/submissions/${encodeURIComponent(key)}`, {
        headers: sessionHeaders,
      });
      confirmKey(key);
      if (version === lookupVersion) showReceipt(receipt, lookupFeedback);
      if (pending?.submission_key === key) {
        pending = null;
        showReceipt(receipt, feedback);
        form.reset();
        updateMemoryField();
        updateControls();
      }
    } catch (error) {
      if (version === lookupVersion) {
        lookupFeedback.textContent = error.status === 404
          ? "当前未核对到回执，仍无法确认提交结果。请保留原提交键。"
          : error.fields?.submission_key || (error.status ? error.message : "暂时无法核对。");
      }
    } finally {
      button.disabled = false;
    }
  });
}

if (session) {
  byId("logout").addEventListener("click", async () => {
    try {
      await request("/api/session", { method: "DELETE", headers: sessionHeaders });
      notifySessionChange();
      window.location.reload();
    } catch (error) {
      if (error.status === 401 || error.status === 403) {
        window.location.reload();
      } else {
        byId("session-feedback").textContent = "暂时无法退出，请重试。";
      }
    }
  });
}
