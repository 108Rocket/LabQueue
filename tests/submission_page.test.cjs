const assert = require("node:assert/strict");
const { randomUUID } = require("node:crypto");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(join(__dirname, "../src/labqueue/static/submission.js"), "utf8");
const storageKey = "labqueue.pending-submission.demo-a";
const timeout = () => Promise.reject(new DOMException("test timeout", "TimeoutError"));
const result = (status, body) => ({ status, ok: status === 200, json: async () => body });
const receipt = { task_id: "synthetic-task-id", memory_gib: null };

test("同键提交已确认后，迟到的核对 404 不能显示仍无法确认", async () => {
  let finishLookup;
  const page = makePage((path, options) => {
    if (options.method === "POST") return Promise.resolve(result(200, receipt));
    assert.equal(path, `/api/submissions/${page("lookup-key").value}`);
    return new Promise(resolve => { finishLookup = resolve; });
  });
  const write = page("submission").fire("submit");
  const lookup = page("lookup").fire("submit");
  await write;
  const confirmed = page("submission-feedback").textContent;
  assert.match(confirmed, /已确认入队/);
  finishLookup(result(404, {}));
  await lookup;
  assert.equal(page("submission-feedback").textContent, confirmed);
  assert.doesNotMatch(page("lookup-feedback").textContent, /仍无法确认|正在核对/);
});

test("同键核对成功后迟到的提交超时不能恢复重试", async () => {
  let rejectWrite;
  const storage = new Map();
  const page = makePage((path, options) => options.method === "POST"
    ? new Promise((resolve, reject) => { rejectWrite = reject; })
    : Promise.resolve(result(200, receipt)), { storage });
  const write = page("submission").fire("submit");
  await page("lookup").fire("submit");
  rejectWrite(new Error("simulated timeout"));
  await write;
  assert.match(page("submission-feedback").textContent, /已确认入队/);
  assert.equal(page("retry").hidden, true);
  assert.deepEqual(JSON.parse(storage.get(storageKey)), []);
});

test("旧页核对携带页面会话凭证，拒绝后保留待核对键", async () => {
  const key = randomUUID();
  const storage = new Map([[storageKey, JSON.stringify([key])]]);
  const page = makePage(async (path, options) => {
    assert.equal(options.headers["X-CSRF-Token"], "test-csrf-token");
    return result(403, { detail: "会话凭证已变更，请刷新页面。" });
  }, { storage });
  await page("lookup").fire("submit");
  assert.match(page("lookup-feedback").textContent, /刷新页面/);
  assert.deepEqual(JSON.parse(storage.get(storageKey)), [key]);
});

function makePage(fetch, {
  storage = new Map(), storageDisabled = false, confirm = () => true,
} = {}) {
  const controls = new Map();
  function element(id) {
    if (id === "login") return null;
    if (!controls.has(id)) {
      const handlers = new Map();
      controls.set(id, {
        value: "",
        hidden: true,
        textContent: "",
        dataset: { owner: "demo-a", csrf: "test-csrf-token" },
        setAttribute() {},
        removeAttribute() {},
        reset() {},
        replaceChildren(...children) { this.children = children; },
        querySelector() { return element(`${id}-button`); },
        addEventListener(type, handler) { handlers.set(type, handler); },
        fire(type) { return handlers.get(type)({ currentTarget: this, preventDefault() {} }); },
      });
    }
    return controls.get(id);
  }
  runInNewContext(script, {
    document: { getElementById: element, createElement: () => ({}) },
    sessionStorage: {
      getItem(key) {
        if (storageDisabled) throw new Error("storage disabled");
        return storage.get(key);
      },
      setItem(key, value) {
        if (storageDisabled) throw new Error("storage disabled");
        storage.set(key, value);
      },
    },
    window: { confirm },
    crypto: { randomUUID }, AbortSignal, fetch,
  });
  element("name").value = "合成任务";
  element("command").value = "echo synthetic-ui-command";
  return element;
}

test("回执超时后只读核对及手动重试复用原提交", async () => {
  const calls = [];
  const page = makePage(async (path, options) => {
    calls.push([path, options]);
    if (calls.length === 1) return timeout();
    if (calls.length === 2) return result(404, {});
    return result(200, receipt);
  });
  await page("submission").fire("submit");
  const key = page("lookup-key").value;
  assert.match(page("submission-feedback").textContent, /暂未确认/);
  assert.equal(page("retry").hidden, false);
  assert.equal(calls.length, 1);
  assert.ok(calls[0][1].signal instanceof AbortSignal);
  await page("lookup").fire("submit");
  assert.equal(calls[1][0], `/api/submissions/${key}`);
  assert.equal(calls[1][1].method, "GET");
  assert.match(page("lookup-feedback").textContent, /仍无法确认/);
  await page("retry").fire("click");
  assert.equal(calls[0][1].body, calls[2][1].body);
  assert.equal(JSON.parse(calls[0][1].body).memory_gib, null);
  assert.match(page("submission-feedback").textContent, /已确认入队/);
  assert.equal(page("retry").hidden, true);
});

test("新提交成功后刷新仍可找回另一条未确认的原键", async () => {
  const storage = new Map();
  let calls = 0;
  const page = makePage(async () => ++calls === 1 ? timeout() : result(200, receipt), { storage });
  await page("submission").fire("submit");
  const oldKey = page("lookup-key").value;
  await page("new-submission").fire("click");
  await page("submission").fire("submit");
  assert.notEqual(page("lookup-key").value, oldKey);
  assert.deepEqual(JSON.parse(storage.get(storageKey)), [oldKey]);

  const reopened = makePage(async () => result(200, receipt), { storage });
  assert.equal(reopened("lookup-key").value, oldKey);
  assert.equal(reopened("retry").hidden, true);
  assert.match(reopened("submission-feedback").textContent, /待核对/);
});

test("旧回执不会显示在新核对键旁", async () => {
  let finishLookup;
  const page = makePage(() => new Promise((resolve) => { finishLookup = resolve; }));
  page("lookup-key").value = randomUUID();
  const checking = page("lookup").fire("submit");
  page("lookup-key").value = randomUUID();
  page("lookup-key").fire("input");
  finishLookup(result(200, receipt));
  await checking;
  assert.equal(page("lookup-feedback").textContent, "");
});

test("禁用存储时仍可提交，冲突拒绝后结束手动重试", async () => {
  let calls = 0;
  const page = makePage(
    async () => ++calls === 1 ? timeout() : result(409, { detail: "原提交键已用于不同内容。" }),
    { storageDisabled: true },
  );
  await page("submission").fire("submit");
  assert.ok(page("lookup-key").value);
  assert.equal(page("retry").hidden, false);
  await page("retry").fire("click");
  assert.equal(page("retry").hidden, true);
  assert.equal(page("submit-button").disabled, false);
  assert.match(page("submission-feedback").textContent, /不同内容/);
});

test("数值显存空白由输入约束拦截，非正数错误显示在显存字段旁", async () => {
  const page = makePage(async () => result(422, {
    detail: "请检查请求内容。", fields: { memory_gib: "请输入大于 0 的 GiB 数值，或选择未知。" },
  }));
  page("memory-mode").value = "estimate";
  page("memory-mode").fire("change");
  assert.equal(page("memory_gib").required, true);
  page("memory_gib").value = "0";
  await page("submission").fire("submit");
  assert.match(page("memory_gib-error").textContent, /大于 0/);
});
