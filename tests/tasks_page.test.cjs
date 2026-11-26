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
        assert.ok(shared.heldLocks.has(key));
        if (!shared.available) throw new Error("storage unavailable");
        return shared.values.get(key) ?? null;
      },
      setItem(key, value) {
        if (key === "labqueue.session-change") return shared.signal(pageWindow, key);
        assert.ok(shared.heldLocks.has(key));
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
      snapshot(detail, "10", { can_cancel: true }),
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
  detail.reply(2, snapshot(true, "3", { queue_position: 1 }));
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
