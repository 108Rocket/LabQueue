const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { setImmediate: settle } = require("node:timers/promises");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const [submissionScript, tasksScript] = ["submission.js", "tasks.js"].map((file) =>
  readFileSync(join(__dirname, "../src/labqueue/static", file), "utf8")
);
const scripts = `${submissionScript}\n${tasksScript}`;
const taskId = "83f45969-1db4-4ac7-91c7-c16f361baf16";
const versionKey = "labqueue.task-version.test-session";
const task = {
  task_id: taskId, name: "合成任务", enqueue_seq: "2", queued_at: "2026-11-25T01:02:03.000001Z",
  status: "queued", queue_position: 2, waiting_reason: "gpu_busy", can_cancel: false,
};

function snapshot(detail, version, changes = {}) {
  const row = { ...task, ...changes };
  return detail ? { snapshot_version: version, task: row }
    : { snapshot_version: version, tasks: [row] };
}

function makeSharedVersionStore() {
  const values = new Map();
  const pending = new Map();
  const heldLocks = new Set();
  const locks = {
    request(key, callback) {
      const work = (pending.get(key) || Promise.resolve()).then(() => {
        heldLocks.add(key);
        try {
          return callback();
        } finally {
          heldLocks.delete(key);
        }
      });
      pending.set(key, work.catch(() => {}));
      return work;
    },
  };
  const tabs = new Set();
  return {
    values, locks, heldLocks, available: true, cookie: "labqueue_view_scope=test-session",
    tabs,
    signal(source, key) {
      for (const tab of tabs) if (tab !== source) tab.fire("storage", { key });
    },
  };
}

function makePage(detail, shared = makeSharedVersionStore(), scope = "test-session") {
  const makeElement = (tag) => {
    const handlers = new Map();
    return {
      tag, children: [], textContent: "", dataset: {},
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      contains(node) { return this === node || this.children.some((child) => child.contains(node)); },
      setAttribute() {},
      addEventListener(type, handler) { handlers.set(type, handler); },
      fire(type, event) { return handlers.get(type)(event); },
    };
  };
  const elements = new Map(["session", "logout", "session-feedback", "tasks-page", "refresh-tasks",
    "tasks-feedback", "task-results"].map((id) => [id, makeElement(id)]));
  elements.get("session").dataset.csrf = "test-csrf-token";
  elements.get("tasks-page").dataset.snapshotScope = scope;
  if (detail) elements.get("tasks-page").dataset.taskId = taskId;
  const pageWindow = makeElement("window");
  pageWindow.confirm = () => true;
  pageWindow.location = { reload() {} };
  let checkInterval;
  pageWindow.setInterval = (callback) => { checkInterval = callback; };
  shared.tabs.add(pageWindow);
  const fetches = [];
  const document = makeElement("document");
  Object.assign(document, {
    getElementById: (id) => elements.get(id) || null,
    createElement: makeElement,
    createDocumentFragment: () => makeElement("fragment"),
    visibilityState: "visible",
  });
  Object.defineProperty(document, "cookie", { get: () => shared.cookie });
  runInNewContext(scripts, {
    document,
    navigator: { locks: shared.locks },
    localStorage: {
      getItem(key) {
        if (!key.startsWith("labqueue.cancel-requests.")) assert.ok(shared.heldLocks.has(key));
        if (!shared.available) throw new Error("storage unavailable");
        return shared.values.get(key) ?? null;
      },
      setItem(key, value) {
        if (key === "labqueue.session-change") return shared.signal(pageWindow, key);
        assert.ok(shared.heldLocks.has(key.startsWith("labqueue.cancel-requests.")
          ? `labqueue.task-version.${scope}` : key));
        if (!shared.available) throw new Error("storage unavailable");
        shared.values.set(key, value);
      },
    },
    window: pageWindow, AbortSignal, crypto: require("node:crypto").webcrypto,
    fetch(path, options) {
      assert.equal(options.headers["X-CSRF-Token"], "test-csrf-token");
      assert.ok(options.signal instanceof AbortSignal);
      return new Promise((resolve, reject) => fetches.push({ path, method: options.method,
        reply(body, status = 200) {
          resolve({ status, ok: status >= 200 && status < 300, json: async () => body });
        },
        interrupt() { reject(new TypeError("Synthetic network interruption")); },
      }));
    },
  });

  function textOf(element) {
    return [element.textContent, ...element.children.map(textOf)].filter(Boolean).join("\n");
  }
  function linksOf(element) {
    return element.tag === "a" ? [element] : element.children.flatMap(linksOf);
  }
  return {
    refresh: () => elements.get("refresh-tasks").fire("click"),
    logout: () => elements.get("logout").fire("click"),
    readOutput: () => elements.get("task-results").children[0].children[0].children
      .find((child) => child.tag === "button").fire("click"),
    button: (label) => elements.get("task-results").children[0]?.children[0]?.children
      .find((child) => child.tag === "button" && child.textContent === label),
    confirmWith: (callback) => { pageWindow.confirm = callback; },
    fire: (event, fields) => pageWindow.fire(event, fields),
    checkSession: () => checkInterval(),
    fetches,
    reply: (index, ...args) => fetches[index].reply(...args),
    rows: () => textOf(elements.get("task-results")),
    hidden: () => elements.get("task-results").hidden,
    links: () => linksOf(elements.get("task-results")),
    feedback: () => textOf(elements.get("tasks-feedback")),
  };
}

for (const detail of [false, true]) {
  const view = detail ? "详情" : "列表";
  test(`${view}保留较新的队位与原因，按精确十进制版本比较`, async () => {
    const shared = makeSharedVersionStore();
    const page = makePage(detail, shared);
    const newer = page.refresh();
    page.reply(1, snapshot(detail, "9007199254740993", { queue_position: 1 }));
    await newer;
    page.reply(0, snapshot(detail, "9007199254740992"));
    await settle();
    assert.match(page.rows(), /全局队位 1/);
    assert.match(page.rows(), /两张模拟 GPU 均被占用/);
    assert.equal(page.feedback(), "已更新任务。");
    assert.equal(shared.values.get(versionKey), "9007199254740993");

    const oldReason = page.refresh();
    const newReason = page.refresh();
    page.reply(3, snapshot(detail, "9007199254740994", {
      queue_position: 1, waiting_reason: "gpu_visibility_unknown",
    }));
    await newReason;
    page.reply(2, snapshot(detail, "9007199254740993", { queue_position: 1 }));
    await oldReason;
    assert.match(page.rows(), /全局队位 1/);
    assert.match(page.rows(), /模拟 GPU 占用状态待确认/);
    assert.doesNotMatch(page.rows(), /两张模拟 GPU 均被占用|取消任务/);
    assert.deepEqual([...shared.values], [[versionKey, "9007199254740994"]]);
  });

  test(`${view}显示观测故障、过期及恢复，拒绝迟到的旧观测结果`, async () => {
    const shared = makeSharedVersionStore();
    const page = makePage(detail, shared);
    const observation = (version, reason) => snapshot(detail, version, { waiting_reason: reason });
    page.reply(0, observation("1", "waiting_dispatch"));
    await settle();
    assert.match(page.rows(), /等待调度/);

    const lateAvailable = page.refresh();
    const failed = page.refresh();
    page.reply(2, observation("3", "gpu_monitoring_failed"));
    await failed;
    page.reply(1, observation("2", "waiting_dispatch"));
    await lateAvailable;
    assert.match(page.rows(), /模拟 GPU 监测失败，暂停派发/);
    assert.doesNotMatch(page.rows(), /等待调度/);

    const expired = page.refresh();
    page.reply(3, observation("4", "gpu_observation_expired"));
    await expired;
    assert.match(page.rows(), /模拟 GPU 观测已过期，暂停派发/);

    const lateExpired = page.refresh();
    const restored = page.refresh();
    page.reply(5, observation("6", "waiting_dispatch"));
    await restored;
    page.reply(4, observation("5", "gpu_observation_expired"));
    await lateExpired;
    assert.match(page.rows(), /等待调度/);
    assert.doesNotMatch(page.rows(), /过期|暂停派发/);
    assert.equal(shared.values.get(versionKey), "6");
  });

  test(`${view}显示原序回队原因和已获准状态，拒绝迟到的排队快照`, async () => {
    const page = makePage(detail);
    page.reply(0, snapshot(detail, "1", {
      queue_position: 1, waiting_reason: "requeue_required",
    }));
    await settle();
    assert.match(page.rows(), /全局队位 1/);
    assert.match(page.rows(), /有更早的任务需先按原序回队/);

    const old = page.refresh();
    const admitted = page.refresh();
    page.reply(2, snapshot(detail, "3", {
      status: "admitted", queue_position: null, waiting_reason: null,
    }));
    await admitted;
    page.reply(1, snapshot(detail, "2", { queue_position: 1 }));
    await old;
    assert.match(page.rows(), /已获准/);
    assert.doesNotMatch(page.rows(), /全局队位|回队|取消任务/);
    assert.equal(page.feedback(), "已更新任务。");
  });

  test(`${view}显示运行中并拒绝迟到的已获准快照`, async () => {
    const page = makePage(detail);
    page.reply(0, snapshot(detail, "1"));
    await settle();
    const admitted = page.refresh();
    const running = page.refresh();
    page.reply(2, snapshot(detail, "3", {
      status: "running", queue_position: null, waiting_reason: null,
    }));
    await running;
    page.reply(1, snapshot(detail, "2", {
      status: "admitted", queue_position: null, waiting_reason: null,
    }));
    await admitted;
    assert.match(page.rows(), /运行中/);
    assert.doesNotMatch(page.rows(), /已获准|全局队位|取消任务/);
  });

  for (const [status, failure_reason, label] of [
    ["completed", undefined, /已完成/],
    ["failed", "oom", /失败.*显存不足（OOM）/],
    ["failed", undefined, /失败.*未提供具体原因/],
  ]) {
    test(`${view}显示终态 ${failure_reason || status} 并拒绝迟到的运行快照`, async () => {
      const page = makePage(detail);
      const running = { status: "running", queue_position: null, waiting_reason: null };
      page.reply(0, snapshot(detail, "1", running));
      await settle();
      const old = page.refresh();
      const latest = page.refresh();
      page.reply(2, snapshot(detail, "3", { ...running, status, failure_reason }));
      await latest;
      page.reply(1, snapshot(detail, "2", running));
      await old;
      assert.match(page.rows(), label);
      assert.doesNotMatch(page.rows(), /运行中|全局队位/);
    });
  }

  test(`${view}拒绝缺版本、不可比较版本或不完整的快照`, async () => {
    const shared = makeSharedVersionStore();
    const page = makePage(detail, shared);
    const good = snapshot(detail, "2");
    page.reply(0, good);
    await settle();
    const before = page.rows();
    assert.match(before, /全局队位 2/);
    const invalid = [
      { ...good, snapshot_version: undefined },
      { ...good, snapshot_version: 10 },
      { ...good, snapshot_version: "10\n" },
      snapshot(detail, "10", { waiting_reason: undefined }),
      snapshot(detail, "10", { can_cancel: "true" }),
      snapshot(detail, "10", { failure_reason: "oom" }),
      snapshot(detail, "10", {
        status: "failed", queue_position: null, waiting_reason: null, failure_reason: 3,
      }),
      detail ? snapshot(true, "10", { task_id: "98557b23-2905-4cbb-8dfe-e359f51badc8" })
        : { snapshot_version: "10", task },
    ];
    for (const [i, bad] of invalid.entries()) {
      const refresh = page.refresh();
      page.reply(i + 1, bad);
      await refresh;
      assert.equal(page.rows(), before);
      assert.match(page.feedback(), /上次确认/);
      assert.equal(shared.values.get(versionKey), "2");
    }
  });
}

test("另一标签退出会清除列表、详情和输出，迟到的任务与输出响应无法恢复画面", async () => {
  const shared = makeSharedVersionStore();
  const list = makePage(false, shared);
  const detail = makePage(true, shared);
  const delayedDetail = makePage(true, shared);
  const outputId = "537fa1a6-e9d7-4c46-9f22-ddfbf6a3b0a4";
  list.reply(0, snapshot(false, "2"));
  for (const page of [detail, delayedDetail]) {
    page.reply(0, { ...snapshot(true, "2"), output_id: outputId });
  }
  await settle();
  const shown = detail.readOutput();
  assert.equal(detail.fetches[1].path, `/api/tasks/${taskId}/outputs/${outputId}`);
  detail.reply(1, { text: "<b>synthetic-private-log</b>" });
  await shown;
  assert.match(detail.rows(), /<b>synthetic-private-log<\/b>/);
  const lateOutput = delayedDetail.readOutput();
  const lateList = list.refresh();

  const source = makePage(false, shared);
  const logout = source.logout();
  assert.equal(source.fetches[1].path, "/api/session");
  assert.equal(source.fetches[1].method, "DELETE");
  shared.cookie = "";
  source.reply(1, null, 204);
  await logout;
  for (const page of [list, detail, delayedDetail]) {
    assert.equal(page.rows(), "");
    assert.match(page.feedback(), /会话已失效/);
  }
  list.reply(1, snapshot(false, "3"));
  delayedDetail.reply(1, { text: "synthetic-late-output" });
  await Promise.all([lateList, lateOutput]);
  assert.equal(list.rows(), "");
  assert.equal(delayedDetail.rows(), "");
  assert.equal(shared.values.get(versionKey), "2");
  assert.equal(list.fetches.length, 2);
  assert.equal(delayedDetail.fetches.length, 2);
});

test("退出响应中断且 Cookie 未变时，服务端会话核对清屏并拒绝迟到内容", async () => {
  const shared = makeSharedVersionStore();
  const source = makePage(false, shared);
  const list = makePage(false, shared);
  const detail = makePage(true, shared);
  const pendingDetail = makePage(true, shared);
  const outputId = "537fa1a6-e9d7-4c46-9f22-ddfbf6a3b0a4";
  list.reply(0, snapshot(false, "2"));
  for (const page of [detail, pendingDetail]) {
    page.reply(0, { ...snapshot(true, "2"), output_id: outputId });
  }
  await settle();
  const output = detail.readOutput();
  detail.reply(1, { text: "synthetic-visible-output" });
  await output;
  assert.match(list.rows(), /合成任务/);
  assert.match(detail.rows(), /synthetic-visible-output/);
  const lateList = list.refresh();
  const lateOutput = pendingDetail.readOutput();

  const logout = source.logout();
  assert.equal(source.fetches[1].method, "DELETE");
  source.fetches[1].interrupt();
  await logout;
  assert.equal(shared.cookie, "labqueue_view_scope=test-session");

  const checks = [list, detail, pendingDetail].map((page) => {
    const index = page.fetches.length;
    const promise = page.checkSession();
    assert.equal(page.fetches[index]?.path, "/api/session");
    assert.equal(page.fetches[index].method, "GET");
    assert.strictEqual(page.checkSession(), promise);
    assert.equal(page.fetches.length, index + 1);
    return { promise, deny: () => page.reply(index, {}, 401) };
  });
  list.reply(1, snapshot(false, "3", { name: "synthetic-late-task" }));
  await settle();
  assert.doesNotMatch(list.rows(), /synthetic-late-task/);
  for (const { deny } of checks) deny();
  await Promise.all(checks.map(({ promise }) => promise));
  for (const page of [list, detail, pendingDetail]) {
    assert.equal(page.rows(), "");
    assert.match(page.feedback(), /会话已失效/);
  }

  pendingDetail.reply(1, { text: "synthetic-late-output" });
  await Promise.all([lateList, lateOutput]);
  assert.equal(list.rows(), "");
  assert.equal(pendingDetail.rows(), "");
});

test("另一标签登录切换账号后，晚到通知不会清除新页，旧页先清除任务", async () => {
  const shared = makeSharedVersionStore();
  const list = makePage(false, shared);
  list.reply(0, snapshot(false, "2"));
  await settle();

  let submitLogin;
  let notice;
  const button = {};
  const login = {
    querySelector: () => button,
    addEventListener: (_event, handler) => { submitLogin = handler; },
  };
  const elements = {
    login, username: { value: "demo-b" }, password: { value: "synthetic-password" },
    "login-feedback": {},
  };
  runInNewContext(submissionScript, {
    document: { getElementById: (id) => elements[id] ?? null },
    window: { location: { reload() {} } }, AbortSignal, crypto: require("node:crypto").webcrypto,
    localStorage: { setItem(key) { notice = key; } },
    async fetch(path, { method, body }) {
      assert.equal(path, "/api/session");
      assert.equal(method, "POST");
      assert.equal(JSON.parse(body).username, "demo-b");
      shared.cookie = "labqueue_view_scope=replacement-session";
      return { status: 200, ok: true, json: async () => ({}) };
    },
  });
  const late = list.refresh();
  await submitLogin({ preventDefault() {} });
  assert.equal(notice, "labqueue.session-change");
  // 即使通知尚未派发，来自旧会话的响应也会触发清屏。
  list.reply(1, snapshot(false, "3"));
  await late;
  assert.equal(list.rows(), "");
  assert.match(list.feedback(), /会话已失效/);

  const replacement = makePage(false, shared, "replacement-session");
  replacement.reply(0, snapshot(false, "3", { name: "新账号任务" }));
  await settle();
  shared.signal(null, notice);
  assert.match(replacement.rows(), /新账号任务/);
  assert.equal(list.rows(), "");
});

test("浏览器恢复旧详情时先核对会话，未收到通知也不会显示已撤销的画面", async () => {
  const shared = makeSharedVersionStore();
  const detail = makePage(true, shared);
  detail.reply(0, snapshot(true, "2"));
  await settle();
  const late = detail.refresh();
  detail.fire("pagehide", { persisted: true });
  assert.equal(detail.hidden(), true);
  shared.cookie = "";
  await detail.fire("pageshow", { persisted: true });
  assert.equal(detail.rows(), "");
  assert.match(detail.feedback(), /会话已失效/);
  detail.reply(1, snapshot(true, "3"));
  await late;
  assert.equal(detail.rows(), "");
  assert.equal(detail.fetches.length, 2);
});

test("可见标签在会话通知丢失时通过定期核对清屏", async () => {
  const shared = makeSharedVersionStore();
  const list = makePage(false, shared);
  list.reply(0, snapshot(false, "2"));
  await settle();
  assert.match(list.rows(), /合成任务/);
  shared.cookie = "labqueue_view_scope=other-session";
  list.checkSession();
  assert.equal(list.rows(), "");
  assert.equal(list.fetches.length, 1);
});

test("历史恢复时即使 Cookie 仍匹配，也要等服务端确认才可重现私有画面", async () => {
  const detail = makePage(true);
  detail.reply(0, snapshot(true, "2"));
  await settle();
  const old = detail.refresh();
  detail.fire("pagehide", { persisted: true });
  const restored = detail.fire("pageshow", { persisted: true });
  await settle();
  detail.reply(1, snapshot(true, "3"));
  await old;
  assert.equal(detail.hidden(), true);
  detail.reply(2, {}, 401);
  await restored;
  assert.equal(detail.rows(), "");
  assert.match(detail.feedback(), /会话已失效/);
});

test("历史恢复拒绝低版本快照，也拒绝返回前发出的私有输出响应", async () => {
  const shared = makeSharedVersionStore();
  const detail = makePage(true, shared);
  const list = makePage(false, shared);
  const outputId = "537fa1a6-e9d7-4c46-9f22-ddfbf6a3b0a4";
  const withOutput = (version) => ({ ...snapshot(true, version), output_id: outputId });
  detail.reply(0, withOutput("2"));
  await settle();
  list.reply(0, snapshot(false, "4"));
  await settle();
  const output = detail.readOutput();
  detail.reply(1, { text: "synthetic-before-history" });
  await output;
  assert.match(detail.rows(), /synthetic-before-history/);

  detail.fire("pagehide", { persisted: true });
  const firstRestore = detail.fire("pageshow", { persisted: true });
  await settle();
  detail.reply(2, { snapshot_version: "3" }, 404);
  await firstRestore;
  assert.equal(detail.hidden(), true);
  assert.match(detail.feedback(), /尚未取得当前任务/);

  const recheck = detail.refresh();
  detail.reply(3, withOutput("4"));
  await recheck;
  assert.equal(detail.hidden(), false);
  assert.doesNotMatch(detail.rows(), /synthetic-before-history/);
  const lateOutput = detail.readOutput();
  detail.fire("pagehide", { persisted: true });
  const secondRestore = detail.fire("pageshow", { persisted: true });
  await settle();
  detail.reply(4, { text: "synthetic-late-history-output" });
  await lateOutput;
  assert.equal(detail.hidden(), true);
  assert.doesNotMatch(detail.rows(), /synthetic-late-history-output/);
  detail.reply(5, withOutput("4"));
  await secondRestore;
  assert.equal(detail.hidden(), false);
  assert.doesNotMatch(detail.rows(), /synthetic-late-history-output/);

  const freshOutput = detail.readOutput();
  detail.reply(6, { text: "synthetic-current-output" });
  await freshOutput;
  assert.match(detail.rows(), /synthetic-current-output/);
});

test("不可见的原任务名保留有效快照并用任务编号提供列表链接", async () => {
  const shared = makeSharedVersionStore();
  const list = makePage(false, shared);
  const detail = makePage(true, shared);
  list.reply(0, snapshot(false, "2", { name: "\uFEFF" }));
  detail.reply(0, snapshot(true, "2", { name: "\uFEFF" }));
  await settle();
  const [link] = list.links();
  assert.equal(link.href, `/tasks/${taskId}`);
  assert.equal(link.textContent, `任务 ${taskId}`);
  for (const page of [list, detail]) {
    assert.match(page.rows(), new RegExp(`任务 ${taskId}`));
    assert.match(page.rows(), /确认时间 2026-11-25 01:02:03 UTC/);
    assert.equal(page.feedback(), "已更新任务。");
  }
});

test("同版本刷新保留任务链接节点，让键盘焦点留在原处", async () => {
  const page = makePage(false);
  page.reply(0, snapshot(false, "2"));
  await settle();
  const [link] = page.links();
  assert.equal(link.href, `/tasks/${taskId}`);

  const refresh = page.refresh();
  page.reply(1, snapshot(false, "2"));
  await refresh;

  assert.strictEqual(page.links()[0], link);
  assert.equal(page.feedback(), "已更新任务。");
});

test("晚到的同版本成功响应不能清除较近一次刷新失败的上次确认提示", async () => {
  const page = makePage(false);
  page.reply(0, snapshot(false, "2"));
  await settle();
  const earlier = page.refresh();
  const later = page.refresh();
  page.reply(2, {}, 503);
  await later;
  const lastConfirmed = page.feedback();
  assert.match(lastConfirmed, /上次确认/);
  page.reply(1, snapshot(false, "2"));
  await earlier;
  assert.equal(page.feedback(), lastConfirmed);

  const fresh = page.refresh();
  page.reply(3, snapshot(false, "2"));
  await fresh;
  assert.equal(page.feedback(), "已更新任务。");
});

test("列表提高版本下限后，详情缓存、迟到和重开画面遵循同一版本", async () => {
  const shared = makeSharedVersionStore();
  const list = makePage(false, shared);
  const detail = makePage(true, shared);
  list.reply(0, snapshot(false, "2"));
  detail.reply(0, snapshot(true, "2"));
  await settle();
  const oldDetail = detail.refresh();
  const newList = list.refresh();
  list.reply(1, snapshot(false, "3", { queue_position: 1, waiting_reason: "gpu_visibility_unknown" }));
  await newList;
  await detail.fire("storage", { key: versionKey });
  assert.match(detail.feedback(), /上次确认/);
  detail.reply(1, snapshot(true, "2"));
  await oldDetail;
  assert.match(detail.feedback(), /上次确认/);
  assert.match(detail.rows(), /全局队位 2/);

  for (const detailView of [false, true]) {
    const reopened = makePage(detailView, shared);
    reopened.reply(0, snapshot(detailView, "2"));
    await settle();
    assert.equal(reopened.rows(), "");
    assert.match(reopened.feedback(), /尚未取得当前任务/);
  }

  const returned = detail.fire("pageshow", { persisted: true });
  assert.match(detail.feedback(), /上次确认/);
  await settle();
  detail.reply(2, snapshot(true, "3", { queue_position: 1, waiting_reason: "gpu_visibility_unknown" }));
  await returned;
  assert.equal(detail.feedback(), "已更新任务。");
  assert.match(detail.rows(), /全局队位 1/);
  assert.match(detail.rows(), /待确认/);

  detail.fire("pagehide", { persisted: true });
  assert.match(detail.feedback(), /上次确认/);
  const changeWhileHidden = list.refresh();
  list.reply(2, snapshot(false, "4", { queue_position: 1 }));
  await changeWhileHidden;
  const restored = detail.fire("pageshow", { persisted: true });
  await settle();
  detail.reply(3, snapshot(true, "3", { queue_position: 1, waiting_reason: "gpu_visibility_unknown" }));
  await restored;
  assert.match(detail.feedback(), /上次确认/);
  assert.match(detail.rows(), /模拟 GPU 占用状态待确认/);
  assert.equal(shared.values.get(versionKey), "4");
});

test("读取失败时保留画面，并提示浏览器版本协调所需能力", async () => {
  const shared = makeSharedVersionStore();
  const detail = makePage(true, shared);
  detail.reply(0, snapshot(true, "2"));
  await settle();
  const before = detail.rows();

  const failed = detail.refresh();
  detail.reply(1, {}, 503);
  await failed;
  assert.equal(detail.rows(), before);
  assert.match(detail.feedback(), /上次确认/);

  shared.available = false;
  const unverified = detail.refresh();
  await unverified;
  assert.equal(detail.rows(), before);
  assert.match(detail.feedback(), /上次确认。请允许访问浏览器本地存储/);

  const withoutLocks = makeSharedVersionStore();
  withoutLocks.locks = undefined;
  const page = makePage(false, withoutLocks);
  page.reply(0, snapshot(false, "3"));
  await settle();
  assert.equal(page.rows(), "");
  assert.match(page.feedback(), /需要支持 Web Locks/);
});

test("详情按共享版本核对不存在，缺版本或较旧的 404 不会清空较新画面", async () => {
  const shared = makeSharedVersionStore();
  const detail = makePage(true, shared);
  detail.reply(0, snapshot(true, "3", { queue_position: 1 }));
  await settle();
  const before = detail.rows();
  const earlier404 = detail.refresh();
  detail.reply(1, { snapshot_version: "2" }, 404);
  await earlier404;
  assert.equal(detail.rows(), before);
  assert.equal(detail.feedback(), "已更新任务。");
  assert.equal(shared.values.get(versionKey), "3");

  const unversioned404 = detail.refresh();
  detail.reply(2, {}, 404);
  await unversioned404;
  assert.equal(detail.rows(), before);
  assert.match(detail.feedback(), /上次确认/);

  const list = makePage(false, shared);
  list.reply(0, snapshot(false, "4"));
  await settle();
  const belowSharedFloor = detail.refresh();
  detail.reply(3, { snapshot_version: "3" }, 404);
  await belowSharedFloor;
  assert.equal(detail.rows(), before);
  assert.match(detail.feedback(), /上次确认/);

  const older = detail.refresh();
  const missing = detail.refresh();
  detail.reply(5, { snapshot_version: "5" }, 404);
  await missing;
  assert.equal(detail.rows(), "");
  assert.equal(detail.feedback(), "未找到该任务。");
  detail.reply(4, snapshot(true, "4", { queue_position: 1 }));
  await older;
  assert.equal(detail.rows(), "");
  assert.equal(detail.feedback(), "未找到该任务。");
  assert.equal(shared.values.get(versionKey), "5");
});

for (const detail of [false, true]) {
  const view = detail ? "详情" : "列表";
  for (const status of [401, 403]) {
    test(`${view}收到 ${status} 后，迟到的成功响应不能再显示任务`, async () => {
      const shared = makeSharedVersionStore();
      const page = makePage(detail, shared);
      page.reply(0, snapshot(detail, "2"));
      await settle();
      assert.match(page.rows(), /全局队位 2/);

      const earlier = page.refresh();
      const denied = page.refresh();
      page.reply(2, {}, status);
      await denied;
      assert.equal(page.rows(), "");
      assert.match(page.feedback(), /会话已失效/);

      page.reply(1, snapshot(detail, "3"));
      await earlier;
      assert.equal(page.rows(), "");
      assert.match(page.feedback(), /会话已失效/);
      assert.equal(shared.values.get(versionKey), "2");
    });
  }
}

for (const status of ["queued", "admitted"]) {
test(`${status} 跨页核对未知、主动封存、完整快照恢复入口，旧回执不能替换新请求`, async () => {
  const shared = makeSharedVersionStore();
  const detail = makePage(true, shared);
  const list = makePage(false, shared);
  const queued = (view, version, fact) => snapshot(view, version, {
    can_cancel: true, ...(status === "admitted"
      ? { status, queue_position: null, waiting_reason: null } : {}), ...(fact && { cancellation: fact }),
  });
  detail.reply(0, queued(true, "3"));
  list.reply(0, queued(false, "3"));
  await settle();
  const oldCommand = detail.button("取消任务").fire("click");
  await settle();
  const old = detail.fetches[1];
  const id = old.path.split("/").at(-1);
  const fact = (state) => ({ request_id: id, state });
  const receipt = (version, state) => ({
    task_id: taskId, snapshot_version: version, ...fact(state),
  });
  assert.equal(old.method, "POST");
  assert.doesNotMatch(detail.rows(), /已取消/);
  const reopened = makePage(true, shared);
  assert.equal(reopened.fetches[0].path, `/api/tasks/${taskId}?request_id=${id}`);
  reopened.reply(0, queued(true, "3", fact("unknown")));
  await settle();
  assert.equal(reopened.button("取消任务"), undefined);
  assert.match(reopened.rows(), /取消结果未知/);
  reopened.confirmWith((message) => {
    assert.match(message, /未受理.*封存.*已受理/);
    return false;
  });
  await reopened.button("核对并封存旧取消请求").fire("click");
  assert.equal(reopened.fetches.length, 1);
  reopened.confirmWith(() => true);
  const read = list.refresh();
  assert.equal(list.fetches[1].path, `/api/tasks?pending=${taskId}.${id}`);
  list.reply(1, queued(false, "3", fact("unknown")));
  await read;
  assert.match(list.rows(), /取消结果未知/);
  assert.ok(list.fetches.every((req) => req.method === "GET"));

  const seal = reopened.button("核对并封存旧取消请求").fire("click");
  await settle();
  assert.equal(reopened.fetches[1].path, old.path + "/seal");
  reopened.reply(1, receipt("4", "sealed"));
  await settle();
  assert.equal(shared.values.get(versionKey), "4");
  assert.equal(reopened.button("取消任务"), undefined);
  assert.match(reopened.rows(), status === "admitted" ? /已获准/ : /全局队位 2/);
  reopened.reply(2, queued(true, "4", fact("sealed")));
  await seal;
  reopened.confirmWith((message) => { assert.ok(message.includes(task.name)); return false; });
  await reopened.button("取消任务").fire("click");
  assert.equal(reopened.fetches.length, 3);
  assert.equal(JSON.parse(shared.values.get("labqueue.cancel-requests.test-session"))[taskId], id);
  reopened.confirmWith(() => true);
  const next = reopened.button("取消任务").fire("click");
  await settle();
  const nextId = reopened.fetches[3].path.split("/").at(-1);
  assert.notEqual(nextId, id);
  old.reply(receipt("80", "sealed"));
  await oldCommand;
  assert.equal(shared.values.get(versionKey), "80");
  assert.equal(detail.fetches.length, 2);
  assert.ok(reopened.button("取消任务").disabled);
  reopened.fetches[3].interrupt();
  await next;
  assert.doesNotMatch(reopened.rows(), /已取消/);
  assert.ok(reopened.button("取消任务").disabled);
  const reconcile = reopened.refresh();
  assert.equal(reopened.fetches[4].method, "GET");
  assert.match(reopened.fetches[4].path, new RegExp(nextId));
  reopened.reply(4, queued(true, "79", { request_id: nextId, state: "processing" }));
  await reconcile;
  assert.ok(reopened.button("取消任务").disabled);
  assert.match(reopened.feedback(), /上次确认/);
  for (const [page, view] of [[reopened, true], [list, false]]) {
    const before = page.rows();
    const stale = page.refresh();
    page.reply(page.fetches.length - 1,
      queued(view, "79", { request_id: nextId, state: "processing" }));
    await stale;
    assert.equal(page.rows(), before);
    assert.match(page.feedback(), /上次确认/);
    const refresh = page.refresh();
    page.reply(page.fetches.length - 1,
      queued(view, "80", { request_id: nextId, state: "processing" }));
    await refresh;
    assert.match(page.rows(), /正在处理/);
    assert.equal(page.button("取消任务"), undefined);
    assert.equal(JSON.parse(shared.values.get("labqueue.cancel-requests.test-session"))[taskId], nextId);
  }
});
}

test("取消回执提高共享版本下限，列表和详情仅从完整快照显示已取消", async () => {
  const shared = makeSharedVersionStore();
  const detail = makePage(true, shared);
  const list = makePage(false, shared);
  detail.reply(0, snapshot(true, "2", { can_cancel: true }));
  list.reply(0, snapshot(false, "2", { can_cancel: true }));
  await settle();
  const late = list.refresh();
  const click = detail.button("取消任务").fire("click");
  await settle();
  const request_id = detail.fetches[1].path.split("/").at(-1);
  const cancellation = { request_id, state: "cancelled" };
  detail.reply(1, { task_id: taskId, snapshot_version: "4", ...cancellation });
  await settle();
  assert.match(detail.rows(), /全局队位 2/);
  assert.doesNotMatch(detail.rows(), /已取消/);
  list.reply(1, snapshot(false, "3", { can_cancel: true, queue_position: 1 }));
  await late;
  assert.match(list.rows(), /全局队位 2/);
  assert.match(list.feedback(), /上次确认/);
  const done = { status: "cancelled", queue_position: null, waiting_reason: null,
    can_cancel: false, cancellation };
  detail.reply(2, snapshot(true, "4", done));
  await click;
  const read = list.refresh();
  assert.equal(list.fetches[2].path, "/api/tasks");
  list.reply(2, snapshot(false, "4", { ...done, cancellation: undefined }));
  await read;
  for (const page of [list, detail]) {
    assert.match(page.rows(), /已取消/);
    assert.doesNotMatch(page.rows(), /全局队位|取消任务/);
  }
});

test("打开的详情响应待核对键变化，错配快照推进版本，迟到的匹配回执仍触发只读核对", async () => {
  const shared = makeSharedVersionStore();
  const page = makePage(true, shared);
  const other = makePage(true, shared);
  const pendingKey = "labqueue.cancel-requests.test-session";
  for (const tab of [page, other]) tab.reply(0, snapshot(true, "1", { can_cancel: true }));
  await settle();
  page.confirmWith((message) => { assert.ok(message.includes(task.name)); return false; });
  await page.button("取消任务").fire("click");
  assert.equal(page.fetches.length, 1);
  assert.ok(!shared.values.has(pendingKey));

  const delayed = other.refresh();
  page.confirmWith(() => true);
  const click = page.button("取消任务").fire("click");
  await settle();
  const id = JSON.parse(shared.values.get(pendingKey))[taskId];
  const storageRead = other.fire("storage", { key: pendingKey });
  assert.ok(other.button("取消任务").disabled);
  assert.equal(other.fetches[2].path, `/api/tasks/${taskId}?request_id=${id}`);
  other.reply(1, snapshot(true, "7", { can_cancel: true, queue_position: 1 }));
  await delayed;
  assert.equal(shared.values.get(versionKey), "7");
  assert.match(other.rows(), /全局队位 2/);
  assert.match(other.feedback(), /上次确认/);

  const processing = { request_id: id, state: "processing" };
  other.reply(2, snapshot(true, "6", { cancellation: processing }));
  await storageRead;
  page.reply(1, { ...processing, task_id: taskId, snapshot_version: "5" });
  await settle();
  assert.equal(shared.values.get(versionKey), "7");
  assert.equal(page.fetches[2].method, "GET");
  page.reply(2, snapshot(true, "7", { cancellation: processing }));
  await click;
  assert.match(page.rows(), /正在处理/);
  assert.equal(page.button("取消任务"), undefined);
});

test("只从匹配的终态完整快照清除已封存或已终结的跟踪", async () => {
  const pendingKey = "labqueue.cancel-requests.test-session";
  const request_id = "0bd4e56f-5f82-4ff6-a75d-d24fe13c8a59";
  for (const state of ["unknown", "processing", "sealed", "ineligible"]) {
    for (const status of ["completed", "failed"]) {
      const shared = makeSharedVersionStore();
      shared.values.set(pendingKey, JSON.stringify({ [taskId]: request_id }));
      const page = makePage(true, shared);
      page.reply(0, snapshot(true, "9", { status, queue_position: null, waiting_reason: null,
        cancellation: { request_id, state } }));
      await settle();
      assert.equal(JSON.parse(shared.values.get(pendingKey))[taskId],
        ["unknown", "processing"].includes(state) ? request_id : undefined);
      assert.equal(page.button("取消任务"), undefined);
    }
  }
});

for (const detail of [false, true]) {
  const view = detail ? "详情" : "列表";
  const admitted = { status: "admitted", can_cancel: true,
    queue_position: null, waiting_reason: null };
  const cancelled = { status: "cancelled", can_cancel: false,
    queue_position: null, waiting_reason: null };
  const otherTask = { ...task, task_id: "0bd4e56f-5f82-4ff6-a75d-d24fe13c8a59", name: "另一任务" };
  const complete = (version, changes) => {
    const data = snapshot(detail, version, changes);
    if (!detail) data.tasks.push(otherTask);
    return data;
  };

  test(`${view} A4-A5 已领取未知请求重开和封存均只从完整快照恢复`, async () => {
    const shared = makeSharedVersionStore();
    const pendingKey = "labqueue.cancel-requests.test-session";
    const id = "58d3dc13-67b6-427b-81fb-524090a6f733";
    shared.values.set(pendingKey, JSON.stringify({ [taskId]: id }));
    const page = makePage(detail, shared);
    const fact = (state) => ({ request_id: id, state });
    page.reply(0, complete("3", { ...admitted, cancellation: fact("unknown") }));
    await settle();
    assert.match(page.rows(), /已获准.*\n.*确认时间/s);
    assert.match(page.rows(), /取消结果未知/);
    assert.equal(page.button("取消任务"), undefined);
    const refresh = page.refresh();
    page.reply(1, complete("3", { ...admitted, cancellation: fact("unknown") }));
    await refresh;
    const reopened = makePage(detail, shared);
    reopened.reply(0, complete("3", { ...admitted, cancellation: fact("unknown") }));
    await settle();
    assert.ok(page.fetches.every((r) => r.method === "GET"));
    assert.ok(reopened.fetches.every((r) => r.method === "GET"));
    // 列表只导航，封存命令仍由详情页发出。
    const commandPage = detail ? reopened : makePage(true, shared);
    if (!detail) {
      commandPage.reply(0, snapshot(true, "3", { ...admitted, cancellation: fact("unknown") }));
      await settle();
    }
    const before = page.rows();
    const seal = commandPage.button("核对并封存旧取消请求").fire("click");
    await settle();
    commandPage.reply(1, { task_id: taskId, snapshot_version: "4", ...fact("sealed") });
    await settle();
    assert.equal(page.rows(), before);
    assert.equal(commandPage.button("取消任务"), undefined);
    commandPage.reply(2, snapshot(true, "4", { ...admitted, cancellation: fact("sealed") }));
    await seal;
    assert.ok(!commandPage.button("取消任务").disabled);
    const refreshed = page.refresh();
    page.reply(2, complete("4", { ...admitted, cancellation: fact("sealed") }));
    await refreshed;
    assert.match(page.rows(), /已获准/);
    if (!detail) assert.match(page.rows(), /另一任务/);
  });

  test(`${view} A10-A11 取消完整快照先到，跨页旧响应和局部回执不能清行或回退`, async () => {
    const shared = makeSharedVersionStore();
    const page = makePage(detail, shared);
    page.reply(0, complete("2", admitted));
    await settle();
    const old = page.refresh();
    const commandPage = makePage(true, shared);
    commandPage.reply(0, snapshot(true, "2", admitted));
    await settle();
    const action = commandPage.button("取消任务").fire("click");
    await settle();
    const request_id = commandPage.fetches[1].path.split("/").at(-1);
    const cancellation = { request_id, state: "cancelled" };
    const fresh = page.refresh();
    page.reply(2, complete("4", { ...cancelled, cancellation }));
    await fresh;
    const before = page.rows();
    assert.match(before, /已取消/);
    assert.doesNotMatch(before, /已获准|取消任务/);
    if (!detail) assert.match(before, /另一任务/);
    // 完整终态快照已清除待核对 ID；迟到回执不得替换画面或新增请求。
    commandPage.reply(1, { task_id: taskId, snapshot_version: "4", ...cancellation });
    await action;
    page.reply(1, complete("2", admitted));
    await old;
    assert.equal(page.rows(), before);
    assert.equal(commandPage.fetches.length, 2);
    assert.equal(shared.values.get(versionKey), "4");
  });

  test(`${view} 已过截止点反馈拒绝非法原因、缺字段、错 ID 及旧处理中响应`, async () => {
    const shared = makeSharedVersionStore();
    const id = "58d3dc13-67b6-427b-81fb-524090a6f733";
    shared.values.set("labqueue.cancel-requests.test-session", JSON.stringify({ [taskId]: id }));
    const page = makePage(detail, shared);
    const cancellation = { request_id: id, state: "ineligible", reason: "past_cutoff" };
    const running = { status: "running", can_cancel: false, queue_position: null, waiting_reason: null };
    page.reply(0, complete("7", { ...running, cancellation }));
    await settle();
    assert.match(page.rows(), /已过取消截止点/);
    const before = page.rows();
    for (const changes of [
      { cancellation: { ...cancellation, reason: "invented" } },
      { cancellation: { ...cancellation, state: "processing" } },
      { cancellation: { ...cancellation, request_id: otherTask.task_id } },
      { can_cancel: undefined },
      { can_cancel: true },
    ]) {
      const reading = page.refresh();
      page.reply(page.fetches.length - 1, complete(changes.cancellation?.request_id === otherTask.task_id ? "7" : "8",
        { ...running, cancellation, ...changes }));
      await reading;
      assert.equal(page.rows(), before);
      assert.equal(shared.values.get(versionKey), "7");
      assert.match(page.feedback(), changes.cancellation?.request_id === otherTask.task_id
        ? /取消请求已变化/ : /暂时无法读取任务/);
      assert.equal(page.button("取消任务"), undefined);
    }
    const old = page.refresh();
    page.reply(page.fetches.length - 1, complete("6", { ...admitted,
      cancellation: { request_id: id, state: "processing" } }));
    await old;
    assert.equal(page.rows(), before);
  });
}

for (const detail of [false, true]) {
  test(`${detail ? "详情" : "列表"} A11 局部回执先到保留完整画面和其他任务`, async () => {
    const shared = makeSharedVersionStore();
    const admitted = { status: "admitted", can_cancel: true,
      queue_position: null, waiting_reason: null };
    const page = makePage(detail, shared);
    const full = snapshot(detail, "2", admitted);
    if (!detail) full.tasks.push({ ...task,
      task_id: "0bd4e56f-5f82-4ff6-a75d-d24fe13c8a59", name: "保留的另一任务" });
    page.reply(0, full);
    await settle();
    const before = page.rows();
    const commandPage = detail ? page : makePage(true, shared);
    if (!detail) {
      commandPage.reply(0, snapshot(true, "2", admitted));
      await settle();
    }
    const click = commandPage.button("取消任务").fire("click");
    await settle();
    const request_id = commandPage.fetches[1].path.split("/").at(-1);
    const cancellation = { request_id, state: "cancelled" };
    commandPage.reply(1, { task_id: taskId, snapshot_version: "4", ...cancellation });
    await settle();
    await page.fire("storage", { key: versionKey });
    assert.equal(page.rows(), before);
    assert.match(page.feedback(), /上次确认/);
    if (detail) assert.ok(page.button("取消任务").disabled);
    else assert.match(page.rows(), /保留的另一任务/);
    commandPage.reply(2, snapshot(true, "4", { status: "cancelled", can_cancel: false,
      queue_position: null, waiting_reason: null, cancellation }));
    await click;
    if (detail) assert.match(page.rows(), /已取消/);
    else assert.equal(page.rows(), before);
  });
}


test("非法原因的取消命令回执不提高版本或触发完整核对", async () => {
  for (const reason of ["invented", "past_cutoff"]) {
    const shared = makeSharedVersionStore();
    const page = makePage(true, shared);
    page.reply(0, snapshot(true, "2", { status: "admitted", can_cancel: true,
      queue_position: null, waiting_reason: null }));
    await settle();
    const before = page.rows();
    const click = page.button("取消任务").fire("click");
    await settle();
    const request_id = page.fetches[1].path.split("/").at(-1);
    page.reply(1, { task_id: taskId, request_id, snapshot_version: "80",
      state: "cancelled", reason });
    await click;
    assert.equal(shared.values.get(versionKey), "2");
    assert.equal(page.rows(), before);
    assert.equal(page.fetches.length, 2);
    assert.ok(page.button("取消任务").disabled);
  }
});

for (const detail of [false, true]) {
  for (const receiptVersion of ["4", "12"]) {
    test(`${detail ? "详情" : "列表"} 新 ID sealed v10 后旧 ID v${receiptVersion} 仅按版本失效缓存`, async () => {
      const shared = makeSharedVersionStore();
      const commandPage = makePage(true, shared);
      const page = detail ? commandPage : makePage(false, shared);
      const admitted = { status: "admitted", can_cancel: true,
        queue_position: null, waiting_reason: null };
      commandPage.reply(0, snapshot(true, "2", admitted));
      if (!detail) page.reply(0, snapshot(false, "2", admitted));
      await settle();
      const oldCommand = commandPage.button("取消任务").fire("click");
      await settle();
      const oldId = commandPage.fetches[1].path.split("/").at(-1);
      const newId = "58d3dc13-67b6-427b-81fb-524090a6f733";
      const pendingKey = "labqueue.cancel-requests.test-session";
      // 模拟另一页面已完成新 ID 的主动封存，当前页面再读取完整快照。
      shared.values.set(pendingKey, JSON.stringify({ [taskId]: newId }));
      for (const [target, view] of detail ? [[page, true]] : [[commandPage, true], [page, false]]) {
        const read = target.refresh();
        target.reply(target.fetches.length - 1, snapshot(view, "10", { ...admitted,
          cancellation: { request_id: newId, state: "sealed" } }));
        await read;
      }
      const before = page.rows();
      const feedback = page.feedback();
      const requests = commandPage.fetches.length;
      assert.ok(!commandPage.button("取消任务").disabled);
      commandPage.reply(1, { task_id: taskId, request_id: oldId,
        state: "sealed", snapshot_version: receiptVersion });
      await oldCommand;
      if (!detail) await page.fire("storage", { key: versionKey });
      assert.equal(page.rows(), before);
      assert.equal(commandPage.fetches.length, requests);
      assert.equal(JSON.parse(shared.values.get(pendingKey))[taskId], newId);
      if (receiptVersion === "4") {
        assert.equal(shared.values.get(versionKey), "10");
        assert.equal(page.feedback(), feedback);
        assert.ok(!commandPage.button("取消任务").disabled);
      } else {
        assert.equal(shared.values.get(versionKey), "12");
        assert.match(page.feedback(), /上次确认/);
        assert.ok(commandPage.button("取消任务").disabled);
      }
    });
  }
}
